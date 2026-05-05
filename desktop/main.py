import json
import platform
import subprocess
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QPainter,
    QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from shares import ShareManager
from ws_client import WsClient

DEFAULT_CONFIG = {"server_url": "", "public_url": "", "token": ""}

APP_DIR = Path.home() / ".instasend"
APP_DIR.mkdir(mode=0o700, exist_ok=True)

CONFIG_PATH = APP_DIR / "config.json"
SHARES_PATH = APP_DIR / "shares.json"
CLIENT_ID_PATH = APP_DIR / "client_id"


def _get_version() -> str:
    try:
        from _version import __version__
        return __version__
    except ImportError:
        pass
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).parent,
        )
        return result.stdout.strip() or "dev"
    except Exception:
        return "dev"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            config = {**DEFAULT_CONFIG, **json.load(f)}
        try:
            CONFIG_PATH.chmod(0o600)
        except OSError:
            pass
        return config
    return DEFAULT_CONFIG.copy()


def _save_config(config: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(config, indent=2))
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass


def _get_or_create_client_id() -> str:
    """Return the stable per-device client ID, creating it on first run."""
    if CLIENT_ID_PATH.exists():
        return CLIENT_ID_PATH.read_text().strip()
    client_id = uuid.uuid4().hex
    CLIENT_ID_PATH.write_text(client_id)
    CLIENT_ID_PATH.chmod(0o600)
    return client_id


def _to_ws_url(server_url: str) -> str:
    """Convert an HTTP server URL to a WebSocket URL pointing at /ws."""
    url = server_url.strip().rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[8:]
    elif url.startswith("http://"):
        url = "ws://" + url[7:]
    return url + "/ws"


def _hide_dock_icon():
    """Remove the app from the Dock and Cmd+Tab switcher (macOS only)."""
    if platform.system() != "Darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def _make_tray_icon() -> QIcon:
    """Draw a minimal send-arrow icon for the menu bar."""
    size = 22
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#89b4fa"))
    arrow = QPolygon([
        QPoint(11, 2),
        QPoint(19, 13),
        QPoint(14, 13),
        QPoint(14, 20),
        QPoint(8, 20),
        QPoint(8, 13),
        QPoint(3, 13),
    ])
    painter.drawPolygon(arrow)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Cross-thread signals
# ---------------------------------------------------------------------------

class _Signals(QObject):
    download_updated = Signal(str, int)   # local_id, count
    share_assigned   = Signal(str, str)   # local_id, server_hash
    ws_status        = Signal(str)        # "connected" | "reconnecting" | "no_server" | "auth_failed"
    auth_failed      = Signal()


# ---------------------------------------------------------------------------
# Setup dialog (first run)
# ---------------------------------------------------------------------------

class SetupDialog(QDialog):
    """Shown on first launch when server URL or token is not configured."""

    _INPUT_STYLE = (
        "QLineEdit {"
        "  background-color: #313244; border: 1px solid #45475a;"
        "  border-radius: 6px; padding: 8px 10px;"
        "  color: #cdd6f4; font-size: 13px;"
        "}"
        "QLineEdit:focus { border-color: #89b4fa; }"
    )
    _LABEL_STYLE = "color: #a6adc8; font-size: 12px; margin-top: 4px;"

    def __init__(self, server_url: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("InstaSend — Server Setup")
        self.setFixedWidth(460)
        self.setStyleSheet("background-color: #1e1e2e; color: #cdd6f4;")

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(28, 28, 28, 28)

        title = QLabel("Server Setup")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #cdd6f4;")

        desc = QLabel(
            "Authentication failed. Enter the correct token for your server."
            if server_url else
            "Enter the details for your InstaSend relay server.\n"
            "Contact your server administrator for the token."
        )
        desc.setStyleSheet("color: #a6adc8; font-size: 13px; line-height: 1.5;")
        desc.setWordWrap(True)

        url_label = QLabel("Server URL")
        url_label.setStyleSheet(self._LABEL_STYLE)
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://your-server.example.com")
        self._url_input.setStyleSheet(self._INPUT_STYLE)

        token_label = QLabel("Token")
        token_label.setStyleSheet(self._LABEL_STYLE)
        self._token_input = QLineEdit()
        self._token_input.setPlaceholderText("Pre-shared secret")
        self._token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_input.setStyleSheet(self._INPUT_STYLE)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 8, 0, 0)

        skip_btn = QPushButton("Skip for now")
        skip_btn.setStyleSheet(
            "QPushButton { color: #a6adc8; background: transparent; border: none; font-size: 13px; }"
            "QPushButton:hover { color: #cdd6f4; }"
        )
        skip_btn.clicked.connect(self.reject)

        self._save_btn = QPushButton("Save && Connect")
        self._save_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: #89b4fa; color: #1e1e2e; border: none;"
            "  border-radius: 6px; padding: 8px 18px; font-size: 13px; font-weight: bold;"
            "}"
            "QPushButton:hover { background-color: #74c7ec; }"
            "QPushButton:disabled { background-color: #45475a; color: #585b70; }"
        )
        self._save_btn.clicked.connect(self._on_save)

        buttons.addWidget(skip_btn)
        buttons.addStretch()
        buttons.addWidget(self._save_btn)

        layout.addWidget(title)
        layout.addSpacing(4)
        layout.addWidget(desc)
        layout.addSpacing(8)
        layout.addWidget(url_label)
        layout.addWidget(self._url_input)
        layout.addWidget(token_label)
        layout.addWidget(self._token_input)
        layout.addLayout(buttons)

        if server_url:
            self._url_input.setText(server_url)

        self._url_input.textChanged.connect(self._update_save_btn)
        self._token_input.textChanged.connect(self._update_save_btn)
        self._update_save_btn()

    def _update_save_btn(self):
        self._save_btn.setEnabled(
            bool(self._url_input.text().strip()) and
            bool(self._token_input.text().strip())
        )

    def _on_save(self):
        if self._url_input.text().strip() and self._token_input.text().strip():
            self.accept()

    def server_url(self) -> str:
        return self._url_input.text().strip()

    def token(self) -> str:
        return self._token_input.text().strip()


# ---------------------------------------------------------------------------
# Connection status bar
# ---------------------------------------------------------------------------

class StatusBar(QWidget):
    _DOT_COLORS = {
        "connected":    "#a6e3a1",  # green
        "reconnecting": "#f9e2af",  # yellow
        "no_server":    "#585b70",  # muted grey
        "auth_failed":  "#f38ba8",  # red
    }
    _LABELS = {
        "connected":    "Connected",
        "reconnecting": "Reconnecting…",
        "no_server":    "No server configured",
        "auth_failed":  "Authentication failed",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._dot = QLabel()
        self._dot.setFixedSize(8, 8)

        self._label = QLabel()
        self._label.setStyleSheet("color: #a6adc8; font-size: 11px;")

        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        layout.addStretch()

        self.set_status("no_server")

    def set_status(self, status: str):
        color = self._DOT_COLORS.get(status, self._DOT_COLORS["no_server"])
        self._dot.setStyleSheet(
            f"background-color: {color}; border-radius: 4px;"
        )
        self._label.setText(self._LABELS.get(status, status))


# ---------------------------------------------------------------------------
# Share row widget
# ---------------------------------------------------------------------------

class ShareRow(QFrame):
    remove_requested = Signal(str)  # local_id

    def __init__(self, share: dict, url: str | None, parent=None):
        super().__init__(parent)
        self._local_id = share["local_id"]
        self._url: str | None = None
        self._setup_ui(share["filename"], url, share.get("downloads", 0))

    def _setup_ui(self, filename: str, url: str | None, downloads: int):
        self.setStyleSheet(
            "QFrame { background-color: #313244; border-radius: 8px; border: none; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)

        self.filename_label = QLabel(filename)
        self.filename_label.setStyleSheet(
            "color: #cdd6f4; font-size: 13px; font-weight: bold;"
        )
        self.filename_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        self.count_label = QLabel(self._format_count(downloads))
        self.count_label.setStyleSheet("color: #cba6f7; font-size: 12px;")

        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(22, 22)
        remove_btn.setStyleSheet(
            "QPushButton { color: #f38ba8; background: transparent; border: none; font-size: 13px; }"
            "QPushButton:hover { color: #ff6b6b; }"
        )
        remove_btn.clicked.connect(lambda: self.remove_requested.emit(self._local_id))

        top.addWidget(self.filename_label)
        top.addWidget(self.count_label)
        top.addWidget(remove_btn)

        self.url_label = QLabel()
        self.url_label.mousePressEvent = self._copy_url
        self.url_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout.addLayout(top)
        layout.addWidget(self.url_label)

        self.set_url(url)

    def set_url(self, url: str | None):
        self._url = url
        if url:
            self.url_label.setText(url)
            self.url_label.setStyleSheet(
                "color: #89b4fa; font-size: 11px; text-decoration: underline;"
            )
            self.url_label.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.url_label.setText("Registering…")
            self.url_label.setStyleSheet("color: #585b70; font-size: 11px;")
            self.url_label.setCursor(Qt.CursorShape.ArrowCursor)

    @staticmethod
    def _format_count(count: int) -> str:
        if count == 0:
            return ""
        return "1 download" if count == 1 else f"{count} downloads"

    def _copy_url(self, _event=None):
        if self._url:
            QApplication.clipboard().setText(self._url)

    def update_count(self, count: int):
        self.count_label.setText(self._format_count(count))


# ---------------------------------------------------------------------------
# Share list (scrollable)
# ---------------------------------------------------------------------------

class ShareList(QWidget):
    def __init__(self, on_remove, parent=None):
        super().__init__(parent)
        self._on_remove = on_remove
        self._rows: dict[str, ShareRow] = {}  # local_id → ShareRow
        self._setup_ui()

    def _setup_ui(self):
        self.setStyleSheet("background-color: #1e1e2e;")
        self._layout = QVBoxLayout(self)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

        self._empty_label = QLabel("No active shares")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: #585b70; font-size: 13px;")
        self._layout.addWidget(self._empty_label)

    def add_row(self, share: dict, url: str | None = None):
        self._empty_label.setVisible(False)
        row = ShareRow(share, url)
        row.remove_requested.connect(self._on_remove)
        self._rows[share["local_id"]] = row
        self._layout.addWidget(row)

    def remove_row(self, local_id: str):
        row = self._rows.pop(local_id, None)
        if row:
            self._layout.removeWidget(row)
            row.deleteLater()
        if not self._rows:
            self._empty_label.setVisible(True)

    def update_count(self, local_id: str, count: int):
        row = self._rows.get(local_id)
        if row:
            row.update_count(count)

    def set_url(self, local_id: str, url: str | None):
        row = self._rows.get(local_id)
        if row:
            row.set_url(url)


# ---------------------------------------------------------------------------
# Drop zone
# ---------------------------------------------------------------------------

class DropZone(QWidget):
    """Visual indicator only — DnD is handled by the parent window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(90)
        self.set_idle()

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        label = QLabel("Drop a file anywhere to share")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #cdd6f4; font-size: 15px; border: none;")
        layout.addWidget(label)

    def set_idle(self):
        self.setStyleSheet(
            "background-color: #1e1e2e; border: 2px dashed #45475a; border-radius: 10px;"
        )

    def set_hover(self):
        self.setStyleSheet(
            "background-color: #181825; border: 2px dashed #89b4fa; border-radius: 10px;"
        )


# ---------------------------------------------------------------------------
# Popup window
# ---------------------------------------------------------------------------

class PopupWindow(QWidget):
    file_dropped = Signal(object)  # pathlib.Path

    def __init__(self, drop_zone: DropZone, share_list: ShareList, version: str):
        super().__init__()
        self._drop_zone = drop_zone
        self.setAcceptDrops(True)
        self.setWindowTitle("InstaSend")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setMinimumSize(500, 400)
        self.resize(500, 500)

        self.setStyleSheet("background-color: #1e1e2e;")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(drop_zone)

        scroll = QScrollArea()
        scroll.setWidget(share_list)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background-color: #1e1e2e; }")
        outer.addWidget(scroll)

        # Footer: connection status (left) + version (right)
        self._status_bar = StatusBar()
        version_label = QLabel(version)
        version_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        version_label.setStyleSheet("color: #45475a; font-size: 10px;")

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self._status_bar)
        footer.addWidget(version_label)
        outer.addLayout(footer)

    def set_status(self, status: str):
        self._status_bar.set_status(status)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and urls[0].isLocalFile():
                event.acceptProposedAction()
                self._drop_zone.set_hover()
                return
        event.ignore()

    def dragLeaveEvent(self, _event):
        self._drop_zone.set_idle()

    def dropEvent(self, event: QDropEvent):
        self._drop_zone.set_idle()
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            if path.is_file():
                self.file_dropped.emit(path)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class InstaSend:
    def __init__(self, config: dict):
        self.config = config
        self._signals = _Signals()
        self._ws_client: WsClient | None = None

        self._manager = ShareManager(
            shares_path=SHARES_PATH,
            on_download=lambda lid, c: self._signals.download_updated.emit(lid, c),
            on_assigned=lambda lid, h: self._signals.share_assigned.emit(lid, h),
        )

        self.qt_app = QApplication(sys.argv)
        self.qt_app.setQuitOnLastWindowClosed(False)

        _hide_dock_icon()

        self._drop_zone = DropZone()
        self._share_list = ShareList(on_remove=self._on_remove)
        self._popup = PopupWindow(self._drop_zone, self._share_list, _get_version())

        self._popup.file_dropped.connect(self._on_file_dropped)
        self._signals.download_updated.connect(self._share_list.update_count)
        self._signals.share_assigned.connect(self._on_share_assigned)
        self._signals.ws_status.connect(self._popup.set_status)
        self._signals.auth_failed.connect(self._on_auth_failed)

        self._tray = QSystemTrayIcon(_make_tray_icon(), self.qt_app)
        self._tray.setToolTip("InstaSend")
        self._tray.activated.connect(self._on_tray_activated)

        self._tray_menu = QMenu()
        show_action = QAction("Show", self.qt_app)
        show_action.triggered.connect(self._show_popup)
        quit_action = QAction("Quit", self.qt_app)
        quit_action.triggered.connect(self.qt_app.quit)
        self._tray_menu.addAction(show_action)
        self._tray_menu.addSeparator()
        self._tray_menu.addAction(quit_action)
        # Do not call setContextMenu — on macOS it intercepts left clicks unpredictably.
        # Context clicks are handled manually in _on_tray_activated.

    def _maybe_create_ws_client(self):
        """Create WsClient if server URL and token are both configured."""
        server_url = self.config.get("server_url", "").strip()
        token = self.config.get("token", "").strip()
        if server_url and token and self._ws_client is None:
            self._ws_client = WsClient(
                url=_to_ws_url(server_url),
                token=token,
                client_id=_get_or_create_client_id(),
                get_shares=self._manager.all,
                get_share_by_hash=self._manager.get_by_hash,
                on_assigned=self._manager.assign,
                on_download=self._manager.record_download,
                on_status=self._on_ws_status,
                on_auth_failed=lambda: self._signals.auth_failed.emit(),
            )

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self._popup.isVisible():
                self._popup.hide()
            else:
                self._show_popup()
        elif reason == QSystemTrayIcon.ActivationReason.Context:
            self._tray_menu.popup(QCursor.pos())

    def _show_popup(self):
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry()
        pw = self._popup.width()
        ph = self._popup.height()

        tray_geom = self._tray.geometry()
        if tray_geom.isValid():
            x = max(avail.left(), min(tray_geom.center().x() - pw // 2, avail.right() - pw))
            y = tray_geom.bottom() + 4
        else:
            x = avail.right() - pw - 10
            y = avail.top() + 30

        if y + ph > avail.bottom():
            y = avail.bottom() - ph

        self._popup.move(x, y)
        self._popup.show()
        self._popup.raise_()
        self._popup.activateWindow()

    def _build_url(self, server_hash: str) -> str:
        base = (
            self.config.get("public_url", "")
            or self.config.get("server_url", "")
        ).strip().rstrip("/")
        return f"{base}/{server_hash}" if base else server_hash

    def _on_share_assigned(self, local_id: str, server_hash: str):
        self._share_list.set_url(local_id, self._build_url(server_hash))

    def _on_file_dropped(self, file_path: Path):
        share = self._manager.add(file_path)
        self._share_list.add_row(share, url=None)
        if self._ws_client:
            self._ws_client.send_add(share["filename"])

    def _on_remove(self, local_id: str):
        server_hash = self._manager.remove(local_id)
        if server_hash and self._ws_client:
            self._ws_client.send_remove(server_hash)
        self._share_list.remove_row(local_id)

    def _on_ws_status(self, status: str):
        self._signals.ws_status.emit(status)

    def _on_auth_failed(self):
        self._ws_client = None  # the loop has already stopped
        self._popup.set_status("auth_failed")
        self._show_popup()

        dialog = SetupDialog(server_url=self.config.get("server_url", ""))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config["server_url"] = dialog.server_url()
            self.config["token"] = dialog.token()
            _save_config(self.config)
            self._maybe_create_ws_client()
            if self._ws_client:
                self._ws_client.start()

    def run(self) -> int:
        self._manager.load()

        # First-run setup: prompt for server URL and token if not configured
        if not self.config.get("server_url") or not self.config.get("token"):
            dialog = SetupDialog()
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.config["server_url"] = dialog.server_url()
                self.config["token"] = dialog.token()
                _save_config(self.config)

        self._maybe_create_ws_client()

        for share in self._manager.all():
            self._share_list.add_row(share, url=None)

        if self._ws_client:
            self._ws_client.start()
        else:
            self._popup.set_status("no_server")

        self._tray.show()
        self._show_popup()

        return self.qt_app.exec()


def main():
    config = load_config()
    sys.exit(InstaSend(config).run())


if __name__ == "__main__":
    main()
