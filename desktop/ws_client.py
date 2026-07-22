"""
Persistent WebSocket client — manages the connection to the InstaSend relay server.

On connect:
  1. Sends a Register frame with all shares that already have a server-assigned hash.
  2. Sends an Add frame for each pending share (hash not yet assigned).

Incoming frames handled:
  - assigned  → calls on_assigned(filename, server_hash)
  - request   → reads file from disk and streams it back as Response + binary chunks
                (multiple requests are served concurrently via asyncio tasks)

Reconnects automatically with exponential backoff (1 s → 2 s → … → 60 s).
"""

import asyncio
import collections
import hmac
import logging
import os
import threading
import time
from pathlib import Path

from websockets.asyncio.client import connect
import websockets.exceptions

import protocol


class _ServerAuthFailed(Exception):
    pass

CHUNK_SIZE = 256 * 1024  # 256 KB per chunk
PROGRESS_INTERVAL = 0.5  # seconds between speed updates sent to the UI
PROGRESS_WINDOW = 2.0    # seconds of send history averaged into each speed update

logger = logging.getLogger("instasend.ws")


class WsClient:
    def __init__(
        self,
        url: str,               # wss:// or ws:// — must include the /ws path
        token: str,
        client_id: str,
        get_shares,             # () → list[dict]  all shares including pending
        get_share_by_hash,      # (hash_val: str) → dict | None
        on_assigned,            # (filename: str, server_hash: str) → None
        on_download,            # (hash_val: str) → None
        on_status,              # (status: str) → None  "connected" | "reconnecting"
        on_auth_failed=None,    # () → None  called once on 4001, then loop stops
        on_progress=None,       # (hash_val: str, sent: int, total: int, speed: float | None) → None
                                 # speed is bytes/sec, averaged over a trailing window;
                                 # None means the transfer just ended (success or error).
    ):
        self._url = url
        self._token = token
        self._client_id = client_id
        self._get_shares = get_shares
        self._get_share_by_hash = get_share_by_hash
        self._on_assigned = on_assigned
        self._on_download = on_download
        self._on_status = on_status
        self._on_auth_failed = on_auth_failed
        self._on_progress = on_progress

        self._loop = asyncio.new_event_loop()
        self._ws = None  # set only while a session is live
        self._active_serves: dict[str, asyncio.Task] = {}  # request_id → serve task
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ws-client"
        )

    def start(self):
        self._thread.start()

    # ------------------------------------------------------------------
    # Internal — runs entirely inside the background event loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self):
        backoff = 1
        while True:
            try:
                self._on_status("reconnecting")
                async with connect(self._url, open_timeout=30, happy_eyeballs_delay=0.25) as ws:
                    self._ws = ws
                    backoff = 1
                    try:
                        await self._session(ws)
                    finally:
                        self._ws = None
            except _ServerAuthFailed:
                return  # on_auth_failed already called inside _session
            except websockets.exceptions.ConnectionClosedError as exc:
                if exc.code == 4001:
                    logger.error("ws auth rejected — bad token")
                    if self._on_auth_failed:
                        self._on_auth_failed()
                    return  # stop the loop; caller must create a new WsClient
                elif exc.code == 4003:
                    logger.error("ws rejected: protocol version mismatch — update the app")
                    backoff = 120
                else:
                    logger.warning("ws closed (code %d), retry in %ds", exc.code, backoff)
            except OSError as exc:
                logger.warning("ws connect failed: %s, retry in %ds", exc, backoff)
            except Exception as exc:
                logger.warning("ws error: %s, retry in %ds", exc, backoff)
            finally:
                self._ws = None
            self._on_status("reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _session(self, ws):
        """One connected session: mutual auth, register, then handle frames."""
        # Receive and verify the server's challenge.
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("ws challenge timed out")
            return

        if not isinstance(raw, str):
            logger.warning("ws expected text challenge, got binary frame")
            return

        try:
            challenge = protocol.decode(raw)
        except ValueError as exc:
            logger.warning("ws malformed challenge: %s", exc)
            return

        if not isinstance(challenge, protocol.Challenge):
            logger.warning("ws expected challenge, got %s", type(challenge).__name__)
            return

        # Verify server knows the token — reject rogue servers / MitM.
        expected_server = protocol.auth_digest(self._token, "server:" + challenge.nonce)
        if not hmac.compare_digest(challenge.server_digest, expected_server):
            logger.error("ws server authentication failed — possible MitM or wrong token on server")
            if self._on_auth_failed:
                self._on_auth_failed()
            raise _ServerAuthFailed()

        # Prove we know the token by signing the nonce — token never sent in the clear.
        shares = self._get_shares()
        hashes = [
            {"hash": s["hash"], "filename": s["filename"]}
            for s in shares if s.get("hash")
        ]
        client_digest = protocol.auth_digest(self._token, "client:" + challenge.nonce)
        await ws.send(protocol.encode(protocol.Register(
            version=protocol.PROTOCOL_VERSION,
            client_id=self._client_id,
            digest=client_digest,
            hashes=hashes,
        )))

        # Send Add for any shares added while disconnected
        for s in shares:
            if not s.get("hash"):
                await ws.send(protocol.encode(protocol.Add(filename=s["filename"])))

        self._on_status("connected")
        logger.info(
            "ws connected — %d hashes registered, %d pending",
            len(hashes), sum(1 for s in shares if not s.get("hash")),
        )

        async for message in ws:
            if isinstance(message, str):
                try:
                    msg = protocol.decode(message)
                except ValueError as exc:
                    logger.warning("malformed frame: %s", exc)
                    continue

                if isinstance(msg, protocol.Assigned):
                    logger.info("assigned filename=%r hash=%s", msg.filename, msg.hash)
                    self._on_assigned(msg.filename, msg.hash)

                elif isinstance(msg, protocol.Request):
                    logger.info("request hash=%s rid=%s", msg.hash, msg.request_id)
                    task = asyncio.create_task(self._serve(ws, msg))
                    self._active_serves[msg.request_id] = task
                    task.add_done_callback(
                        lambda _t, rid=msg.request_id: self._active_serves.pop(rid, None)
                    )

                elif isinstance(msg, protocol.Cancel):
                    logger.info("cancel rid=%s", msg.request_id)
                    task = self._active_serves.get(msg.request_id)
                    if task:
                        task.cancel()

    async def _serve(self, ws, req: protocol.Request):
        """Serve one file request — runs as a concurrent task."""
        share = self._get_share_by_hash(req.hash)
        path = Path(share["path"]) if share else None

        if path is None or not path.is_file():
            try:
                await ws.send(protocol.encode(
                    protocol.Error(request_id=req.request_id, status=404)
                ))
            except Exception:
                pass
            return

        sent = 0
        size = 0
        try:
            with open(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                await ws.send(protocol.encode(protocol.Response(
                    request_id=req.request_id,
                    status=200,
                    filename=share["filename"],
                    size=size,
                )))
                # (timestamp, cumulative_bytes) samples, trimmed to PROGRESS_WINDOW,
                # used to average out per-chunk jitter in the speed estimate.
                samples = collections.deque([(time.monotonic(), 0)])
                last_emit = 0.0
                while chunk := f.read(CHUNK_SIZE):
                    await ws.send(protocol.encode_chunk(req.request_id, chunk))
                    sent += len(chunk)
                    now = time.monotonic()
                    samples.append((now, sent))
                    while len(samples) > 2 and now - samples[0][0] > PROGRESS_WINDOW:
                        samples.popleft()
                    if self._on_progress and now - last_emit >= PROGRESS_INTERVAL:
                        elapsed = now - samples[0][0]
                        speed = (sent - samples[0][1]) / elapsed if elapsed > 0 else 0.0
                        self._on_progress(req.hash, sent, size, speed)
                        last_emit = now
        except asyncio.CancelledError:
            logger.info("serve cancelled hash=%s rid=%s (%d/%d bytes sent)",
                        req.hash, req.request_id, sent, size)
            if self._on_progress:
                self._on_progress(req.hash, sent, size, None)
            return
        except Exception as exc:
            logger.error("serve error hash=%s: %s", req.hash, exc)
            if self._on_progress:
                self._on_progress(req.hash, 0, 0, None)
            return

        if self._on_progress:
            self._on_progress(req.hash, size, size, None)
        if self._on_download:
            self._on_download(req.hash)
        logger.info("served hash=%s filename=%r size=%d", req.hash, share["filename"], size)

    # ------------------------------------------------------------------
    # Thread-safe public API — called from the Qt main thread
    # ------------------------------------------------------------------

    def send_add(self, filename: str):
        """Queue an Add frame. No-op if not currently connected."""
        ws = self._ws
        if ws is None:
            return

        async def _do():
            try:
                await ws.send(protocol.encode(protocol.Add(filename=filename)))
            except Exception:
                pass  # reconnect will re-add pending shares automatically

        asyncio.run_coroutine_threadsafe(_do(), self._loop)

    def send_remove(self, hash_val: str):
        """Queue a Remove frame. No-op if not currently connected."""
        ws = self._ws
        if ws is None:
            return

        async def _do():
            try:
                await ws.send(protocol.encode(protocol.Remove(hash=hash_val)))
            except Exception:
                pass

        asyncio.run_coroutine_threadsafe(_do(), self._loop)
