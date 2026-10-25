"""Minimal, local-first HTTP ingest server for external/mobile captures.

Append-only, strictly private: accepts POST /ingest or /yakala into inbox.
Zero outbound data leakage: no vault files are ever served or read over HTTP.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hmac
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import socket
import sys
import unicodedata

MAX_PAYLOAD_BYTES = 1024 * 1024  # 1 MB
INBOX_CANDIDATES = ("000-Inbox", "📥 000-Inbox", "Inbox", "inbox", "Gelen Kutusu")


def _find_inbox_dir(vault: Path) -> Path:
    """Dynamically resolve preferred inbox directory inside vault."""
    vault = Path(vault).resolve()
    for candidate in INBOX_CANDIDATES:
        candidate_path = vault / candidate
        if candidate_path.is_symlink():
            continue
        if candidate_path.is_dir():
            return candidate_path
    target = vault / "000-Inbox"
    if target.is_symlink():
        raise ValueError("Inbox symlink is not allowed")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _clean_yaml_str(value: str) -> str:
    """Sanitize strings for YAML frontmatter to prevent multiline or separator injection."""
    text = re.sub(r'[\r\n]+', ' ', str(value or '')).strip()
    # JSON string escaping is a valid YAML quoted scalar, including backslashes.
    return json.dumps(text, ensure_ascii=False)


def _slugify(text: str, max_length: int = 40) -> str:
    text = unicodedata.normalize('NFKD', text)
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    text = re.sub(r'[-\s]+', '-', text)
    return text[:max_length].strip('-') or 'not'


def save_capture(vault: Path, data: dict | str, now: datetime | None = None) -> tuple[Path, int]:
    """Save an incoming capture append-only into vault inbox."""
    vault = Path(vault).resolve()
    inbox_dir = _find_inbox_dir(vault)

    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")

    if isinstance(data, str):
        text = data.strip()
        first_line = text.splitlines()[0][:60] if text else "Mobil Not"
        title = first_line.lstrip("# \t")
        url = None
        tags = ["mobil", "yakalama"]
        source = "mobile-ingest"
    else:
        text = str(data.get("text", "")).strip()
        title = str(data.get("title", "")).strip()
        if not title:
            first_line = text.splitlines()[0][:60] if text else "Mobil Not"
            title = first_line.lstrip("# \t")
        title = re.sub(r'[\r\n]+', ' ', title)
        url = data.get("url")
        tags = data.get("tags") or ["mobil", "yakalama"]
        source = data.get("source") or "mobile-ingest"

    slug = _slugify(title)
    filename = f"{stamp}-{slug}.md"
    # Build Markdown document with untrusted: true marker for V3 trust model
    lines = [
        "---",
        f"created_at: {now.isoformat()}",
        f"source: {_clean_yaml_str(source)}",
        "untrusted: true",
        "trust: untrusted",
    ]
    if url:
        lines.append(f"url: {_clean_yaml_str(url)}")
    if tags:
        if isinstance(tags, list):
            lines.append("tags:")
            for t in tags:
                clean_tag = _slugify(str(t), max_length=30)
                if clean_tag:
                    lines.append(f"  - {clean_tag}")
        else:
            clean_tag = _slugify(str(tags), max_length=30)
            if clean_tag:
                lines.append(f"tags: [{clean_tag}]")
    lines.append("---\n")
    if title and not text.startswith("#"):
        lines.append(f"# {title}\n")
    if url and str(url) not in text:
        lines.append(f"Kaynak: {url}\n")
    lines.append(text)
    lines.append("")

    content = "\n".join(lines).encode("utf-8")
    # Exclusive creation preserves existing captures, including dangling symlinks.
    counter = 0
    while True:
        target = inbox_dir / (filename if not counter else f"{stamp}-{slug}-{counter}.md")
        if inbox_dir.is_symlink() or not inbox_dir.resolve().is_relative_to(vault):
            raise ValueError("Invalid inbox path")
        try:
            with target.open("xb") as output:
                output.write(content)
            return target, len(content)
        except FileExistsError:
            counter += 1


class IngestHandler(BaseHTTPRequestHandler):
    vault: Path
    token: str | None = None

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, socket.timeout):
            pass

    def _send_json(self, status_code: int, data: dict):
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if self.token:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            if getattr(self, "close_connection", False):
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _browser_write_forbidden(self):
        # Native local captures carry no Origin. Browser sites cannot write to a
        # tokenless loopback listener, including sandboxed origins ("null").
        return not self.token and (self.headers.get("Origin") is not None or
                                   self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site")

    def do_OPTIONS(self):
        if self._browser_write_forbidden():
            self._send_json(403, {"error": "cross_origin_forbidden"})
            return
        self.send_response(204)
        if self.token:
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
        if self._browser_write_forbidden():
            self._send_json(403, {"error": "cross_origin_forbidden"})
            return
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/ingest", "/yakala"):
            self._send_json(404, {"error": "not_found", "message": "Endpoint must be /ingest or /yakala"})
            return

        # Check authentication using constant-time comparison
        if self.token:
            auth_header = self.headers.get("Authorization", "")
            expected_bearer = f"Bearer {self.token}"
            token_valid = False
            if auth_header.startswith("Bearer "):
                token_valid = hmac.compare_digest(auth_header.encode("utf-8"), expected_bearer.encode("utf-8"))
            if not token_valid:
                self._send_json(401, {"error": "unauthorized", "message": "Invalid or missing Bearer token"})
                return

        # Read and bound request body
        try:
            cl_header = self.headers.get("Content-Length")
            if cl_header is None:
                self.close_connection = True
                self._send_json(411, {"error": "length_required", "message": "Content-Length header required"})
                return
            length = int(cl_header)
            if length < 0:
                self.close_connection = True
                self._send_json(400, {"error": "bad_request", "message": "Negative Content-Length"})
                return
            if length > MAX_PAYLOAD_BYTES:
                self.close_connection = True
                self._send_json(413, {"error": "payload_too_large", "message": "Payload exceeds 1MB limit"})
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body")
            body = raw.decode("utf-8", errors="strict")
        except (ValueError, OSError) as exc:
            self.close_connection = True
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

        if not isinstance(data, (dict, str)):
            self._send_json(400, {"error": "invalid_payload"})
            return
        try:
            target_path, nbytes = save_capture(self.vault, data)
            rel = target_path.resolve().relative_to(self.vault.resolve()).as_posix()
            self._send_json(201, {
                "status": "accepted",
                "file": rel,
                "bytes": nbytes,
                "message": "Kayit basariyla gelen kutusuna eklendi."
            })
        except Exception as exc:
            self._send_json(500, {"error": "write_failed", "message": "Capture could not be saved"})

    def log_message(self, format, *args):
        # Quiet stderr logging
        sys.stderr.write(f"[ingest-server] {self.address_string()} request completed\n")


def create_server(vault: Path, host: str = "127.0.0.1", port: int = 8484, token: str | None = None) -> ThreadingHTTPServer:
    token = token or os.environ.get("BEYIN_INGEST_TOKEN")

    class BoundHandler(IngestHandler):
        pass

    BoundHandler.vault = Path(vault).resolve()
    BoundHandler.token = token
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise ValueError("Non-loopback ingest requires a Bearer token")
    return ThreadingHTTPServer((host, port), BoundHandler)


def run_server(vault: Path, host: str = "127.0.0.1", port: int = 8484, token: str | None = None):
    server = create_server(vault, host=host, port=port, token=token)
    auth_msg = " (Bearer token aktif)" if (token or os.environ.get("BEYIN_INGEST_TOKEN")) else " (Token korumasi yok - yerel ag)"
    print("=== İkinci Beyin Ingest Sunucusu Başlatıldı ===")
    print(f"Adres: http://{host}:{port}/ingest{auth_msg}")
    print(f"Kasa: {vault}")
    print("Gelen kutusu: hazir")
    print("Durdurmak için: Ctrl+C")
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
