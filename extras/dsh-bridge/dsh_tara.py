#!/usr/bin/env python3
"""DSH oturum kayitlarindan son N gundeki ana oturumlari listeler (telafi taramasi).

Kullanim:
    python3 dsh_tara.py --days 7
    python3 dsh_tara.py --days 7 --projects proje-a,proje-b
    python3 dsh_tara.py --days 7 --json

Yalnizca okur: `~/.dsh/sessions` altindaki `session.v4.jsonl.zstd` dosyalarini acar,
hicbir sey yazmaz. Subagent oturumlari listelenmez. `zstd` komutu gerekir.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

SESSIONS = pathlib.Path.home() / ".dsh" / "sessions"
SKIP_PREFIX = (
    "Current runtime context",
    "Time sampled",
    "<system-reminder>",
    "<available_skills>",
    "The approval policy changed",
    "The user changed the approval policy",
)


def project_of(folder: str) -> str:
    """Kodlanmis klasor adindan okunabilir bir proje kimligi cikarir."""
    name = folder.strip("-")
    # ...-projects-<proje>-- kalibi yaygin; yoksa klasor adini oldugu gibi kullan.
    marker = "-projects-"
    if marker in name:
        return name.split(marker, 1)[1]
    return name


def text_of(message: dict) -> str:
    return "\n".join(
        part["text"]
        for part in (message.get("content") or [])
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    ).strip()


def real_user(text: str) -> bool:
    if not text or len(text) < 4:
        return False
    if text.startswith(SKIP_PREFIX):
        return False
    return not re.match(r"^Agent [0-9a-f-]{20,} sent a message:", text)


def clip(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


def read_session(path: pathlib.Path) -> dict:
    raw = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, timeout=180).stdout
    meta: dict = {}
    title = None
    users: list[str] = []
    last_assistant = ""
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "session":
            meta = event
        elif kind == "session/title":
            title = data.get("title") or title
        elif kind == "user/message":
            text = text_of(data.get("message") or data)
            if real_user(text):
                users.append(clip(text, 300))
        elif kind == "assistant/message":
            text = text_of(data.get("message") or {})
            if text:
                last_assistant = clip(text, 400)
    created = meta.get("createdAt")
    when = dt.datetime.fromtimestamp(created / 1000) if created else None
    return {
        "date": when.strftime("%Y-%m-%d %H:%M") if when else "?",
        "created": created,
        "id": meta.get("id") or path.parent.name,
        "origin": meta.get("origin"),
        "depth": meta.get("delegationDepth") or 0,
        "turns": len(users),
        "title": title,
        "first": users[0] if users else "",
        "last_assistant": last_assistant,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--projects", default="", help="virgulle ayrilmis proje kimlikleri; bos ise hepsi")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    wanted = {item.strip() for item in args.projects.split(",") if item.strip()}
    cutoff = dt.datetime.now() - dt.timedelta(days=args.days)
    rows = []

    if not SESSIONS.is_dir():
        print("DSH oturum dizini bulunamadi: ~/.dsh/sessions", file=sys.stderr)
        return 1

    for folder in sorted(SESSIONS.iterdir()):
        if not folder.is_dir():
            continue
        project = project_of(folder.name)
        if wanted and project not in wanted:
            continue
        for session in sorted(folder.iterdir()):
            path = session / "session.v4.jsonl.zstd"
            if not path.exists():
                continue
            try:
                record = read_session(path)
            except Exception as exc:  # bozuk/acilamayan oturum atlanir
                print(f"uyari: {session.name}: {exc}", file=sys.stderr)
                continue
            if record["origin"] == "subagent" or record["depth"] > 0 or record["turns"] == 0:
                continue
            created = record.get("created")
            if created and dt.datetime.fromtimestamp(created / 1000) < cutoff:
                continue
            record["project"] = project
            rows.append(record)

    rows.sort(key=lambda row: row.get("created") or 0)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    if not rows:
        print(f"Son {args.days} gunde ana oturum yok.")
        return 0

    print(f"Son {args.days} gunde {len(rows)} oturum:\n")
    for record in rows:
        print(f"- {record['date']} · {record['project']} · {record['id'][-12:][:8]} · {record['title'] or '(basliksiz)'}")
        print(f"  istek: {record['first'][:140]}")
        print(f"  son:   {record['last_assistant'][:160]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
