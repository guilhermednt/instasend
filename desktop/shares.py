"""
Share management: persistence and local state.

shares.json schema:
[
  {
    "local_id":  "uuid hex",        -- stable local identifier
    "hash":      "AbCd1234",        -- server-assigned hash; null if not yet assigned
    "path":      "/absolute/path",
    "filename":  "display-name.ext",
    "downloads": 0
  }
]

The file is automatically migrated from the old dict-keyed format on first load.
"""

import json
import threading
import uuid
from pathlib import Path


class ShareManager:
    def __init__(
        self,
        shares_path: Path,
        on_download=None,   # callable(local_id: str, count: int) | None
        on_assigned=None,   # callable(local_id: str, server_hash: str) | None
    ):
        self._path = shares_path
        self._on_download = on_download
        self._on_assigned = on_assigned
        self._shares: dict[str, dict] = {}     # local_id → share
        self._hash_index: dict[str, str] = {}  # server_hash → local_id
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self):
        """Load shares from disk. Drops entries whose file no longer exists."""
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            return
        with self._lock:
            # Migrate old format: {hash: {path, filename, downloads}} → list
            if isinstance(data, dict):
                data = [
                    {**v, "local_id": uuid.uuid4().hex, "hash": k}
                    for k, v in data.items()
                ]
            for share in data:
                if not Path(share.get("path", "")).is_file():
                    continue
                lid = share.get("local_id") or uuid.uuid4().hex
                share["local_id"] = lid
                share.setdefault("hash", None)
                share.setdefault("downloads", 0)
                self._shares[lid] = share
                if share.get("hash"):
                    self._hash_index[share["hash"]] = lid
            self._save_locked()

    def _save_locked(self):
        """Write to disk. Must be called with _lock held."""
        try:
            self._path.write_text(
                json.dumps(list(self._shares.values()), indent=2)
            )
            self._path.chmod(0o600)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Share operations
    # ------------------------------------------------------------------

    def add(self, file_path: Path) -> dict:
        """
        Add a new file share locally (server hash not yet assigned).
        Returns the share dict with local_id set and hash=None.
        """
        lid = uuid.uuid4().hex
        share = {
            "local_id":  lid,
            "hash":      None,
            "path":      str(file_path.resolve()),
            "filename":  file_path.name,
            "downloads": 0,
        }
        with self._lock:
            self._shares[lid] = share
            self._save_locked()
        return share

    def assign(self, filename: str, server_hash: str):
        """
        Called when the server sends an Assigned frame.

        Fast path: if the hash is already in the index the server echoed back a
        hash we already own (valid reconnect) — just fire the callback.

        Otherwise: find the first pending share (hash=None) with this filename
        (new assignment, consumed in insertion order so duplicate filenames are
        handled correctly).  If no pending share exists, this is a reconnect
        reassignment — update the first matching share's hash.
        """
        local_id = None
        with self._lock:
            # Reconnect confirmation: server echoed a hash we already have.
            if server_hash in self._hash_index:
                local_id = self._hash_index[server_hash]
            else:
                # New assignment: consume the first pending share with this filename.
                for lid, share in self._shares.items():
                    if share["filename"] == filename and share.get("hash") is None:
                        share["hash"] = server_hash
                        self._hash_index[server_hash] = lid
                        local_id = lid
                        self._save_locked()
                        break
                else:
                    # Reconnect reassignment: server retired the old hash.
                    for lid, share in self._shares.items():
                        if share["filename"] == filename:
                            old_hash = share.get("hash")
                            if old_hash:
                                self._hash_index.pop(old_hash, None)
                            share["hash"] = server_hash
                            self._hash_index[server_hash] = lid
                            local_id = lid
                            self._save_locked()
                            break

        if local_id and self._on_assigned:
            self._on_assigned(local_id, server_hash)

    def remove(self, local_id: str) -> str | None:
        """
        Remove a share locally.
        Returns the server-assigned hash (to send a Remove WS frame),
        or None if the share had not yet been assigned a hash.
        """
        with self._lock:
            share = self._shares.pop(local_id, None)
            if share is None:
                return None
            h = share.get("hash")
            if h:
                self._hash_index.pop(h, None)
            self._save_locked()
            return h

    def get_by_hash(self, hash_val: str) -> dict | None:
        with self._lock:
            lid = self._hash_index.get(hash_val)
            return self._shares.get(lid) if lid else None

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._shares.values())

    def record_download(self, hash_val: str):
        local_id = None
        count = None
        with self._lock:
            lid = self._hash_index.get(hash_val)
            if lid and lid in self._shares:
                self._shares[lid]["downloads"] += 1
                count = self._shares[lid]["downloads"]
                local_id = lid
                self._save_locked()
        if count is not None and self._on_download:
            self._on_download(local_id, count)
