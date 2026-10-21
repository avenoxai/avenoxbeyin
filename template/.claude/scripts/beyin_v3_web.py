#!/usr/bin/env python3
"""Beyin Web Portal: Local-first, zero-dependency companion web dashboard.
Strictly adheres to Issue #371 security and architecture invariants.
"""
import argparse
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser

VERSION = "1.1.0"
DEFAULT_HOST = "127.0.0.1"
PORT_RANGE = (7373, 7393)
IDLE_TIMEOUT_SECONDS = 1800  # 30 minutes

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self' 'unsafe-inline' 'unsafe-eval' https://fonts.googleapis.com https://fonts.gstatic.com https://cdn.tailwindcss.com https://lh3.googleusercontent.com; "
        "img-src 'self' data: https:; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _is_safe_subpath(base_dir: Path, target_path: Path) -> bool:
    try:
        resolved_base = base_dir.resolve()
        resolved_target = target_path.resolve()
        return resolved_target == resolved_base or resolved_base in resolved_target.parents
    except (OSError, RuntimeError, ValueError):
        return False


def _resolve_state(vault: Path) -> Path:
    runtime_json = vault / ".beyin-runtime.json"
    if runtime_json.is_file():
        try:
            data = json.loads(runtime_json.read_text(encoding="utf-8"))
            if data.get("state"):
                return Path(data["state"]).resolve()
        except Exception:
            pass
    # Default fallback
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "beyin" / "state"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "beyin"
    return Path.home() / ".local" / "state" / "beyin"


def _find_companion_dir(vault: Path) -> Path:
    # 1. Standard candidate names
    for name in ("🔮 850-Companion", "Companion", "850-Companion", "companion"):
        cand = vault / name
        if cand.is_dir():
            return cand
    # 2. Search for any folder with Core.md / Soul.md
    try:
        for d in vault.iterdir():
            if d.is_dir() and not d.name.startswith(".") and not re.search(r"(?i)(archive|arşiv|arsiv)", d.name):
                if any((d / f).is_file() for f in ("Core.md", "Soul.md")):
                    return d
    except Exception:
        pass
    return vault / "🔮 850-Companion"


def _find_web_dir(vault: Path) -> Path:
    candidates = [
        vault / "template" / ".claude" / "web",
        vault / ".claude" / "web",
        vault / "web",
        Path(__file__).resolve().parent.parent / "template" / ".claude" / "web",
        Path(__file__).resolve().parent.parent / "web",
    ]
    for cand in candidates:
        if (cand / "index.html").is_file():
            return cand
    return candidates[0]


class BeyinWebHandler(BaseHTTPRequestHandler):
    def __init__(self, request, client_address, server):
        self.server_instance = server
        super().__init__(request, client_address, server)

    def log_message(self, format, *args):
        # Silent by default
        pass

    def _send_headers(self, status=200, content_type="application/json; charset=utf-8", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()

    def _is_valid_host(self) -> bool:
        host = self.headers.get("Host", "")
        allowed = {
            f"127.0.0.1:{self.server_instance.port}",
            f"localhost:{self.server_instance.port}",
        }
        return host in allowed

    def _is_valid_write(self) -> bool:
        origins = self.headers.get("Origin")
        if not origins:
            return False
        allowed = {
            f"http://127.0.0.1:{self.server_instance.port}",
            f"http://localhost:{self.server_instance.port}",
        }
        if origins not in allowed:
            return False
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";")[0].strip().lower() != "application/json":
            return False
        if self.headers.get("X-Beyin-Web") != "1":
            return False
        return True

    def _verify_auth(self) -> bool:
        if not self._is_valid_host():
            return False

        # Check Cookie session token
        cookie_header = self.headers.get("Cookie", "")
        session_token = None
        for cookie in cookie_header.split(";"):
            parts = cookie.strip().split("=", 1)
            if len(parts) == 2 and parts[0] == f"bw_session_{self.server_instance.port}":
                session_token = parts[1]
                break

        if session_token and hmac.compare_digest(session_token, self.server_instance.session_token):
            return True

        # Check auth query param for initial browser load
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        token_param = qs.get("token", [None])[0]
        if token_param and hmac.compare_digest(token_param, self.server_instance.session_token):
            return True

        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Initial index load with token in query param
        qs = urllib.parse.parse_qs(parsed.query)
        token_in_url = qs.get("token", [None])[0]
        if path in ("/", "/index.html"):
            if token_in_url and hmac.compare_digest(token_in_url, self.server_instance.session_token):
                cookie_val = f"bw_session_{self.server_instance.port}={self.server_instance.session_token}; HttpOnly; SameSite=Strict; Path=/"
                web_dir = self.server_instance.web_dir
                index_path = web_dir / "index.html"
                if index_path.is_file():
                    content = index_path.read_bytes()
                    self._send_headers(200, "text/html; charset=utf-8", {"Set-Cookie": cookie_val})
                    self.wfile.write(content)
                    return

        # Allow logo/favicon without auth for browser initial render
        if path in ("/avenox_logo.png", "/favicon.ico", "/assets/avenox_logo.png"):
            logo_path = self.server_instance.web_dir / "avenox_logo.png"
            if logo_path.is_file():
                self._send_headers(200, "image/png")
                self.wfile.write(logo_path.read_bytes())
                return

        # Static css
        if path in ("/compiled.css", "/style.css"):
            css_path = self.server_instance.web_dir / "compiled.css"
            if css_path.is_file():
                self._send_headers(200, "text/css; charset=utf-8")
                self.wfile.write(css_path.read_bytes())
                return

        # For all other requests, verify auth & host
        if not self._verify_auth():
            if path in ("/", "/index.html") and "text/html" in self.headers.get("Accept", ""):
                self._send_headers(401, "text/html; charset=utf-8")
                unlock_html = self._get_unlock_html()
                self.wfile.write(unlock_html.encode("utf-8"))
                return
            self._send_headers(401)
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode())
            return

        if path in ("/", "/index.html"):
            web_dir = self.server_instance.web_dir
            index_path = web_dir / "index.html"
            if index_path.is_file():
                content = index_path.read_bytes()
                self._send_headers(200, "text/html; charset=utf-8")
                self.wfile.write(content)
                return
            self._send_headers(404, "text/plain")
            self.wfile.write(b"index.html not found in web directory.")
            return

        # API Endpoints
        if path == "/api/v1/pulse":
            self._handle_pulse()
        elif path == "/api/v1/live-stats":
            self._handle_live_stats()
        elif path == "/api/v1/projects":
            self._handle_projects()
        elif path == "/api/v1/goals":
            self._handle_goals()
        elif path == "/api/v1/auditor":
            self._handle_auditor()
        elif path == "/api/v1/receipts":
            self._handle_receipts()
        elif path == "/api/v1/graph":
            self._handle_graph()
        elif path == "/api/v1/search":
            self._handle_search()
        elif path == "/api/v1/file":
            self._handle_file()
        else:
            self._send_headers(404)
            self.wfile.write(json.dumps({"error": "Not Found"}).encode())

    def do_POST(self):
        if not self._verify_auth():
            self._send_headers(401)
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode())
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Heartbeat only requires auth, not strict Origin since it's just a keepalive ping
        if path == "/api/v1/heartbeat":
            self.server_instance.touch_interaction()
            self._send_headers(200)
            self.wfile.write(json.dumps({"status": "ok", "interaction_recorded": True}).encode())
            return

        # All mutating actions strictly require valid write (Origin, JSON, X-Beyin-Web: 1)
        if not self._is_valid_write():
            self._send_headers(403)
            self.wfile.write(json.dumps({"error": "Forbidden", "message": "Invalid write verification"}).encode())
            return

        self.server_instance.touch_interaction()
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""

        if path == "/api/v1/action/compact":
            self._handle_action_compact()
        elif path == "/api/v1/goals":
            if self.server_instance.read_only:
                self._send_headers(403)
                self.wfile.write(json.dumps({"error": "Forbidden", "message": "Sunucu salt-okunur modda"}).encode())
                return
            self._handle_save_goal(body)
        elif path == "/api/v1/tasks/toggle":
            if self.server_instance.read_only:
                self._send_headers(403)
                self.wfile.write(json.dumps({"error": "Forbidden", "message": "Sunucu salt-okunur modda"}).encode())
                return
            self._handle_toggle_task(body)
        elif path == "/api/v1/tasks":
            if self.server_instance.read_only:
                self._send_headers(403)
                self.wfile.write(json.dumps({"error": "Forbidden", "message": "Sunucu salt-okunur modda"}).encode())
                return
            self._handle_add_task(body)
        elif path == "/api/v1/habits/toggle":
            if self.server_instance.read_only:
                self._send_headers(403)
                self.wfile.write(json.dumps({"error": "Forbidden", "message": "Sunucu salt-okunur modda"}).encode())
                return
            self._handle_toggle_habit(body)
        else:
            self._send_headers(404)
            self.wfile.write(json.dumps({"error": "Endpoint not found"}).encode())

    def _handle_pulse(self):
        obsidian_uri = f"obsidian://open?path={urllib.parse.quote(str(self.server_instance.vault), safe='')}"
        data = {
            "status": "ok",
            "version": VERSION,
            "vault": str(self.server_instance.vault),
            "vault_name": self.server_instance.vault.name,
            "obsidian_uri": obsidian_uri,
            "read_only": self.server_instance.read_only,
            "idle_remaining": max(0, int(IDLE_TIMEOUT_SECONDS - (time.time() - self.server_instance.last_interaction))),
        }
        self._send_headers(200)
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _get_auditor_stats(self):
        vault = self.server_instance.vault
        companion_dir = _find_companion_dir(vault)
        stats = {}
        limits = {"Threads.md": 8000, "Last-Session.md": 3000, "Kurallar.md": 3000, "Core.md": 1500, "Journal.md": 5000}
        for fname, limit in limits.items():
            fpath = companion_dir / fname
            if not fpath.is_file() and fname == "Kurallar.md":
                fpath = vault / "Kurallar.md"
            if fpath.is_file():
                length = len(fpath.read_text(encoding="utf-8", errors="replace"))
                pct = min(150, int((length / limit) * 100))
                stats[fname] = {
                    "chars": length,
                    "limit": limit,
                    "percent": pct,
                    "heavy": length > limit,
                    "status": "warning" if length > limit else ("caution" if pct > 75 else "healthy")
                }
            else:
                stats[fname] = {"chars": 0, "limit": limit, "percent": 0, "heavy": False, "status": "none"}
        return stats

    def _handle_live_stats(self):
        vault = self.server_instance.vault
        companion_dir = _find_companion_dir(vault)

        # 1. Doctor status calculation
        doctor_status = {"status": "ok", "errors": 0, "warnings": 0, "checks": []}
        try:
            if not (vault / "AGENTS.md").is_file():
                doctor_status["errors"] += 1
                doctor_status["checks"].append("AGENTS.md eksik")

            # Companion hygiene check
            for fname, limit in [("Threads.md", 8000), ("Last-Session.md", 3000), ("Kurallar.md", 3000)]:
                fpath = companion_dir / fname
                if not fpath.is_file() and fname == "Kurallar.md":
                    fpath = vault / "Kurallar.md"
                if fpath.is_file():
                    clen = len(fpath.read_text(encoding="utf-8", errors="replace"))
                    if clen > limit:
                        doctor_status["warnings"] += 1
                        doctor_status["checks"].append(f"{fname} limit aşımı ({clen}/{limit})")
            if doctor_status["errors"] > 0:
                doctor_status["status"] = "error"
            elif doctor_status["warnings"] > 0:
                doctor_status["status"] = "warning"
        except Exception:
            pass

        # 2. Token budgets
        auditor = self._get_auditor_stats()

        # 3. Counts
        project_count = len(list((vault / "projects").iterdir())) if (vault / "projects").is_dir() else 0
        receipt_count = len(list((vault / "receipts").glob("*.md"))) if (vault / "receipts").is_dir() else 0
        goal_count = len(list((vault / "goals").glob("*.md"))) if (vault / "goals").is_dir() else 0
        task_files_count = len(list((vault / "tasks").glob("*.md"))) if (vault / "tasks").is_dir() else 0

        # 4. Latest Receipts Stream
        recent_receipts = []
        receipts_dir = vault / "receipts"
        if receipts_dir.is_dir():
            rfiles = sorted(receipts_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:5]
            for rf in rfiles:
                try:
                    text = rf.read_text(encoding="utf-8", errors="replace")
                    summary = ""
                    project = "genel"
                    m_sum = re.search(r'"summary":\s*"([^"]+)"', text)
                    if m_sum: summary = m_sum.group(1)
                    m_proj = re.search(r'"project":\s*"([^"]+)"', text)
                    if m_proj: project = m_proj.group(1)
                    if not summary:
                        m_h = re.search(r"^#+\s+(.+)$", text, re.MULTILINE)
                        if m_h: summary = m_h.group(1).strip()
                    recent_receipts.append({
                        "name": rf.name,
                        "date": time.strftime("%d.%m %H:%M", time.localtime(rf.stat().st_mtime)),
                        "project": project,
                        "summary": summary or rf.stem
                    })
                except Exception:
                    pass

        # 5. Active Threads Summary
        active_threads_preview = []
        threads_file = companion_dir / "Threads.md"
        if threads_file.is_file():
            try:
                ttext = threads_file.read_text(encoding="utf-8", errors="replace")
                m_act = re.search(r"## Active Threads\s*([\s\S]*?)(?=\n## Closed Threads|\Z)", ttext)
                if m_act:
                    titles = re.findall(r"### ([^\n]+)", m_act.group(1))
                    active_threads_preview = [t.strip() for t in titles[:6]]
            except Exception:
                pass

        # Git telemetry
        git_info = {"branch": "main", "commit": "local", "clean": True}
        git_dir = vault / ".git"
        if git_dir.is_dir():
            head_file = git_dir / "HEAD"
            if head_file.is_file():
                try:
                    head_txt = head_file.read_text(encoding="utf-8", errors="replace").strip()
                    if head_txt.startswith("ref: refs/heads/"):
                        branch = head_txt.replace("ref: refs/heads/", "").strip()
                        git_info["branch"] = branch
                        ref_file = git_dir / "refs" / "heads" / branch
                        if ref_file.is_file():
                            git_info["commit"] = ref_file.read_text(encoding="utf-8", errors="replace").strip()[:7]
                except Exception:
                    pass

        # Real Last-Session Context
        last_session_info = self._get_last_session_info(vault)

        # Real Companion Info
        companion_info = {"name": "Atlas", "user": "Baki"}
        core_path = companion_dir / "Core.md"
        if core_path.is_file():
            try:
                core_txt = core_path.read_text(encoding="utf-8", errors="replace")
                m_u = re.search(r"-\s+\*\*Kullan[ıi]c[ıi]\s*:\*\*\s*([^\r\n]+)", core_txt)
                if m_u: companion_info["user"] = m_u.group(1).strip()
                m_a = re.search(r"-\s+\*\*Asistan\s*Ad[ıi]\s*:\*\*\s*([^\r\n]+)", core_txt)
                if m_a: companion_info["name"] = m_a.group(1).strip()
            except Exception:
                pass

        data = {
            "vault_name": vault.name,
            "read_only": self.server_instance.read_only,
            "doctor": doctor_status,
            "auditor": auditor,
            "counts": {
                "projects": project_count,
                "receipts": receipt_count,
                "goals": goal_count,
                "tasks_files": task_files_count
            },
            "recent_receipts": recent_receipts,
            "active_threads": active_threads_preview,
            "git_info": git_info,
            "last_session": last_session_info,
            "companion": companion_info
        }
        self._send_headers(200)
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _handle_auditor(self):
        stats = self._get_auditor_stats()
        vault = self.server_instance.vault
        companion_dir = _find_companion_dir(vault)

        # Recent sessions
        sessions = []
        last_session_file = companion_dir / "Last-Session.md"
        if last_session_file.is_file():
            try:
                stext = last_session_file.read_text(encoding="utf-8", errors="replace")
                sm = re.findall(r"## (\d{4}-\d{2}-\d{2}[^\n]+)\n([\s\S]*?)(?=\n## |\Z)", stext)
                for header, sbody in sm[:5]:
                    sessions.append({
                        "header": header.strip(),
                        "snippet": sbody.strip()[:300]
                    })
            except Exception:
                pass

        # Rules preview
        rules = []
        rules_file = companion_dir / "Kurallar.md"
        if not rules_file.is_file():
            rules_file = vault / "Kurallar.md"
        if rules_file.is_file():
            try:
                rtext = rules_file.read_text(encoding="utf-8", errors="replace")
                rlines = [l.strip() for l in rtext.splitlines() if l.strip().startswith("- **kural") or l.strip().startswith("- ")]
                rules = rlines[:10]
            except Exception:
                pass

        data = {
            "stats": stats,
            "sessions": sessions,
            "rules": rules,
            "companion_dir": str(companion_dir.relative_to(vault) if _is_safe_subpath(vault, companion_dir) else companion_dir.name)
        }
        self._send_headers(200)
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _parse_tasks(self, text: str, source_label: str):
        tasks = []
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            line_s = line.strip()
            if line_s.startswith("- [ ]") or line_s.startswith("- [x]"):
                done = line_s.startswith("- [x]")
                task_text = line_s[5:].strip()
                priority = "P2"
                if re.search(r"(?i)\bP0\b|kritik|urgent", task_text):
                    priority = "P0"
                elif re.search(r"(?i)\bP1\b|önemli|high", task_text):
                    priority = "P1"

                if done:
                    col = "done"
                elif priority == "P0" or re.search(r"(?i)aktif|devam|running|açık", task_text):
                    col = "in_progress"
                else:
                    col = "todo"

                tasks.append({
                    "id": f"{source_label}::line::{idx}",
                    "line_index": idx,
                    "text": task_text,
                    "done": done,
                    "priority": priority,
                    "column": col,
                    "source": source_label
                })
        return tasks

    def _handle_projects(self):
        vault = self.server_instance.vault
        projects_dir = vault / "projects"
        results = []

        # 1. Projects directory
        if projects_dir.is_dir():
            for pdir in sorted(projects_dir.iterdir()):
                if pdir.is_dir():
                    tasks_file = pdir / "tasks.md"
                    rules_file = pdir / "Kurallar.md"
                    readme_file = pdir / "README.md"
                    tasks = []
                    raw_content = ""

                    if tasks_file.is_file():
                        raw_content = tasks_file.read_text(encoding="utf-8", errors="replace")
                        tasks.extend(self._parse_tasks(raw_content, str(tasks_file.relative_to(vault))))
                    elif rules_file.is_file():
                        raw_content = rules_file.read_text(encoding="utf-8", errors="replace")
                    elif readme_file.is_file():
                        raw_content = readme_file.read_text(encoding="utf-8", errors="replace")

                    # Check matching task files in tasks/
                    tasks_dir = vault / "tasks"
                    if tasks_dir.is_dir():
                        for tf in tasks_dir.glob(f"{pdir.name}*.md"):
                            t_content = tf.read_text(encoding="utf-8", errors="replace")
                            tasks.extend(self._parse_tasks(t_content, str(tf.relative_to(vault))))
                            if not raw_content:
                                raw_content = t_content

                    results.append({
                        "name": pdir.name,
                        "path": str(pdir.relative_to(vault)),
                        "has_rules": rules_file.is_file(),
                        "task_count": len(tasks),
                        "done_count": sum(1 for t in tasks if t["done"]),
                        "tasks": tasks,
                        "raw_markdown": raw_content[:2000]
                    })

        # 2. General tasks in tasks/ not mapped
        tasks_dir = vault / "tasks"
        if tasks_dir.is_dir():
            unmapped_tasks = []
            for tf in sorted(tasks_dir.glob("*.md")):
                if not any(p["name"] in tf.stem for p in results):
                    t_content = tf.read_text(encoding="utf-8", errors="replace")
                    unmapped_tasks.extend(self._parse_tasks(t_content, str(tf.relative_to(vault))))
            if unmapped_tasks:
                results.append({
                    "name": "tasks (Genel)",
                    "path": "tasks/",
                    "has_rules": False,
                    "task_count": len(unmapped_tasks),
                    "done_count": sum(1 for t in unmapped_tasks if t["done"]),
                    "tasks": unmapped_tasks,
                    "raw_markdown": "# Genel Görevler\ntasks/ altındaki notlar"
                })

        # 3. Active Threads virtual project
        companion_dir = _find_companion_dir(vault)
        threads_file = companion_dir / "Threads.md"
        if threads_file.is_file():
            try:
                ttext = threads_file.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"## Active Threads\s*([\s\S]*?)(?=\n## Closed Threads|\Z)", ttext)
                if m:
                    thread_sections = re.findall(r"### ([^\n]+)\n([\s\S]*?)(?=\n### |\Z)", m.group(1))
                    threads_tasks = []
                    for title, body in thread_sections:
                        sub_t = self._parse_tasks(body, f"Threads ({title.strip()[:20]})")
                        if not sub_t:
                            threads_tasks.append({
                                "id": f"thread::{len(threads_tasks)}",
                                "line_index": 0,
                                "text": title.strip(),
                                "done": False,
                                "priority": "P0",
                                "column": "in_progress",
                                "source": "Threads.md"
                            })
                        else:
                            threads_tasks.extend(sub_t)

                    results.insert(0, {
                        "name": "Aktif Odaklar (Threads)",
                        "path": str(threads_file.relative_to(vault) if _is_safe_subpath(vault, threads_file) else threads_file.name),
                        "has_rules": False,
                        "task_count": len(threads_tasks),
                        "done_count": sum(1 for t in threads_tasks if t["done"]),
                        "tasks": threads_tasks,
                        "raw_markdown": ttext[:2000]
                    })
            except Exception:
                pass

        self._send_headers(200)
        self.wfile.write(json.dumps({"projects": results}, ensure_ascii=False).encode())

    def _handle_goals(self):
        vault = self.server_instance.vault
        goals_dir = vault / "goals"
        goals = []
        if goals_dir.is_dir():
            for gfile in sorted(goals_dir.glob("*.md")):
                try:
                    text = gfile.read_text(encoding="utf-8", errors="replace")
                    title = gfile.stem
                    m = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
                    if m: title = m.group(1).strip().strip("\"'")
                    progress = 0
                    pm = re.search(r"progress:\s*(\d+)", text)
                    if pm: progress = int(pm.group(1))
                    cat = "Odak"
                    cm = re.search(r"category:\s*(.+)$", text, re.MULTILINE)
                    if cm: cat = cm.group(1).strip().strip("\"'")
                    goals.append({
                        "name": gfile.name,
                        "title": title,
                        "category": cat,
                        "progress": progress,
                        "snippet": text[:300]
                    })
                except Exception:
                    pass

        # If goals directory is empty, synthesize goals from Active Threads
        if not goals:
            companion_dir = _find_companion_dir(vault)
            threads_file = companion_dir / "Threads.md"
            if threads_file.is_file():
                try:
                    ttext = threads_file.read_text(encoding="utf-8", errors="replace")
                    m = re.search(r"## Active Threads\s*([\s\S]*?)(?=\n## Closed Threads|\Z)", ttext)
                    if m:
                        thread_titles = re.findall(r"### ([^\n]+)", m.group(1))
                        for idx, ttitle in enumerate(thread_titles[:6]):
                            clean_t = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)", "", ttitle).strip()
                            progress = 85 if idx == 0 else (70 if idx == 1 else (50 if idx == 2 else 40))
                            cat = "Mühendislik" if "SQL" in clean_t or "PR" in clean_t else ("Gelişim" if "Atlas" in clean_t or "İmaj" in clean_t else "Proje")
                            goals.append({
                                "name": f"thread_goal_{idx}.md",
                                "title": clean_t,
                                "category": cat,
                                "progress": progress,
                                "snippet": f"Aktif Thread: {clean_t}"
                            })
                except Exception:
                    pass

        # Habit tracker default states
        habits = [
            {"id": "h1", "title": "Günlük İkinci Beyin Senkronizasyonu", "category": "Sistem", "streak": 14, "days": [True, True, True, True, True, True, True]},
            {"id": "h2", "title": "Derin Çalışma (Deep Work) 2x Blok", "category": "Üretim", "streak": 8, "days": [True, True, True, False, True, True, True]},
            {"id": "h3", "title": "Hafıza Hijyeni & Receipt Kontrolü", "category": "Hafıza", "streak": 21, "days": [True, True, True, True, True, True, True]},
            {"id": "h4", "title": "Fiziksel Antrenman & Biyomekanik", "category": "Sağlık", "streak": 5, "days": [True, False, True, True, False, True, True]},
        ]
        if hasattr(self.server_instance, "habits_state"):
            for h in habits:
                hid = h["id"]
                if hid in self.server_instance.habits_state:
                    for d_idx, d_val in self.server_instance.habits_state[hid].items():
                        if 0 <= d_idx < len(h["days"]):
                            h["days"][d_idx] = d_val

        self._send_headers(200)
        self.wfile.write(json.dumps({"goals": goals, "habits": habits}, ensure_ascii=False).encode())

    def _handle_save_goal(self, body):
        vault = self.server_instance.vault
        try:
            req = json.loads(body)
            filename = req.get("filename", "").strip()
            content = req.get("content", "")
            if not filename or not filename.endswith(".md"):
                self._send_headers(400)
                self.wfile.write(json.dumps({"error": "Geçersiz dosya adı (.md olmalı)"}).encode())
                return
            target_path = (vault / "goals" / filename).resolve()
            if not _is_safe_subpath(vault / "goals", target_path):
                self._send_headers(403)
                self.wfile.write(json.dumps({"error": "goals/ dışına yazma yasaktır"}).encode())
                return
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            self._send_headers(200)
            self.wfile.write(json.dumps({"status": "saved", "path": str(target_path.relative_to(vault))}).encode())
        except Exception as e:
            self._send_headers(500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _handle_toggle_task(self, body):
        vault = self.server_instance.vault
        try:
            req = json.loads(body)
            source_file = req.get("source_file", "").strip()
            line_index = int(req.get("line_index", -1))
            new_done = bool(req.get("done", False))

            if not source_file or line_index < 0:
                self._send_headers(400)
                self.wfile.write(json.dumps({"error": "Invalid source or line index"}).encode())
                return

            target_path = (vault / source_file).resolve()
            # Only allow toggling tasks in projects/ or tasks/
            is_valid_dir = _is_safe_subpath(vault / "projects", target_path) or _is_safe_subpath(vault / "tasks", target_path)
            if not is_valid_dir:
                self._send_headers(403)
                self.wfile.write(json.dumps({"error": "Yalnızca projects/ ve tasks/ dosyaları güncellenebilir"}).encode())
                return

            if not target_path.is_file():
                self._send_headers(404)
                self.wfile.write(json.dumps({"error": "Dosya bulunamadı"}).encode())
                return

            lines = target_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line_index >= len(lines):
                self._send_headers(400)
                self.wfile.write(json.dumps({"error": "Satır indeksi aralık dışı"}).encode())
                return

            orig_line = lines[line_index]
            if new_done and orig_line.strip().startswith("- [ ]"):
                lines[line_index] = orig_line.replace("- [ ]", "- [x]", 1)
            elif not new_done and orig_line.strip().startswith("- [x]"):
                lines[line_index] = orig_line.replace("- [x]", "- [ ]", 1)

            target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._send_headers(200)
            self.wfile.write(json.dumps({"status": "toggled", "line": lines[line_index]}).encode())
        except Exception as e:
            self._send_headers(500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _handle_receipts(self):
        vault = self.server_instance.vault
        receipts_dir = vault / "receipts"
        receipts = []
        if receipts_dir.is_dir():
            files = sorted(receipts_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:30]
            for f in files:
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    summary = ""
                    project = "genel"
                    m_sum = re.search(r'"summary":\s*"([^"]+)"', text)
                    if m_sum: summary = m_sum.group(1)
                    m_proj = re.search(r'"project":\s*"([^"]+)"', text)
                    if m_proj: project = m_proj.group(1)
                    if not summary:
                        m_h = re.search(r"^#+\s+(.+)$", text, re.MULTILINE)
                        if m_h: summary = m_h.group(1).strip()
                    receipts.append({
                        "name": f.name,
                        "mtime": f.stat().st_mtime,
                        "date_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
                        "summary": summary or f.stem,
                        "project": project,
                        "snippet": text[:400]
                    })
                except Exception:
                    pass
        self._send_headers(200)
        self.wfile.write(json.dumps({"receipts": receipts}, ensure_ascii=False).encode())

    def _handle_graph(self):
        vault = self.server_instance.vault
        nodes = []
        links = []
        node_ids = set()

        folders = [
            ("knowledge", "knowledge"),
            ("projects", "projects"),
            ("tasks", "tasks"),
        ]
        cdir = _find_companion_dir(vault)
        if cdir.is_dir():
            folders.append((str(cdir.relative_to(vault) if _is_safe_subpath(vault, cdir) else cdir.name), "companion"))

        for folder_rel, group in folders:
            fdir = vault / folder_rel
            if fdir.is_dir():
                for fpath in list(fdir.glob("**/*.md"))[:35]:
                    nid = fpath.stem
                    if nid not in node_ids:
                        node_ids.add(nid)
                        nodes.append({
                            "id": nid,
                            "title": nid.replace("-", " ").capitalize(),
                            "group": group,
                            "path": str(fpath.relative_to(vault))
                        })
                    try:
                        text = fpath.read_text(encoding="utf-8", errors="replace")
                        wikilinks = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", text)
                        for target in wikilinks:
                            ts = target.strip()
                            if ts not in node_ids:
                                node_ids.add(ts)
                                nodes.append({
                                    "id": ts,
                                    "title": ts.replace("-", " ").capitalize(),
                                    "group": "linked",
                                    "path": ""
                                })
                            links.append({"source": nid, "target": ts})
                    except Exception:
                        pass

        self._send_headers(200)
        self.wfile.write(json.dumps({"nodes": nodes, "links": links}, ensure_ascii=False).encode())


    def _get_last_session_info(self, vault):
        companion_dir = _find_companion_dir(vault)
        ls_path = companion_dir / "Last-Session.md"
        if not ls_path.is_file():
            return None
        try:
            content = ls_path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^##\s+([^\r\n]+)", content, re.MULTILINE)
            header = match.group(1).strip() if match else "Aktif Oturum"
            req_match = re.search(r"-\s+\*\*İstek\s*\([^)]*\)\s*:\*\*\s*([^\r\n]+)", content)
            request = req_match.group(1).strip() if req_match else ""
            decisions = []
            for d in re.findall(r"\d+\.\s+\*\*([^*]+?)\*?:?\*\*:?\s*([^\r\n]+)", content):
                decisions.append(f"{d[0].strip()}: {d[1].strip()}")
            if not decisions:
                for d in re.findall(r"-\s+\*\*([^*]+?)\*?:?\*\*:?\s*([^\r\n]+)", content):
                    if "İstek" not in d[0] and "Kaynak" not in d[0]:
                        decisions.append(f"{d[0].strip()}: {d[1].strip()}")
            wikilinks = re.findall(r"\[\[([^\]]+)\]\]", content)
            md_links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", content)
            return {
                "header": header,
                "request": request,
                "decisions": decisions[:5],
                "wikilinks": list(dict.fromkeys(wikilinks))[:8],
                "links": [{"title": m[0], "url": m[1]} for m in md_links][:6]
            }
        except Exception:
            return None

    def _handle_search(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        q = qs.get("q", [""])[0].strip()
        vault = self.server_instance.vault
        if not q or len(q) < 2:
            self._send_headers(200)
            self.wfile.write(json.dumps({"results": []}).encode())
            return

        results = []
        q_lower = q.lower()
        companion_dir = _find_companion_dir(vault)
        search_dirs = [vault / "projects", vault / "knowledge", companion_dir, vault / "tasks", vault / "receipts", vault / "goals"]

        for sdir in search_dirs:
            if not sdir or not sdir.is_dir():
                continue
            for f in sdir.rglob("*.md"):
                if not f.is_file():
                    continue
                try:
                    rel_p = str(f.relative_to(vault)).replace("\\", "/")
                except Exception:
                    rel_p = f.name
                
                # Title match
                if q_lower in f.stem.lower():
                    results.append({
                        "title": f.stem,
                        "path": rel_p,
                        "category": sdir.name,
                        "match_type": "title",
                        "snippet": f"Not başlığı: {f.name}"
                    })
                    if len(results) >= 25:
                        break
                    continue

                # Content match
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    idx = text.lower().find(q_lower)
                    if idx != -1:
                        start = max(0, idx - 40)
                        end = min(len(text), idx + 80)
                        snip = ("..." if start > 0 else "") + text[start:end].replace("\n", " ").strip() + ("..." if end < len(text) else "")
                        results.append({
                            "title": f.stem,
                            "path": rel_p,
                            "category": sdir.name,
                            "match_type": "content",
                            "snippet": snip
                        })
                        if len(results) >= 25:
                            break
                except Exception:
                    pass
            if len(results) >= 25:
                break

        self._send_headers(200)
        self.wfile.write(json.dumps({"results": results}, ensure_ascii=False).encode())

    def _handle_file(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        rel_path = qs.get("path", [""])[0].strip()
        vault = self.server_instance.vault

        if not rel_path:
            self._send_headers(400)
            self.wfile.write(json.dumps({"error": "Path parameter required"}).encode())
            return

        target_path = (vault / rel_path).resolve()
        if not _is_safe_subpath(vault, target_path) or not target_path.is_file():
            self._send_headers(404)
            self.wfile.write(json.dumps({"error": "Dosya bulunamadı veya erişim engellendi"}).encode())
            return

        if target_path.suffix.lower() not in (".md", ".txt", ".json"):
            self._send_headers(403)
            self.wfile.write(json.dumps({"error": "Yalnızca metin dosyaları önizlenebilir"}).encode())
            return

        try:
            content = target_path.read_text(encoding="utf-8", errors="replace")
            self._send_headers(200)
            self.wfile.write(json.dumps({
                "path": str(target_path.relative_to(vault)).replace("\\", "/"),
                "name": target_path.name,
                "content": content
            }, ensure_ascii=False).encode())
        except Exception as e:
            self._send_headers(500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _handle_add_task(self, body):
        vault = self.server_instance.vault
        try:
            req = json.loads(body)
            project_name = req.get("project_name", "").strip()
            task_text = req.get("text", "").strip()
            priority = req.get("priority", "p1").strip().upper()
            if not task_text:
                self._send_headers(400)
                self.wfile.write(json.dumps({"error": "Görev metni boş olamaz"}).encode())
                return

            target_path = None
            if project_name:
                pdir = vault / "projects" / project_name
                if pdir.is_dir():
                    target_path = pdir / "tasks.md"
            if not target_path:
                target_path = vault / "tasks" / f"{project_name or 'genel'}.md"

            if not _is_safe_subpath(vault / "projects", target_path) and not _is_safe_subpath(vault / "tasks", target_path):
                self._send_headers(403)
                self.wfile.write(json.dumps({"error": "Yetkisiz dosya konumu"}).encode())
                return

            target_path.parent.mkdir(parents=True, exist_ok=True)
            task_line = f"- [ ] #{priority.lower()} {task_text}\n"
            if target_path.is_file():
                existing = target_path.read_text(encoding="utf-8", errors="replace")
                if not existing.endswith("\n"):
                    existing += "\n"
                existing += task_line
            else:
                existing = f"# {project_name.capitalize()} Görevleri\n\n{task_line}"

            target_path.write_text(existing, encoding="utf-8")
            self._send_headers(200)
            self.wfile.write(json.dumps({
                "status": "created",
                "path": str(target_path.relative_to(vault)).replace("\\", "/"),
                "line": task_line.strip()
            }, ensure_ascii=False).encode())
        except Exception as e:
            self._send_headers(500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _handle_toggle_habit(self, body):
        try:
            req = json.loads(body)
            habit_id = req.get("id", "")
            day_index = int(req.get("day", -1))
            val = bool(req.get("value", False))
            if not habit_id or day_index < 0 or day_index > 6:
                self._send_headers(400)
                self.wfile.write(json.dumps({"error": "Geçersiz habit veya gün indeksi"}).encode())
                return
            if not hasattr(self.server_instance, "habits_state"):
                self.server_instance.habits_state = {}
            if habit_id not in self.server_instance.habits_state:
                self.server_instance.habits_state[habit_id] = {}
            self.server_instance.habits_state[habit_id][day_index] = val
            self._send_headers(200)
            self.wfile.write(json.dumps({"status": "updated", "id": habit_id, "day": day_index, "value": val}).encode())
        except Exception as e:
            self._send_headers(500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _handle_action_compact(self):
        vault = self.server_instance.vault
        stats = self._get_auditor_stats()
        needs_compact = any(s.get("heavy") for s in stats.values())

        # Safe compact execution
        try:
            from beyin_v3_compact import compact_file
            companion_dir = _find_companion_dir(vault)
            threads = companion_dir / "Threads.md"
            last_session = companion_dir / "Last-Session.md"
            compacted = []
            if threads.is_file() and len(threads.read_text(encoding="utf-8", errors="replace")) > 8000:
                compact_file(threads, vault)
                compacted.append("Threads.md")
            if last_session.is_file() and len(last_session.read_text(encoding="utf-8", errors="replace")) > 3000:
                compact_file(last_session, vault)
                compacted.append("Last-Session.md")

            new_stats = self._get_auditor_stats()
            self._send_headers(200)
            self.wfile.write(json.dumps({
                "status": "compacted" if compacted else "already_optimal",
                "compacted_files": compacted,
                "stats_after": new_stats
            }, ensure_ascii=False).encode())
            return
        except Exception as e:
            self._send_headers(200)
            self.wfile.write(json.dumps({
                "status": "notice",
                "message": f"Compact tavsiyesi: Terminalde `beyin.py companion-compact` çalıştırabilirsiniz ({e})",
                "stats": stats
            }, ensure_ascii=False).encode())

    def _get_unlock_html(self) -> str:
        return f"""<!DOCTYPE html>
<html class="dark" lang="tr">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>AvenoxBeyin Portal - Kilit Açma</title>
  <style>
    body {{ background: #0a0e14; color: #dfe2eb; font-family: system-ui, -apple-system, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 2rem; width: 100%; max-width: 400px; box-shadow: 0 16px 36px rgba(0,0,0,0.85); text-align: center; }}
    .logo {{ width: 64px; height: 64px; margin: 0 auto 1rem; }}
    h1 {{ color: #f9d71c; font-size: 1.25rem; margin: 0 0 0.5rem; }}
    p {{ color: #8b949e; font-size: 0.875rem; margin: 0 0 1.5rem; }}
    input {{ width: 100%; box-sizing: border-box; background: #0a0e14; border: 1px solid #30363d; border-radius: 4px; padding: 0.75rem; color: #dfe2eb; font-family: monospace; font-size: 0.875rem; margin-bottom: 1rem; }}
    input:focus {{ outline: none; border-color: #f9d71c; box-shadow: 0 0 8px rgba(249, 215, 28, 0.35); }}
    button {{ width: 100%; background: #f9d71c; color: #0a0e14; font-weight: 600; border: none; border-radius: 4px; padding: 0.75rem; cursor: pointer; transition: all 0.2s; }}
    button:hover {{ background: #ffe25a; box-shadow: 0 0 12px rgba(249, 215, 28, 0.35); }}
  </style>
</head>
<body>
  <div class="card">
    <img src="/avenox_logo.png" class="logo" alt="Avenox"/>
    <h1>Obsidian Citrus Auth</h1>
    <p>Yerel vault güvenliği için oturum anahtarınızı girin</p>
    <form method="GET" action="/">
      <input type="password" name="token" placeholder="Oturum Belirteci (Token)" required autofocus/>
      <button type="submit">Kilidi Aç</button>
    </form>
  </div>
</body>
</html>"""


class BeyinWebServer(HTTPServer):
    def __init__(self, host, port, vault, state, web_dir, read_only=False):
        self.host = host
        self.port = port
        self.vault = Path(vault).resolve()
        self.state = Path(state).resolve()
        self.web_dir = Path(web_dir).resolve()
        self.read_only = read_only
        self.session_token = secrets.token_urlsafe(32)
        self.last_interaction = time.time()
        self.running_lock_path = None
        self.habits_state = {}
        super().__init__((host, port), BeyinWebHandler)

    def touch_interaction(self):
        self.last_interaction = time.time()

    def is_idle_expired(self) -> bool:
        return (time.time() - self.last_interaction) > IDLE_TIMEOUT_SECONDS


def find_free_port(host=DEFAULT_HOST, preferred=None):
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, preferred)) != 0:
                return preferred
    for p in range(PORT_RANGE[0], PORT_RANGE[1] + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, p)) != 0:
                return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def write_lock_file(server: BeyinWebServer):
    lock_dir = server.state / "web" / "running"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{server.port}.json"
    data = {
        "schema": 1,
        "port": server.port,
        "pid": os.getpid(),
        "vault": str(server.vault),
        "session": server.session_token,
        "read_only": server.read_only,
        "started_at": int(time.time()),
    }
    lock_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    server.running_lock_path = lock_path
    return lock_path


def remove_lock_file(server: BeyinWebServer):
    if server.running_lock_path and server.running_lock_path.is_file():
        try:
            server.running_lock_path.unlink()
        except OSError:
            pass


def get_running_server(vault: Path, state: Path):
    lock_dir = state / "web" / "running"
    if not lock_dir.is_dir():
        return None
    for lfile in sorted(lock_dir.glob("*.json")):
        try:
            data = json.loads(lfile.read_text(encoding="utf-8"))
            if data.get("vault") == str(vault.resolve()):
                port = data.get("port")
                pid = data.get("pid")
                # Test connectivity
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/api/v1/pulse", headers={
                    "Host": f"127.0.0.1:{port}",
                    "Cookie": f"bw_session_{port}={data.get('session')}"
                })
                res = conn.getresponse()
                if res.status == 200:
                    conn.close()
                    return data
                conn.close()
        except Exception:
            try:
                lfile.unlink()
            except OSError:
                pass
    return None


def run_server(vault: Path, state: Path, port=None, read_only=False, open_browser=True):
    vault = Path(vault).resolve()
    state = Path(state).resolve()

    existing = get_running_server(vault, state)
    if existing:
        port = existing["port"]
        url = f"http://127.0.0.1:{port}/?token={existing['session']}"
        print(f"Sunucu zaten çalışıyor: {url}")
        if open_browser:
            webbrowser.open(url)
        return 0

    actual_port = find_free_port(preferred=port)
    web_dir = _find_web_dir(vault)

    server = BeyinWebServer(DEFAULT_HOST, actual_port, vault, state, web_dir, read_only=read_only)
    write_lock_file(server)

    url = f"http://127.0.0.1:{actual_port}/?token={server.session_token}"
    print(f"\n=======================================================")
    print(f"  [Beyin] AvenoxBeyin Web Portal (Obsidian Citrus Cyber)")
    print(f"  Adres:     {url}")
    print(f"  Vault:     {vault}")
    print(f"  Mod:       {'Salt-Okunur (Read-Only)' if read_only else 'Normal (Yazma İzinli)'}")
    print(f"  Kapatmak:  Ctrl + C")
    print(f"=======================================================\n")

    if open_browser:
        webbrowser.open(url)

    # Idle watchdog thread
    def idle_checker():
        while True:
            time.sleep(10)
            if server.is_idle_expired():
                print("\n[Otomatik Kapanma] 30 dakika kullanıcı etkileşimi olmadı, sunucu kapatılıyor...")
                threading.Thread(target=server.shutdown).start()
                break

    watchdog = threading.Thread(target=idle_checker, daemon=True)
    watchdog.start()

    def handle_signal(sig, frame):
        print("\nSunucu kapatılıyor...")
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        server.serve_forever()
    finally:
        remove_lock_file(server)
        server.server_close()
        print("Sunucu kapandı.")

    return 0


def status_server(vault: Path, state: Path):
    existing = get_running_server(vault, state)
    if existing:
        return {
            "status": "calisiyor",
            "port": existing["port"],
            "pid": existing["pid"],
            "url": f"http://127.0.0.1:{existing['port']}/",
            "read_only": existing.get("read_only", False)
        }
    return {"status": "calismiyor"}


def stop_server(vault: Path, state: Path):
    existing = get_running_server(vault, state)
    if not existing:
        print("Çalışan web sunucusu bulunamadı.")
        return 0
    pid = existing["pid"]
    port = existing["port"]
    print(f"Sunucu durduruluyor (PID: {pid}, Port: {port})...")
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        print("Durduruldu.")
    except Exception as e:
        print(f"Durdurulamadı: {e}")
    # clean lock
    lpath = state / "web" / "running" / f"{port}.json"
    if lpath.is_file():
        lpath.unlink()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="beyin.py web", description="AvenoxBeyin Web Portali")
    sub = parser.add_subparsers(dest="command")

    parser_run = sub.add_parser("ac", help="Web portalını başlat ve tarayıcıda aç")
    parser_run.add_argument("--port", type=int, help="Tercih edilen port (varsayılan: 7373)")
    parser_run.add_argument("--read-only", action="store_true", help="Salt-okunur modda çalıştır (tüm yazmalar reddedilir)")
    parser_run.add_argument("--no-browser", action="store_true", help="Tarayıcıyı otomatik açma")
    parser_run.add_argument("--vault", type=str, help="Vault klasörü (varsayılan: geçerli dizin)")

    parser_status = sub.add_parser("durum", help="Web portalının çalışma durumunu göster")
    parser_status.add_argument("--vault", type=str, help="Vault klasörü")

    parser_stop = sub.add_parser("durdur", help="Çalışan web portalını durdur")
    parser_stop.add_argument("--vault", type=str, help="Vault klasörü")

    args = parser.parse_args(argv)

    vault_path = Path(args.vault if hasattr(args, "vault") and args.vault else ".").resolve()
    state_path = _resolve_state(vault_path)

    cmd = args.command or "ac"
    if cmd == "ac":
        return run_server(
            vault_path,
            state_path,
            port=args.port,
            read_only=args.read_only,
            open_browser=not args.no_browser
        )
    elif cmd == "durum":
        st = status_server(vault_path, state_path)
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return 0
    elif cmd == "durdur":
        return stop_server(vault_path, state_path)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
