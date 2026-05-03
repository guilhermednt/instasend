import ipaddress
import os
from contextlib import asynccontextmanager

import aiosqlite
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# HTML page helpers
# ---------------------------------------------------------------------------

def _page(title: str, heading: str, body: str) -> HTMLResponse:
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
    return HTMLResponse(html)

DB_PATH = os.environ.get("DB_PATH", "shares.db")
INTERNAL_NETWORK = ipaddress.IPv4Network(
    os.environ.get("INTERNAL_NETWORK", "10.0.0.0/24")
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                hash     TEXT PRIMARY KEY,
                client_ip TEXT NOT NULL,
                port     INTEGER NOT NULL,
                filename TEXT NOT NULL
            )
        """)
        await db.commit()
    yield


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return _page(
        title="InstaSend",
        heading="InstaSend",
        body="Drop a file into the InstaSend app to generate a share link.",
    )


# ---------------------------------------------------------------------------
# Custom error pages
# ---------------------------------------------------------------------------

@app.exception_handler(404)
async def not_found_handler(request: Request, _exc):
    return _page(
        title="Not Found",
        heading="File not found",
        body="This link has expired or was never created. Ask the sender for a new one.",
    )


@app.exception_handler(403)
async def forbidden_handler(request: Request, _exc):
    return _page(
        title="Forbidden",
        heading="Access denied",
        body="This action is only available from the local network.",
    )


@app.exception_handler(503)
async def unavailable_handler(request: Request, _exc):
    return _page(
        title="Unavailable",
        heading="File temporarily unavailable",
        body="The host machine is offline or the InstaSend app is not running. Try again later.",
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, _exc):
    return _page(
        title="Bad Request",
        heading="Bad request",
        body="The request could not be understood.",
    )


# ---------------------------------------------------------------------------
# Internal-network guard (S4)
# ---------------------------------------------------------------------------

def require_internal(request: Request):
    """Dependency that rejects requests from outside INTERNAL_NETWORK."""
    raw = request.client.host if request.client else ""
    try:
        ip = ipaddress.IPv4Address(raw)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden")
    if ip not in INTERNAL_NETWORK:
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Management endpoints (internal only)
# ---------------------------------------------------------------------------

class ShareIn(BaseModel):
    hash: str
    port: int
    filename: str


@app.post("/shares", status_code=201, dependencies=[Depends(require_internal)])
async def register_share(body: ShareIn, request: Request):
    """Register (or update) a share. Client IP is read from the connection."""
    client_ip = request.client.host
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO shares (hash, client_ip, port, filename)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                client_ip = excluded.client_ip,
                port      = excluded.port,
                filename  = excluded.filename
            """,
            (body.hash, client_ip, body.port, body.filename),
        )
        await db.commit()
    return {"hash": body.hash, "client_ip": client_ip}


@app.delete("/shares/{hash}", status_code=204, dependencies=[Depends(require_internal)])
async def delete_share(hash: str):
    """Remove a share entirely. Future GET requests for this hash return 404."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM shares WHERE hash = ?", (hash,))
        await db.commit()


# ---------------------------------------------------------------------------
# Public download endpoint (S7)
# ---------------------------------------------------------------------------

# Headers the upstream sends that we must not blindly forward to the client
# (they describe the upstream connection, not the proxied one).
_HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
}

PROXY_TIMEOUT = httpx.Timeout(10.0, read=None)  # no read timeout for large files


@app.get("/{hash}")
async def download(hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT client_ip, port, filename FROM shares WHERE hash = ?", (hash,)
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Not found")

    client_ip, port, filename = row
    upstream = f"http://{client_ip}:{port}/{hash}"

    async def stream_upstream():
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
            async with client.stream("GET", upstream) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    yield chunk

    # Probe the upstream first to get status + headers, then stream the body.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            head = await client.head(upstream)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="File host unreachable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="File host timed out")

    if head.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")

    headers = {
        k: v for k, v in head.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    return StreamingResponse(
        stream_upstream(),
        status_code=200,
        headers=headers,
        media_type=head.headers.get("content-type", "application/octet-stream"),
    )
