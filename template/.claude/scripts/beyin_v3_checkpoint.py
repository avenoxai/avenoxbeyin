"""Unified session checkpoint handoff CLI command (Issue #767).

Orchestrates companion compaction, Last-Session card update, Threads update,
learning candidate staging, receipt submission, and optional chat-trace.
Standard library only.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path

from beyin_v3_companion import directory as locate_companion
from beyin_v3_compact import compact as companion_compact
from beyin_v3_notes import read as note_read, apply as note_apply
from beyin_v3_learning import stage_candidate
import beyin_v3_chat_trace

def _step_key(session, step, *content):
    data = json.dumps([session, step, *content], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _update_note(sync, source, previous, updated):
    if updated == previous["content"]:
        return "unchanged"
    request = {"source": source, "expected_sha256": previous["sha256"]}
    if previous["content"] is None:
        request["content"] = updated
    else:
        request["edits"] = [{"old": previous["content"], "new": updated}]
    return note_apply(sync, request)["status"]


def _update_card(sync, source, card, session, now):
    label = (card.get("label") or card.get("title") or "oturum").strip() or "oturum"
    if label.splitlines() != [label]:
        raise ValueError("checkpoint card label must be one line")
    body = card.get("body", "").strip().replace("\r\n", "\n").replace("\r", "\n")
    revision = _step_key(session, "card", label, body)
    # An anonymous handoff can replay its own text but has no session whose card it can replace.
    owner = _step_key(session, "card-owner") if session else revision
    previous = note_read(sync, source)
    content = previous["content"] or ""
    newline = "\r\n" if "\r\n" in content else "\n"
    short = session[:8] if session and re.fullmatch(r"[a-zA-Z0-9_-]+", session) else owner[:8]
    suffix = f" · {label} · {short} <!-- beyin-checkpoint:{owner}:{revision} -->"
    end = f"<!-- /beyin-checkpoint:{owner} -->"
    card_text = (f"## {now:%Y-%m-%d %H:%M}{suffix}" + newline
                 + (body.replace("\n", newline) + newline if body else "") + end + newline)
    opening = re.search(rf"^## [^\r\n]+ <!-- beyin-checkpoint:{owner}:([a-f0-9]{{64}}) -->\r?$",
                        content, re.MULTILINE)
    if opening:
        ending = re.search(rf"^{re.escape(end)}\r?\n?", content[opening.end():], re.MULTILINE)
        if not ending:
            raise ValueError("checkpoint card end marker missing")
        if opening.group(1) == revision:
            return "unchanged"
        updated = content[:opening.start()] + card_text + content[opening.end() + ending.end():]
    else:
        # A legacy eight-character suffix cannot prove ownership. Preserve those cards.
        first = re.search(r"^## ", content, re.MULTILINE)
        start = first.start() if first else len(content)
        preamble = content[:start]
        if preamble and not preamble.endswith(newline * 2):
            preamble += newline if preamble.endswith(newline) else newline * 2
        updated = preamble + card_text + (newline + content[start:] if first else "")
    return _update_note(sync, source, previous, updated)


def run_checkpoint(sync, vault, state, request, harness=None, session=None, dry_run=False):
    """Execute a unified session checkpoint handoff.

    Accepts a request dict with optional keys:
      - card: {"label": str, "body": str} or {"title": str}
      - threads: list of {"heading": str, "old": str, "new": str} or {"heading": str, "next_step": str}
      - learn: list of {"candidate": str, "source": str, "scope": str}
      - receipt: {"event_id": str, "summary": str, "refs": [...], "links": [...]}
      - chat_trace: str <= 450 chars
      - now: ISO timestamp override for tests or current local time
    """
    vault = Path(vault).resolve()
    state = Path(state).resolve()
    if not isinstance(request, dict):
        raise ValueError("checkpoint request must be an object")

    now_str = request.get("now")
    if now_str:
        try:
            now_dt = datetime.fromisoformat(now_str)
        except Exception:
            now_dt = datetime.now(timezone.utc)
    else:
        now_dt = datetime.now(timezone.utc)

    steps = {}

    # 1. Locate companion directory and run companion-compact automatically if limits exceeded
    companion_dir = locate_companion(vault, strict=False)
    compact_res = None
    try:
        limits_file = state / "companion-limits.json"
        limits = {}
        if limits_file.is_file():
            try:
                limits = json.loads(limits_file.read_text(encoding="utf-8"))
            except Exception:
                limits = {}
        max_last = limits.get("Last-Session.md", 40960)
        max_threads = limits.get("Threads.md", 40960)

        last_session_path = companion_dir / "Last-Session.md"
        threads_path = companion_dir / "Threads.md"

        run_compaction = False
        if last_session_path.is_file() and len(last_session_path.read_bytes()) > max_last:
            run_compaction = True
        if threads_path.is_file() and len(threads_path.read_bytes()) > max_threads:
            run_compaction = True

        if run_compaction and not dry_run:
            compact_res = companion_compact(vault, state, dry_run=False, now=now_dt)
            steps["compact"] = compact_res.get("status", "compacted")
        else:
            steps["compact"] = "skipped_within_limit"
    except Exception as exc:
        steps["compact"] = {"status": "error", "message": str(exc)}

    # 2. Last-Session card update
    card_data = request.get("card")
    if card_data:
        if dry_run:
            steps["card"] = "planned"
        else:
            try:
                last_session_rel = (companion_dir / "Last-Session.md").relative_to(vault).as_posix()
                steps["card"] = _update_card(sync, last_session_rel, card_data, session, now_dt)
            except Exception as exc:
                steps["card"] = {"status": "error", "message": str(exc)}

    # 3. Threads update
    threads_data = request.get("threads")
    if threads_data:
        if dry_run:
            steps["threads"] = "planned"
        else:
            try:
                threads_rel = (companion_dir / "Threads.md").relative_to(vault).as_posix()
                read_res = note_read(sync, threads_rel)
                content = read_res.get("content") or ""
                expected_sha = read_res.get("sha256")

                edits = []
                for t in threads_data:
                    old_text = t.get("old")
                    new_text = t.get("new")
                    heading = t.get("heading")
                    next_step = t.get("next_step")

                    if old_text and new_text:
                        edits.append({"old": old_text, "new": new_text})
                    elif heading and next_step:
                        pattern = rf"(###\s+{re.escape(heading)}[^\n]*\n)(.*?)(?=\n###\s+|\Z)"
                        m = re.search(pattern, content, re.DOTALL)
                        if m:
                            sec_header = m.group(1)
                            sec_body = m.group(2)
                            updated_body = sec_body.strip() + f"\n- {next_step}\n"
                            edits.append({"old": sec_header + sec_body, "new": sec_header + updated_body})
                        else:
                            edits.append({"old": content, "new": content.strip() + f"\n\n### {heading}\n- {next_step}\n"})

                if edits:
                    edit_req = {
                        "source": threads_rel,
                        "expected_sha256": expected_sha,
                        "edits": edits
                    }
                    edit_res = note_apply(sync, edit_req)
                    steps["threads"] = edit_res.get("status", "written")
                else:
                    steps["threads"] = "unchanged"
            except Exception as exc:
                steps["threads"] = {"status": "error", "message": str(exc)}

    # 4. Learn candidates
    learn_data = request.get("learn")
    if learn_data:
        if dry_run:
            steps["learn"] = "planned"
        else:
            try:
                recorded_candidates = []
                for item in learn_data:
                    cand = item.get("candidate")
                    src = item.get("source", "")
                    scp = item.get("scope", "global")
                    sess_id = session or "cli"
                    if cand:
                        res = stage_candidate(state, sess_id, cand, source=src, scope=scp)
                        recorded_candidates.append(res)
                steps["learn"] = {"status": "recorded", "count": len(recorded_candidates)}
            except Exception as exc:
                steps["learn"] = {"status": "error", "message": str(exc)}

    # 5. Receipt submission
    receipt_data = request.get("receipt")
    if receipt_data:
        if dry_run:
            steps["receipt"] = "planned"
        else:
            try:
                event_id = receipt_data.get("event_id")
                summary = receipt_data.get("summary")
                refs = receipt_data.get("refs")
                links = receipt_data.get("links")
                harness_val = harness or "manual"
                sess_id = session or None

                receipt_res = sync.receipt(
                    event_id=event_id,
                    summary=summary,
                    refs=refs,
                    harness=harness_val,
                    session=sess_id,
                    links=links
                )
                steps["receipt"] = receipt_res.get("status", "succeeded")
            except Exception as exc:
                steps["receipt"] = {"status": "error", "message": str(exc)}

    # 6. Chat trace
    chat_trace_data = request.get("chat_trace")
    if chat_trace_data:
        if dry_run:
            steps["chat_trace"] = "planned"
        else:
            try:
                sess_id = session or "cli"
                harness_val = harness or "claude"
                trace_res = beyin_v3_chat_trace.write(vault, state, harness_val, sess_id, chat_trace_data)
                steps["chat_trace"] = trace_res.get("status", "written")
            except Exception as exc:
                steps["chat_trace"] = {"status": "error", "message": str(exc)}

    has_error = any(
        isinstance(v, dict) and (v.get("status") in ("error", "conflict") or v.get("error")) or v == "error"
        for v in steps.values()
    )
    status = "failed" if has_error and len(steps) == 1 else ("partial" if has_error else "succeeded")

    return {
        "status": status,
        "steps": steps,
        "session": session or "cli"
    }
