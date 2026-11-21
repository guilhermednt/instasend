"""Shared fixtures: boots the real relay server in a subprocess.

The test process imports the desktop's ``ws_client`` / ``shares`` modules (they
have no Qt dependency), so these tests exercise the actual desktop client
against the actual server over a real WebSocket.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "server"
DESKTOP_DIR = ROOT / "desktop"

sys.path.insert(0, str(DESKTOP_DIR))

TOKEN = "t" * 64


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    """Start a fresh server (fresh DB, fresh per-IP limits) for each test."""
    port = _free_port()
    env = {**os.environ, "TOKEN": TOKEN, "DB_PATH": str(tmp_path / "shares.db")}
    log = open(tmp_path / "server.log", "w+")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--ws-per-message-deflate", "false"],
        cwd=SERVER_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            if proc.poll() is not None:
                log.seek(0)
                pytest.fail(f"server exited early:\n{log.read()}")
            try:
                urllib.request.urlopen(base + "/healthcheck-probe", timeout=1)
                break
            except urllib.error.HTTPError:
                break  # any HTTP response means it's up
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("server did not start within 20s")
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.seek(0)
        print(log.read())  # shown by pytest only on failure
        log.close()
