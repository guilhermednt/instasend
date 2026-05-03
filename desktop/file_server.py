"""
Lightweight HTTP server that serves shared files by hash.

GET  /<hash>  →  forced file download (Content-Disposition: attachment)
HEAD /<hash>  →  same headers, no body (used by the Server to probe availability)
"""

import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FileServer:
    def __init__(self, port: int, share_manager):
        self._port = port
        self._share_manager = share_manager
        self._server: ThreadingHTTPServer | None = None

    def start(self):
        manager = self._share_manager

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self._respond(head_only=True)

            def do_GET(self):
                self._respond(head_only=False)

            def _respond(self, head_only: bool):
                hash_val = self.path.lstrip("/")
                share = manager.get(hash_val)

                if share is None:
                    self.send_response(404)
                    self.end_headers()
                    return

                file_path = Path(share["path"])
                if not file_path.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return

                file_size = file_path.stat().st_size
                safe_name = share["filename"].replace('"', '\\"')

                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{safe_name}"',
                )
                self.send_header("Content-Length", str(file_size))
                self.end_headers()

                if not head_only:
                    with open(file_path, "rb") as f:
                        shutil.copyfileobj(f, self.wfile, length=1024 * 1024)
                    manager.record_download(hash_val)

            def log_message(self, *_):
                pass  # suppress default stderr logging

        self._server = ThreadingHTTPServer(("", self._port), Handler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None
