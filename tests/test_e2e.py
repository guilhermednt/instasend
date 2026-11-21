"""End-to-end tests: real desktop WsClient + ShareManager against the real server."""

import os
import threading
import urllib.error
import urllib.request
import uuid

import pytest

from conftest import DESKTOP_DIR, SERVER_DIR, TOKEN
from shares import ShareManager
from ws_client import CHUNK_SIZE, WsClient

TIMEOUT = 15


class Desktop:
    """Minimal stand-in for the Qt app: wires ShareManager to WsClient."""

    def __init__(self, base_url, tmp_path, token=TOKEN, client_id=None, shares_file=None):
        self.client_id = client_id or uuid.uuid4().hex
        self.assigned = threading.Event()
        self.downloaded = threading.Event()
        self.auth_failed = threading.Event()
        self.manager = ShareManager(
            shares_file or tmp_path / f"shares-{uuid.uuid4().hex}.json",
            on_assigned=lambda _lid, _h: self.assigned.set(),
            on_download=lambda _lid, _n: self.downloaded.set(),
        )
        self.manager.load()
        self.client = WsClient(
            url=base_url.replace("http://", "ws://") + "/ws",
            token=token,
            client_id=self.client_id,
            get_shares=self.manager.all,
            get_share_by_hash=self.manager.get_by_hash,
            on_assigned=self.manager.assign,
            on_download=self.manager.record_download,
            on_status=lambda _s: None,
            on_auth_failed=self.auth_failed.set,
        )

    def start(self):
        self.client.start()
        return self

    def hash_of(self, filename):
        return next(s["hash"] for s in self.manager.all() if s["filename"] == filename)


def _get(url):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return r.status, r.headers, r.read()


def test_protocol_copies_in_sync():
    assert (SERVER_DIR / "protocol.py").read_bytes() == (DESKTOP_DIR / "protocol.py").read_bytes(), \
        "server/protocol.py and desktop/protocol.py must be identical"


def test_unknown_hash_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server + "/doesnotexist")
    assert exc.value.code == 404


def test_share_and_download_roundtrip(server, tmp_path):
    # Multi-chunk payload to exercise binary framing and credit flow control.
    payload = os.urandom(CHUNK_SIZE * 2 + 12345)
    f = tmp_path / "hello wörld.bin"
    f.write_bytes(payload)

    d = Desktop(server, tmp_path)
    d.manager.add(f)
    d.start()
    assert d.assigned.wait(TIMEOUT), "server never assigned a hash"

    h = d.hash_of(f.name)
    assert h and len(h) == 8

    status, headers, body = _get(f"{server}/{h}")
    assert status == 200
    assert body == payload
    assert "attachment" in headers["Content-Disposition"]
    assert d.downloaded.wait(TIMEOUT)
    assert d.manager.get_by_hash(h)["downloads"] == 1


def test_missing_file_returns_404(server, tmp_path):
    f = tmp_path / "gone.txt"
    f.write_text("x")
    d = Desktop(server, tmp_path)
    d.manager.add(f)
    d.start()
    assert d.assigned.wait(TIMEOUT)
    h = d.hash_of(f.name)
    f.unlink()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{server}/{h}")
    assert exc.value.code == 404


def test_reconnect_keeps_hash_for_same_client(server, tmp_path):
    f = tmp_path / "keep.txt"
    f.write_text("keep me")
    shares_file = tmp_path / "shares.json"

    first = Desktop(server, tmp_path, shares_file=shares_file)
    first.manager.add(f)
    first.start()
    assert first.assigned.wait(TIMEOUT)
    h = first.hash_of(f.name)

    # A second process with the same identity and persisted shares reclaims the hash.
    second = Desktop(server, tmp_path, client_id=first.client_id, shares_file=shares_file).start()
    assert second.assigned.wait(TIMEOUT)
    assert second.hash_of(f.name) == h


def test_other_client_cannot_claim_hash(server, tmp_path):
    f = tmp_path / "mine.txt"
    f.write_text("mine")
    shares_file = tmp_path / "shares.json"

    owner = Desktop(server, tmp_path, shares_file=shares_file)
    owner.manager.add(f)
    owner.start()
    assert owner.assigned.wait(TIMEOUT)
    h = owner.hash_of(f.name)

    thief = Desktop(server, tmp_path, shares_file=shares_file).start()  # new client_id
    assert thief.assigned.wait(TIMEOUT)
    new_h = thief.hash_of(f.name)
    assert new_h != h


def test_wrong_token_is_rejected(server, tmp_path):
    d = Desktop(server, tmp_path, token="x" * 64).start()
    assert d.auth_failed.wait(TIMEOUT), "client did not detect bad token"

