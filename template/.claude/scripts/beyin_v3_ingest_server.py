"""Minimal, local-first HTTP ingest server for external/mobile captures.

Append-only, strictly private: accepts POST /ingest or /yakala into 000-Inbox.
Zero outbound data leakage: no vault files are ever served or read over HTTP.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

MAX_PAYLOAD_BYTES = 1024 * 1024  # 1 MB


def _slugify(text: str, max_length: int = 40) -> str:
    # Normalize unicode
    text = unicodedata.normalize('NFKD', text)
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    text = re.sub(r'[-\s]+', '-', text)
    return text[:max_length].strip('-') or 'not'


def save_capture(vault: Path, data: dict | str, now: datetime | None = None) -> tuple[Path, int]:
    """Save an incoming capture append-only into vault inbox."""
    vault = Path(vault).resolve()
    # Preferred inbox directory
    inbox_dir = vault / "000-Inbox"
    if not inbox_dir.exists() and (vault / "inbox").is_dir():
        inbox_dir = vault / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")

    if isinstance(data, str):
        text = data.strip()
        title = text.splitlines()[0][:60] if text else "Mobil Not"
        url = None
        tags = ["mobil", "yakalama"]
        source = "mobile-ingest"
    else:
        text = str(data.get("text", "")).strip()
        title = str(data.get("title", "")).strip() or (text.splitlines()[0][:60] if text else "Mobil Not")
        url = data.get("url")
        tags = data.get("tags") or ["mobil", "yakalama"]
        source = data.get("source") or "mobile-ingest"

    slug = _slugify(title)
    filename = f"{stamp}-{slug}.md"
    target = inbox_dir / filename

    # Avoid any collision
    counter = 1
    while target.exists():
        target = inbox_dir / f"{stamp}-{slug}-{counter}.md"
        counter += 1

    # Security check: ensure path is strictly inside inbox
    if not target.resolve().is_relative_to(vault):
        raise ValueError("Invalid target path traversal")

    # Build Markdown document
    lines = ["---", f"created_at: {now.isoformat()}", f"source: {source}"]
    if url:
        lines.append(f"url: {url}")
    if tags:
        if isinstance(tags, list):
            lines.append("tags:")
            for t in tags:
                lines.append(f"  - {t}")
        else:
            lines.append(f"tags: [{tags}]")
    lines.append("---\n")
    if title and not text.startswith("#"):
        lines.append(f"# {title}\n")
    if url and url not in text:
        lines.append(f"Kaynak: {url}\n")
    lines.append(text)
    lines.append("")

    content = "\n".join(lines).encode("utf-8")
    target.write_bytes(content)
    return target, len(content)


class IngestHandler(BaseHTTPRequestHandler):
    vault: Path
    token: str | None = None

    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/health", "/durum"):
            self._send_json(200, {
                "status": "ok",
                "service": "beyin-ingest-server",
                "version": "1.0.0",
                "inbox": "ready"
            })
        else:
            # Strictly reject all other GET requests: no vault browsing allowed!
            self._send_json(404, {"error": "not_found", "message": "Only health and ingest endpoints are exposed."})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/ingest", "/yakala"):
            self._send_json(404, {"error": "not_found", "message": "Endpoint must be /ingest or /yakala"})
            return

        # Check authentication if token is set
        if self.token:
            auth_header = self.headers.get("Authorization", "")
            expected_bearer = f"Bearer {self.token}"
            if auth_header != expected_bearer:
                # Also check query param fallback e.g. ?token=...
                query_token = None
                if "?" in self.path:
                    params = dict(q.split("=", 1) for q in self.path.split("?")[1].split("&") if "=" in q)
                    query_token = params.get("token")
                if query_token != self.token:
                    self._send_json(401, {"error": "unauthorized", "message": "Invalid or missing Bearer token"})
                    return

        # Read request body
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_PAYLOAD_BYTES:
                self._send_json(413, {"error": "payload_too_large", "message": "Payload exceeds 1MB limit"})
                return
            body = self.rfile.read(length).decode("utf-8", errors="replace")
        except Exception as exc:
            self._send_json(400, {"error": "bad_request", "message": str(exc)})
            return

        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                data = json.loads(body)
            except Exception:
                data = body
        else:
            try:
                data = json.loads(body)
            except Exception:
                data = body

        try:
            target_path, nbytes = save_capture(self.vault, data)
            rel = target_path.relative_to(self.vault).as_posix()
            self._send_json(201, {
                "status": "accepted",
                "file": rel,
                "bytes": nbytes,
                "message": "Kayit basariyla gelen kutusuna eklendi."
            })
        except Exception as exc:
            self._send_json(500, {"error": "write_failed", "message": str(exc)})

    def log_message(self, format, *args):
        # Concise quiet logging
        sys.stderr.write(f"[ingest-server] {self.address_string()} - {format % args}\n")


def create_server(vault: Path, host: str = "127.0.0.1", port: int = 8484, token: str | None = None) -> HTTPServer:
    token = token or os.environ.get("BEYIN_INGEST_TOKEN")

    class BoundHandler(IngestHandler):
        pass

    BoundHandler.vault = Path(vault).resolve()
    BoundHandler.token = token
    return HTTPServer((host, port), BoundHandler)


def run_server(vault: Path, host: str = "127.0.0.1", port: int = 8484, token: str | None = None):
    server = create_server(vault, host=host, port=port, token=token)
    auth_msg = f" (Bearer token aktif)" if (token or os.environ.get("BEYIN_INGEST_TOKEN")) else " (Token korumasi yok - yerel ag)"
    print(f"=== İkinci Beyin Ingest Sunucusu Başlatıldı ===")
    print(f"Adres: http://{host}:{port}/ingest{auth_msg}")
    print(f"Kasa: {vault}")
    print(f"Gelen kutusu: 000-Inbox/")
    print(f"Durdurmak için: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSunucu kapatıldı.")
    finally:
        server.server_close()


if __name__ == "__main__":
    vault_path = Path.cwd()
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8484
    run_server(vault_path, port=port)
