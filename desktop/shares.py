"""
Share management: persistence, hash generation, and Server API sync.

shares.json schema:
{
  "<hash>": {
    "path":      "/absolute/path/to/file",
    "filename":  "display-name.ext",
    "downloads": 0
  }
}
"""

import json
import secrets
import string
import threading
from pathlib import Path

import requests

HASH_CHARS = string.ascii_letters + string.digits  # A-Za-z0-9
HASH_LEN = 6


def _generate_hash() -> str:
    return "".join(secrets.choice(HASH_CHARS) for _ in range(HASH_LEN))


class ShareManager:
    def __init__(
        self,
        shares_path: Path,
        server_url: str,
        file_server_port: int,
        on_download=None,    # callable(hash: str, count: int) | None
        on_registered=None,  # callable(hash: str, success: bool) | None
    ):
        self._path = shares_path
        self._server_url = server_url.rstrip("/") if server_url else ""
        self._port = file_server_port
        self._on_download = on_download
        self._on_registered = on_registered
        self._shares: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self):
        """Load shares from disk. Missing or unreadable files are dropped."""
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            return
        with self._lock:
            for h, share in data.items():
                if Path(share.get("path", "")).is_file():
                    self._shares[h] = share
            # Persist back in case any stale entries were dropped
            self._save_locked()

    def _save_locked(self):
        """Write current shares to disk. Must be called with _lock held."""
        try:
            self._path.write_text(json.dumps(self._shares, indent=2))
        except Exception:
            pass

    def _save(self):
        with self._lock:
            self._save_locked()

    # ------------------------------------------------------------------
    # Share operations
    # ------------------------------------------------------------------

    def add(self, file_path: Path) -> dict:
        """Register a new file. Returns the new share dict (includes hash)."""
        with self._lock:
            # Avoid hash collisions
            h = _generate_hash()
            while h in self._shares:
                h = _generate_hash()
            share = {
                "hash":      h,
                "path":      str(file_path.resolve()),
                "filename":  file_path.name,
                "downloads": 0,
            }
            self._shares[h] = share
            self._save_locked()

        threading.Thread(
            target=self._server_register, args=(h, share["filename"]), daemon=True
        ).start()
        return share

    def remove(self, hash_val: str):
        """Remove a share locally and notify the Server."""
        with self._lock:
            self._shares.pop(hash_val, None)
            self._save_locked()
        threading.Thread(
            target=self._server_unregister, args=(hash_val,), daemon=True
        ).start()

    def get(self, hash_val: str) -> dict | None:
        with self._lock:
            return self._shares.get(hash_val)

    def all(self) -> list[dict]:
        with self._lock:
            return [{"hash": h, **s} for h, s in self._shares.items()]

    def record_download(self, hash_val: str):
        count = None
        with self._lock:
            if hash_val in self._shares:
                self._shares[hash_val]["downloads"] += 1
                count = self._shares[hash_val]["downloads"]
                self._save_locked()
        if count is not None and self._on_download:
            self._on_download(hash_val, count)

    # ------------------------------------------------------------------
    # Server API (D3)
    # ------------------------------------------------------------------

    def retry_register(self, hash_val: str):
        """Re-attempt server registration for a single share."""
        with self._lock:
            share = self._shares.get(hash_val)
        if share:
            threading.Thread(
                target=self._server_register, args=(hash_val, share["filename"]), daemon=True
            ).start()

    def startup_sync(self):
        """Re-register all persisted shares with the Server after a restart."""
        with self._lock:
            items = list(self._shares.items())
        for h, share in items:
            self._server_register(h, share["filename"])

    def _server_register(self, hash_val: str, filename: str):
        if not self._server_url:
            return
        try:
            r = requests.post(
                f"{self._server_url}/shares",
                json={"hash": hash_val, "port": self._port, "filename": filename},
                timeout=5,
            )
            success = r.status_code in (200, 201)
        except Exception:
            success = False
        if self._on_registered:
            self._on_registered(hash_val, success)

    def _server_unregister(self, hash_val: str):
        if not self._server_url:
            return
        try:
            requests.delete(
                f"{self._server_url}/shares/{hash_val}",
                timeout=5,
            )
        except Exception:
            pass
