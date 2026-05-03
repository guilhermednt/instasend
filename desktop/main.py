import json
import platform
import socket
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent  # noqa: F401 (used in MainWindow)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from file_server import FileServer
from shares import ShareManager

DEFAULT_CONFIG = {"file_server_port": 8081, "server_url": ""}


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        if platform.system() == "Darwin" and exe.parent.name == "MacOS":
            return exe.parent.parent.parent.parent
        return exe.parent
    return Path(__file__).parent


CONFIG_PATH = get_app_dir() / "config.json"
SHARES_PATH = get_app_dir() / "shares.json"


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


# ---------------------------------------------------------------------------
# Cross-thread signals
# ---------------------------------------------------------------------------

class _Signals(QObject):
    download_updated = Signal(str, int)  # hash, count


# ---------------------------------------------------------------------------
# Share row widget
# ---------------------------------------------------------------------------

class ShareRow(QFrame):
    remove_requested = Signal(str)  # hash

    def __init__(self, share: dict, url: str, parent=None):
        super().__init__(parent)
        self._hash = share["hash"]
        self._setup_ui(share["filename"], url, share.get("downloads", 0))

    def _setup_ui(self, filename: str, url: str, downloads: int):
        self.setStyleSheet(
            "QFrame { background-color: #313244; border-radius: 8px; border: none; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # Top row: filename + download count + remove button
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

        # Bottom row: URL
        self.url_label = QLabel(url)
        self.url_label.setStyleSheet(
            "color: #89b4fa; font-size: 11px; text-decoration: underline;"
        )
        self.url_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.url_label.mousePressEvent = self._copy_url

        layout.addLayout(top)
        layout.addWidget(self.url_label)

    @staticmethod
    def _format_count(count: int) -> str:
        if count == 0:
            return ""
        return "1 download" if count == 1 else f"{count} downloads"

    def _copy_url(self, _event=None):
        url = self.url_label.text()
        if url:
            QApplication.clipboard().setText(url)

    def update_count(self, count: int):
        self.count_label.setText(self._format_count(count))


# ---------------------------------------------------------------------------
# Share list (scrollable)
# ---------------------------------------------------------------------------

class ShareList(QWidget):
    def __init__(self, on_remove, parent=None):
        super().__init__(parent)
        self._on_remove = on_remove
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


class MainWindow(QMainWindow):
    file_dropped = Signal(object)  # pathlib.Path

    def __init__(self, drop_zone: DropZone):
        super().__init__()
        self._drop_zone = drop_zone
        self.setAcceptDrops(True)

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
        )
        self._file_server = FileServer(
            port=config["file_server_port"],
            share_manager=self._manager,
        )

        self.qt_app = QApplication(sys.argv)

        self._drop_zone = DropZone()
        window = MainWindow(self._drop_zone)
        window.setWindowTitle("InstaSend")
        window.setMinimumSize(500, 400)
        window.resize(500, 500)
        window.file_dropped.connect(self._on_file_dropped)

        central = QWidget()
        central.setStyleSheet("background-color: #1e1e2e;")
        window.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        outer.addWidget(self._drop_zone)

        self._share_list = ShareList(on_remove=self._on_remove)
        scroll = QScrollArea()
        scroll.setWidget(self._share_list)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background-color: #1e1e2e; }"
        )
        outer.addWidget(scroll)

        self._signals.download_updated.connect(self._share_list.update_count)
        self.window = window

    def _build_url(self, hash_val: str) -> str:
        server_url = self.config.get("server_url", "").strip().rstrip("/")
        if server_url:
            return f"{server_url}/{hash_val}"
        return f"http://{get_local_ip()}:{self.config['file_server_port']}/{hash_val}"

    def _on_file_dropped(self, file_path: Path):
        share = self._manager.add(file_path)
        self._share_list.add_row(share, self._build_url(share["hash"]))

    def _on_remove(self, hash_val: str):
        self._manager.remove(hash_val)
        self._share_list.remove_row(hash_val)

    def run(self) -> int:
        self._manager.load()
        self._file_server.start()
        threading.Thread(target=self._manager.startup_sync, daemon=True).start()
        self._share_list.populate(self._manager.all(), url_fn=self._build_url)

        self.window.show()
        code = self.qt_app.exec()
        self._file_server.stop()
        return code


def main():
    config = load_config()
    sys.exit(InstaSend(config).run())


if __name__ == "__main__":
    main()
