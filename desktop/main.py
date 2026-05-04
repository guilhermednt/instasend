import json
import platform
import socket
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QPainter,
    QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from file_server import FileServer
from shares import ShareManager

DEFAULT_CONFIG = {"file_server_port": 8081, "server_url": "", "public_url": ""}


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


APP_DIR = Path.home() / ".instasend"
APP_DIR.mkdir(exist_ok=True)

CONFIG_PATH = APP_DIR / "config.json"
SHARES_PATH = APP_DIR / "shares.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


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
    download_updated = Signal(str, int)   # hash, count
    share_registered = Signal(str, bool)  # hash, success


# ---------------------------------------------------------------------------
# Share row widget
# ---------------------------------------------------------------------------

class ShareRow(QFrame):
    remove_requested = Signal(str)  # hash
    retry_requested  = Signal(str)  # hash

    def __init__(self, share: dict, url: str | None, parent=None):
        super().__init__(parent)
        self._hash = share["hash"]
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
        remove_btn.clicked.connect(lambda: self.remove_requested.emit(self._hash))

        top.addWidget(self.filename_label)
        top.addWidget(self.count_label)
        top.addWidget(remove_btn)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)

        self.url_label = QLabel()
        self.url_label.mousePressEvent = self._copy_url
        self.url_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.retry_btn = QPushButton("Retry")
        self.retry_btn.setFixedHeight(18)
        self.retry_btn.setStyleSheet(
            "QPushButton { color: #cba6f7; background: transparent; border: none; font-size: 11px; }"
            "QPushButton:hover { color: #d0bcff; }"
        )
        self.retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry_btn.clicked.connect(lambda: self.retry_requested.emit(self._hash))
        self.retry_btn.hide()

        bottom.addWidget(self.url_label)
        bottom.addWidget(self.retry_btn)

        layout.addLayout(top)
        layout.addLayout(bottom)

        self.set_url(url)

    def set_url(self, url: str | None, failed: bool = False):
        self._url = url
        if url:
            self.url_label.setText(url)
            self.url_label.setStyleSheet(
                "color: #89b4fa; font-size: 11px; text-decoration: underline;"
            )
            self.url_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.retry_btn.hide()
        elif failed:
            self.url_label.setText("Could not reach server")
            self.url_label.setStyleSheet("color: #f38ba8; font-size: 11px;")
            self.url_label.setCursor(Qt.CursorShape.ArrowCursor)
            self.retry_btn.show()
        else:
            self.url_label.setText("Registering…")
            self.url_label.setStyleSheet("color: #585b70; font-size: 11px;")
            self.url_label.setCursor(Qt.CursorShape.ArrowCursor)
            self.retry_btn.hide()

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
    def __init__(self, on_remove, on_retry, parent=None):
        super().__init__(parent)
        self._on_remove = on_remove
        self._on_retry = on_retry
        self._rows: dict[str, ShareRow] = {}
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

    def add_row(self, share: dict, url: str):
        self._empty_label.setVisible(False)
        row = ShareRow(share, url)
        row.remove_requested.connect(self._on_remove)
        row.retry_requested.connect(self._on_retry)
        self._rows[share["hash"]] = row
        self._layout.addWidget(row)

    def remove_row(self, hash_val: str):
        row = self._rows.pop(hash_val, None)
        if row:
            self._layout.removeWidget(row)
            row.deleteLater()
        if not self._rows:
            self._empty_label.setVisible(True)

    def update_count(self, hash_val: str, count: int):
        row = self._rows.get(hash_val)
        if row:
            row.update_count(count)

    def set_url(self, hash_val: str, url: str | None, failed: bool = False):
        row = self._rows.get(hash_val)
        if row:
            row.set_url(url, failed=failed)

    def populate(self, shares: list[dict], url_fn):
        for share in shares:
            self.add_row(share, url_fn(share["hash"]))


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

        version_label = QLabel(version)
        version_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        version_label.setStyleSheet("color: #45475a; font-size: 10px;")
        outer.addWidget(version_label)

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

        self._manager = ShareManager(
            shares_path=SHARES_PATH,
            server_url=config.get("server_url", ""),
            file_server_port=config["file_server_port"],
            on_download=lambda h, c: self._signals.download_updated.emit(h, c),
            on_registered=lambda h, ok: self._signals.share_registered.emit(h, ok),
        )
        self._file_server = FileServer(
            port=config["file_server_port"],
            share_manager=self._manager,
        )

        self.qt_app = QApplication(sys.argv)
        self.qt_app.setQuitOnLastWindowClosed(False)

        _hide_dock_icon()

        self._drop_zone = DropZone()
        self._share_list = ShareList(on_remove=self._on_remove, on_retry=self._on_retry_registration)

        self._popup = PopupWindow(self._drop_zone, self._share_list, _get_version())
        self._popup.file_dropped.connect(self._on_file_dropped)
        self._signals.download_updated.connect(self._share_list.update_count)
        self._signals.share_registered.connect(self._on_share_registered)

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
        # Do not call setContextMenu — on macOS that causes the menu to intercept
        # left clicks unpredictably. Context clicks are handled in _on_tray_activated.

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self._popup.isVisible():
                self._popup.hide()
            else:
                self._show_popup()
        elif reason == QSystemTrayIcon.ActivationReason.Context:
            from PySide6.QtGui import QCursor
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

    def _build_url(self, hash_val: str) -> str:
        base = (
            self.config.get("public_url", "")
            or self.config.get("server_url", "")
        ).strip().rstrip("/")
        if base:
            return f"{base}/{hash_val}"
        return f"http://{get_local_ip()}:{self.config['file_server_port']}/{hash_val}"

    def _on_retry_registration(self, hash_val: str):
        self._share_list.set_url(hash_val, url=None, failed=False)  # back to "Registering…"
        self._manager.retry_register(hash_val)

    def _on_share_registered(self, hash_val: str, success: bool):
        url = self._build_url(hash_val) if success else None
        self._share_list.set_url(hash_val, url, failed=not success)

    def _on_file_dropped(self, file_path: Path):
        share = self._manager.add(file_path)
        has_server = bool(self.config.get("server_url", "").strip())
        url = None if has_server else self._build_url(share["hash"])
        self._share_list.add_row(share, url)

    def _on_remove(self, hash_val: str):
        self._manager.remove(hash_val)
        self._share_list.remove_row(hash_val)

    def run(self) -> int:
        self._manager.load()
        self._file_server.start()

        has_server = bool(self.config.get("server_url", "").strip())
        url_fn = (lambda h: None) if has_server else self._build_url
        self._share_list.populate(self._manager.all(), url_fn=url_fn)

        threading.Thread(target=self._manager.startup_sync, daemon=True).start()

        self._tray.show()
        self._show_popup()

        code = self.qt_app.exec()
        self._file_server.stop()
        return code


def main():
    config = load_config()
    sys.exit(InstaSend(config).run())


if __name__ == "__main__":
    main()
