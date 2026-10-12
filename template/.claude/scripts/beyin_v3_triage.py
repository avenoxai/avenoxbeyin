"""Triage and digestion routines: inbox scanning, stale tasks, and weekly knowledge digest."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

INBOX_DIRS = ("000-Inbox", "inbox", "sources", "Inbox")
TASK_DIRS = ("tasks", "Tasks", "500-Tasks")
KNOWLEDGE_DIRS = ("knowledge", "🧠 500-Knowledge", "decisions", "Decisions")
COMPANION_DIRS = ("🔮 850-Companion", "Companion", "850-Companion")


def _is_safe_file(path: Path) -> bool:
    name = path.name
    if name.startswith(".") or name.endswith("~") or name == ".gitkeep":
        return False
    return path.is_file() and not path.is_symlink()


def _file_age_days(path: Path, now: float) -> float:
    try:
        mtime = path.stat().st_mtime
        return max(0.0, (now - mtime) / 86400.0)
    except OSError:
        return 0.0


def _parse_task_status(content: str) -> tuple[str, str]:
    """Extract (status, title) from a task markdown file."""
    status = "open"
    title = ""
    for line in content.splitlines():
        line_strip = line.strip()
        if not title and line_strip.startswith("#"):
            title = line_strip.lstrip("#").strip()
        m_status = re.match(r"(?i)^status:\s*(\w+)", line_strip)
        if m_status:
            status = m_status.group(1).lower()
        if line_strip.startswith("- [x]") or line_strip.startswith("- [X]"):
            status = "completed"
    return status, title


def scan_inbox(vault: Path, now: float) -> dict:
    items = []
    visited_folders = set()
    for d_name in INBOX_DIRS:
        folder = vault / d_name
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            resolved = os.path.normcase(str(folder.resolve()))
        except OSError:
            continue
        if resolved in visited_folders:
            continue
        visited_folders.add(resolved)
        try:
            for root, _, files in os.walk(folder):
                for f in files:
                    file_path = Path(root) / f
                    if not _is_safe_file(file_path):
                        continue
                    age = _file_age_days(file_path, now)
                    rel = file_path.relative_to(vault).as_posix()
                    ext = file_path.suffix.lower()
                    kind = "note" if ext in (".md", ".txt") else "data" if ext in (".json", ".csv") else "media" if ext in (".png", ".jpg", ".mp4", ".pdf") else "file"
                    items.append({
                        "path": rel,
                        "name": f,
                        "kind": kind,
                        "age_days": round(age, 1),
                        "mtime": file_path.stat().st_mtime
                    })
        except OSError:
            pass

    items.sort(key=lambda x: x["age_days"], reverse=True)
    oldest = items[0] if items else None
    return {
        "count": len(items),
        "items": items,
        "oldest": oldest,
    }


def scan_stale_tasks(vault: Path, now: float, stale_days: int = 14) -> dict:
    open_tasks = []
    stale_tasks = []
    visited_folders = set()
    for d_name in TASK_DIRS:
        folder = vault / d_name
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            resolved = os.path.normcase(str(folder.resolve()))
        except OSError:
            continue
        if resolved in visited_folders:
            continue
        visited_folders.add(resolved)
        try:
            for root, _, files in os.walk(folder):
                for f in files:
                    file_path = Path(root) / f
                    if not _is_safe_file(file_path) or file_path.suffix.lower() != ".md":
                        continue
                    try:
                        text = file_path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    status, title = _parse_task_status(text)
                    if status in ("done", "completed", "archived", "closed", "cancelled"):
                        continue
                    age = _file_age_days(file_path, now)
                    rel = file_path.relative_to(vault).as_posix()
                    task_info = {
                        "path": rel,
                        "title": title or f,
                        "status": status,
                        "age_days": round(age, 1),
                    }
                    open_tasks.append(task_info)
                    if age >= stale_days:
                        stale_tasks.append(task_info)
        except OSError:
            pass

    stale_tasks.sort(key=lambda x: x["age_days"], reverse=True)
    return {
        "open_count": len(open_tasks),
        "stale_count": len(stale_tasks),
        "stale_days_threshold": stale_days,
        "stale_tasks": stale_tasks,
    }


def scan_knowledge_digest(vault: Path, now: float, digest_days: int = 7) -> dict:
    recent_items = []
    visited_folders = set()
    for d_name in KNOWLEDGE_DIRS:
        folder = vault / d_name
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            resolved = os.path.normcase(str(folder.resolve()))
        except OSError:
            continue
        if resolved in visited_folders:
            continue
        visited_folders.add(resolved)
        try:
            for root, _, files in os.walk(folder):
                for f in files:
                    file_path = Path(root) / f
                    if not _is_safe_file(file_path) or file_path.suffix.lower() != ".md":
                        continue
                    age = _file_age_days(file_path, now)
                    if age <= digest_days:
                        rel = file_path.relative_to(vault).as_posix()
                        title = f.replace(".md", "")
                        try:
                            with file_path.open(encoding="utf-8") as f_obj:
                                first_line = f_obj.readline().strip()
                            if first_line.startswith("#"):
                                title = first_line.lstrip("#").strip()
                        except Exception:
                            pass
                        recent_items.append({
                            "path": rel,
                            "title": title,
                            "age_days": round(age, 1),
                        })
        except OSError:
            pass

    outcomes_file = vault / "outcomes.md"
    if outcomes_file.is_file() and _file_age_days(outcomes_file, now) <= digest_days:
        recent_items.append({
            "path": "outcomes.md",
            "title": "Kazanımlar ve Sonuçlar",
            "age_days": round(_file_age_days(outcomes_file, now), 1),
        })

    recent_items.sort(key=lambda x: x["age_days"])
    return {
        "digest_days": digest_days,
        "count": len(recent_items),
        "items": recent_items,
    }


def scan_companion(vault: Path) -> dict:
    info = {"rules_count": 0, "active_threads": 0, "last_session": None}
    comp_dir = None
    for d in COMPANION_DIRS:
        c = vault / d
        if c.is_dir() and not c.is_symlink():
            comp_dir = c
            break
    if not comp_dir:
        return info

    rules_file = comp_dir / "Kurallar.md"
    if rules_file.is_file():
        try:
            text = rules_file.read_text(encoding="utf-8", errors="replace")
            rule_count = sum(1 for line in text.splitlines() if re.match(r"^(?:[-*+]|\d+[.)])[ \t]", line))
            info["rules_count"] = rule_count
        except OSError:
            pass

    threads_file = comp_dir / "Threads.md"
    if threads_file.is_file():
        try:
            text = threads_file.read_text(encoding="utf-8", errors="replace")
            active_count = len(re.findall(r"(?i)\*\*status:\*\*\s*active", text))
            info["active_threads"] = active_count
        except OSError:
            pass

    session_file = comp_dir / "Last-Session.md"
    if session_file.is_file():
        try:
            text = session_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"##\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text)
            if m:
                info["last_session"] = m.group(1)
        except OSError:
            pass

    return info


def generate_recommendations(inbox: dict, tasks: dict, digest: dict, companion: dict) -> list[str]:
    recs = []
    if inbox["count"] > 0:
        recs.append(f"Gelen kutusunda bekleyen {inbox['count']} kaynak var. Kalıcı bilgi notlarına dönüştürmek için 'beyin-yakala' kullanın.")
    if tasks["stale_count"] > 0:
        recs.append(f"{tasks['stale_count']} görev {tasks['stale_days_threshold']} günden uzun süredir hareketsiz. Bu görevleri tamamlayın, kapatın veya güncelleyin.")
    if companion.get("active_threads", 0) > 4:
        recs.append(f"Threads.md içinde {companion['active_threads']} aktif konu var. Tamamlananları 'beyin.py companion-compact' ile arşivleyin.")
    if digest["count"] == 0:
        recs.append(f"Son {digest['digest_days']} günde yeni bilgi/karar notu üretilmedi. Öğrenilenleri knowledge/ altına damıtın.")
    else:
        recs.append(f"Son {digest['digest_days']} günde {digest['count']} bilgi notu üretildi/güncellendi; ritim sağlıklı.")
    return recs


def triage(vault: Path, state: Path | None = None, stale_days: int = 14, digest_days: int = 7, now: float | None = None) -> dict:
    now_ts = time.time() if now is None else now
    vault = Path(vault)
    inbox_report = scan_inbox(vault, now_ts)
    tasks_report = scan_stale_tasks(vault, now_ts, stale_days=stale_days)
    digest_report = scan_knowledge_digest(vault, now_ts, digest_days=digest_days)
    companion_report = scan_companion(vault)
    recs = generate_recommendations(inbox_report, tasks_report, digest_report, companion_report)

    return {
        "schema": 1,
        "vault": str(vault),
        "generated_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "inbox": inbox_report,
        "tasks": tasks_report,
        "digest": digest_report,
        "companion": companion_report,
        "recommendations": recs,
    }


def format_human(report: dict) -> str:
    lines = ["=== İkinci Beyin Sindirim ve Durum Raporu (Triage) ===\n"]
    inbox = report.get("inbox", {})
    lines.append(f"[Gelen Kutusu / Yakalananlar]")
    lines.append(f"  Bekleyen kaynak: {inbox.get('count', 0)}")
    oldest = inbox.get("oldest")
    if oldest:
        lines.append(f"  En eski: {oldest.get('path')} ({oldest.get('age_days', 0)} gun once)")
    lines.append("")

    tasks = report.get("tasks", {})
    stale_count = tasks.get("stale_count", 0)
    stale_thresh = tasks.get("stale_days_threshold", 14)
    lines.append(f"[Hareketsiz Gorevler] (>{stale_thresh} gun)")
    lines.append(f"  Acik gorev: {tasks.get('open_count', 0)}, Hareketsiz: {stale_count}")
    for t in tasks.get("stale_tasks", [])[:5]:
        lines.append(f"  - {t.get('path')}: {t.get('title')} ({t.get('age_days', 0)} gun once)")
    if stale_count > 5:
        lines.append(f"  ... ve {stale_count - 5} gorev daha")
    lines.append("")

    digest = report.get("digest", {})
    digest_days = digest.get("digest_days", 7)
    digest_count = digest.get("count", 0)
    lines.append(f"[Bilgi ve Karar Ozeti] (son {digest_days} gun)")
    lines.append(f"  Uretilen/guncellenen not: {digest_count}")
    for k in digest.get("items", [])[:5]:
        lines.append(f"  - {k.get('path')}: {k.get('title')}")
    if digest_count > 5:
        lines.append(f"  ... ve {digest_count - 5} not daha")
    lines.append("")

    comp = report.get("companion", {})
    lines.append("[Eslikci ve Akis]")
    lines.append(f"  Kurallar: {comp.get('rules_count', 0)} kural")
    lines.append(f"  Acik is parcaciklari: {comp.get('active_threads', 0)} thread")
    if comp.get("last_session"):
        lines.append(f"  Son oturum: {comp.get('last_session')}")
    lines.append("")

    recs = report.get("recommendations", [])
    if recs:
        lines.append("[Sindirim Onerileri]")
        for i, r in enumerate(recs, 1):
            lines.append(f"  {i}. {r}")

    return "\n".join(lines)
