import asyncio
import hmac
import logging
import os
import secrets
import string
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, StreamingResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

import protocol

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("instasend")

# ---------------------------------------------------------------------------
# HTML page helpers
# ---------------------------------------------------------------------------

_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


def _page(title: str, heading: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — InstaSend</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #1e1e2e; color: #cdd6f4;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; padding: 24px;
    }}
    .card {{
      text-align: center; max-width: 480px; width: 100%;
    }}
    h1 {{ font-size: 2rem; margin-bottom: 12px; }}
    p  {{ color: #a6adc8; line-height: 1.6; }}
    .badge {{
      display: inline-block; margin-bottom: 24px;
      font-size: 0.75rem; font-weight: 600; letter-spacing: .08em;
      text-transform: uppercase; color: #89b4fa;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">InstaSend</div>
    <h1>{heading}</h1>
    <p>{body}</p>
  </div>
</body>
</html>"""
    return HTMLResponse(html, status_code=status_code)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/shares.db")
TOKEN   = os.environ.get("TOKEN", "")

# ---------------------------------------------------------------------------
# Per-connection limits
# ---------------------------------------------------------------------------

MAX_HASHES_PER_CLIENT    = 20   # max active shares a single desktop can register
MAX_WS_PER_IP            = 3    # max concurrent WebSocket connections from one IP
MAX_CONCURRENT_REQUESTS  = 20   # max in-flight downloads per desktop connection

# ---------------------------------------------------------------------------
# Hash generation
# ---------------------------------------------------------------------------

_HASH_CHARS = string.ascii_letters + string.digits
_HASH_LEN   = 8  # 62^8 ≈ 218 trillion combinations


async def _generate_hash(db: aiosqlite.Connection) -> str:
    """Return a random hash that has never appeared in the ownership DB."""
    while True:
        h = "".join(secrets.choice(_HASH_CHARS) for _ in range(_HASH_LEN))
        async with db.execute("SELECT 1 FROM shares WHERE hash = ?", (h,)) as cur:
            if await cur.fetchone() is None:
                return h


# ---------------------------------------------------------------------------
# Ownership DB (persistent) + in-memory live registry
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not TOKEN or TOKEN == "change-me":
        raise RuntimeError(
            "TOKEN env var is not set or is the default placeholder — "
            "set a strong token before starting the server"
        )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                hash      TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                filename  TEXT NOT NULL
            )
        """)
        await db.commit()
    yield


class Connection:
    """Represents one live desktop client."""

    def __init__(self, ws: WebSocket, client_id: str):
        self.ws = ws
        self.client_id = client_id
        self.hashes: set[str] = set()
        # request_id → asyncio.Queue — populated by the download handler (S4)
        self.pending: dict[str, asyncio.Queue] = {}

    def dispatch(self, msg):
        """Route a Response or Error frame to its waiting download queue."""
        request_id = getattr(msg, "request_id", None)
        if request_id and request_id in self.pending:
            self.pending[request_id].put_nowait(msg)

    def dispatch_chunk(self, data: bytes):
        """Route a binary chunk frame to its waiting download queue."""
        request_id, chunk = protocol.decode_chunk(data)
        if request_id in self.pending:
            self.pending[request_id].put_nowait(chunk)


class Registry:
    def __init__(self):
        self._hash_to_conn: dict[str, Connection] = {}
        self._connections: set[Connection] = set()

    def connect(self, ws: WebSocket, client_id: str) -> Connection:
        conn = Connection(ws, client_id)
        self._connections.add(conn)
        return conn

    def add(self, conn: Connection, hash_val: str):
        conn.hashes.add(hash_val)
        self._hash_to_conn[hash_val] = conn

    def remove(self, conn: Connection, hash_val: str):
        conn.hashes.discard(hash_val)
        if self._hash_to_conn.get(hash_val) is conn:
            del self._hash_to_conn[hash_val]

    def disconnect(self, conn: Connection):
        self._connections.discard(conn)
        for h in list(conn.hashes):
            if self._hash_to_conn.get(h) is conn:
                del self._hash_to_conn[h]
        conn.hashes.clear()

    def get(self, hash_val: str) -> Connection | None:
        return self._hash_to_conn.get(hash_val)


registry = Registry()

# Active WebSocket connections per remote IP — used to enforce MAX_WS_PER_IP.
_active_ws_per_ip: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# App + middleware
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers[k] = v
        return response


app.add_middleware(_SecurityHeadersMiddleware)

# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, _exc):
    return _page("Too Many Requests", "Slow down",
                 "Too many requests. Please try again in a moment.", 429)


@app.exception_handler(404)
async def not_found_handler(request: Request, _exc):
    return _page("Not Found", "File not found",
                 "This link has expired or was never created. Ask the sender for a new one.", 404)


@app.exception_handler(503)
async def unavailable_handler(request: Request, _exc):
    return _page("Unavailable", "File temporarily unavailable",
                 "The host machine is offline or the InstaSend app is not running. Try again later.", 503)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, _exc):
    return _page("Bad Request", "Bad request", "The request could not be understood.", 400)


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return _page("InstaSend", "InstaSend",
                 "Drop a file into the InstaSend app to generate a share link.")


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    ip = ws.client.host if ws.client else "unknown"
    if _active_ws_per_ip.get(ip, 0) >= MAX_WS_PER_IP:
        await ws.close(code=4008, reason="Too many connections")
        return
    _active_ws_per_ip[ip] = _active_ws_per_ip.get(ip, 0) + 1
    try:
        # First frame must be register, received within 10 s
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
            msg = protocol.decode(raw)
        except asyncio.TimeoutError:
            await ws.close(code=4002, reason="Registration timeout")
            return
        except ValueError:
            await ws.close(code=4002, reason="Protocol error")
            return

        if not isinstance(msg, protocol.Register):
            await ws.close(code=4002, reason="Expected register message")
            return

        if msg.version != protocol.PROTOCOL_VERSION:
            await ws.close(code=4003, reason="Protocol version mismatch")
            return

        if not TOKEN or not hmac.compare_digest(msg.token, TOKEN):
            logger.warning("ws auth failed client_id=%s ip=%s", msg.client_id[:8], ip)
            await ws.close(code=4001, reason="Authentication failed")
            return

        if len(msg.hashes) > MAX_HASHES_PER_CLIENT:
            await ws.close(code=4002, reason="Too many hashes")
            return

        client_id = msg.client_id
        conn = registry.connect(ws, client_id)

        # Validate ownership of each stored hash.
        # Hashes owned by this client are re-registered in the live registry.
        # Any hash that is unrecognised or owned by a different client is retired
        # and a fresh one is assigned — the desktop is notified via `assigned`.
        async with aiosqlite.connect(DB_PATH) as db:
            for entry in msg.hashes:
                hash_val = entry["hash"]
                filename = entry["filename"]

                async with db.execute(
                    "SELECT client_id FROM shares WHERE hash = ?", (hash_val,)
                ) as cur:
                    row = await cur.fetchone()

                if row is not None and row[0] == client_id:
                    registry.add(conn, hash_val)
                    await ws.send_text(protocol.encode(
                        protocol.Assigned(filename=filename, hash=hash_val)
                    ))
                else:
                    new_hash = await _generate_hash(db)
                    await db.execute(
                        "INSERT INTO shares (hash, client_id, filename) VALUES (?, ?, ?)",
                        (new_hash, client_id, filename),
                    )
                    await db.commit()
                    registry.add(conn, new_hash)
                    await ws.send_text(protocol.encode(
                        protocol.Assigned(filename=filename, hash=new_hash)
                    ))
                    logger.info("ws reassigned %s → %s client=%s", hash_val, new_hash, client_id[:8])

        logger.info("ws connected client=%s hashes=%d", client_id[:8], len(conn.hashes))

        try:
            while True:
                frame = await ws.receive()

                if frame["type"] == "websocket.disconnect":
                    break

                if frame.get("text"):
                    try:
                        msg = protocol.decode(frame["text"])
                    except ValueError:
                        await ws.close(code=4002, reason="Protocol error")
                        return

                    if isinstance(msg, protocol.Add):
                        if len(conn.hashes) >= MAX_HASHES_PER_CLIENT:
                            await ws.close(code=4002, reason="Too many active shares")
                            return
                        async with aiosqlite.connect(DB_PATH) as db:
                            new_hash = await _generate_hash(db)
                            await db.execute(
                                "INSERT INTO shares (hash, client_id, filename) VALUES (?, ?, ?)",
                                (new_hash, client_id, msg.filename),
                            )
                            await db.commit()
                        registry.add(conn, new_hash)
                        await ws.send_text(protocol.encode(
                            protocol.Assigned(filename=msg.filename, hash=new_hash)
                        ))
                        logger.info("ws add hash=%s client=%s", new_hash, client_id[:8])

                    elif isinstance(msg, protocol.Remove):
                        registry.remove(conn, msg.hash)
                        # Hash is intentionally kept in DB so it can never be
                        # reassigned to a different client.
                        logger.info("ws remove hash=%s", msg.hash)

                    else:
                        # Response / Error frames: routed to download handler
                        conn.dispatch(msg)

                elif frame.get("bytes"):
                    try:
                        conn.dispatch_chunk(frame["bytes"])
                    except (ValueError, UnicodeDecodeError):
                        await ws.close(code=4002, reason="Protocol error")
                        return

        except WebSocketDisconnect:
            pass
        finally:
            logger.info("ws disconnected client=%s released %d hashes",
                        client_id[:8], len(conn.hashes))
            registry.disconnect(conn)

    finally:
        _active_ws_per_ip[ip] -= 1
        if _active_ws_per_ip[ip] == 0:
            del _active_ws_per_ip[ip]


# ---------------------------------------------------------------------------
# Public download endpoint — stubbed pending S4
# ---------------------------------------------------------------------------

@app.get("/{hash}")
@limiter.limit("30/minute")
async def download(request: Request, hash: str):
    client_host = request.client.host if request.client else "unknown"
    logger.info("download request hash=%s from=%s", hash, client_host)

    conn = registry.get(hash)
    if conn is None:
        raise HTTPException(status_code=404, detail="Not found")

    if len(conn.pending) >= MAX_CONCURRENT_REQUESTS:
        return _page("Too Many Requests", "Slow down",
                     "Too many concurrent downloads for this file. Try again in a moment.", 429)

    request_id = uuid.uuid4().hex  # 32 ASCII hex chars — fits REQUEST_ID_BYTES
    queue: asyncio.Queue = asyncio.Queue()
    conn.pending[request_id] = queue

    # Ask the desktop to serve the file
    try:
        await conn.ws.send_text(protocol.encode(
            protocol.Request(request_id=request_id, hash=hash)
        ))
    except Exception:
        conn.pending.pop(request_id, None)
        raise HTTPException(status_code=503, detail="Desktop connection lost")

    # Wait for the desktop's response frame (metadata)
    try:
        msg = await asyncio.wait_for(queue.get(), timeout=30.0)
    except asyncio.TimeoutError:
        conn.pending.pop(request_id, None)
        raise HTTPException(status_code=503, detail="Desktop timed out")

    if isinstance(msg, protocol.Error):
        conn.pending.pop(request_id, None)
        raise HTTPException(status_code=404 if msg.status == 404 else 503)

    if not isinstance(msg, protocol.Response):
        conn.pending.pop(request_id, None)
        raise HTTPException(status_code=503, detail="Unexpected response from desktop")

    meta = msg
    safe_ascii = "".join(
        c for c in meta.filename if 0x20 <= ord(c) < 0x7F and c not in '"\\;'
    ) or "download"
    encoded_name = quote(meta.filename, safe="")
    logger.info("streaming hash=%s filename=%r size=%d to=%s",
                hash, safe_ascii, meta.size, client_host)

    async def stream_chunks():
        try:
            remaining = meta.size
            while remaining > 0:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    logger.warning("chunk timeout hash=%s request_id=%s", hash, request_id)
                    return
                if not isinstance(chunk, bytes):
                    logger.warning("expected bytes, got %s hash=%s", type(chunk), hash)
                    return
                yield chunk
                remaining -= len(chunk)
        finally:
            conn.pending.pop(request_id, None)

    return StreamingResponse(
        stream_chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded_name}',
            "Content-Length": str(meta.size),
        },
    )
