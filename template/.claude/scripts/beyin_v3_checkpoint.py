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

from beyin_v3 import _trusted_record
from beyin_v3_companion import directory as locate_companion, headings
from beyin_v3_compact import ARCHIVE_DIRECTORY, _archive_history, compact as companion_compact
from beyin_v3_notes import read as note_read, apply as note_apply, NoteConflict, locked, _index_excluded
from beyin_v3_learning import stage_candidate
from beyin_v3_parallel import receipt_session
from beyin_v3_sync import parse
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


def _confirm_replayed_note(sync, source, previous):
    # The key and source bytes commit together. A crash can still precede the
    # SQLite projection, so verify or repair the readback without rewriting the note.
    def indexed_hash():
        with sync.store._connect() as db:
            row = db.execute("SELECT r.payload FROM records r JOIN markdown_sources m ON r.id=m.id "
                             "WHERE m.source=?", (previous["source"],)).fetchone()
            return json.loads(row[0]).get("source_sha256") if row else None

    with locked(sync):
        if note_read(sync, source)["sha256"] != previous["sha256"]:
            raise NoteConflict("checkpoint source changed; reread and retry")
        if not _index_excluded(sync, previous["source"]) and indexed_hash() != previous["sha256"]:
            result = sync.sync()
            if (sync._source_issues(result, previous["source"]) or indexed_hash() != previous["sha256"]
                    or note_read(sync, source)["sha256"] != previous["sha256"]):
                raise NoteConflict("checkpoint source is written but index readback failed")
    return "unchanged"


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
    # A receipt key is already normalized. Raw inputs use the same display key as
    # active-session markers without changing the card owner or its replay identity.
    short = owner[:8]
    if session:
        receipt_key = session if re.fullmatch(r"[0-9a-f]{24}", session) else receipt_session(session)
        short = receipt_key[:8]
    marker = f"<!-- beyin-checkpoint:{owner}:{revision} -->"
    suffix = f" · {label} · {short} {marker}"
    end = f"<!-- /beyin-checkpoint:{owner} -->"
    card_text = (f"## {now:%Y-%m-%d %H:%M}{suffix}" + newline
                 + (body.replace("\n", newline) + newline if body else "") + end + newline)
    starts = headings(content, 2)
    pattern = re.compile(rf"^## [^\r\n]+ <!-- beyin-checkpoint:{owner}:([a-f0-9]{{64}}) -->\r?$",
                         re.MULTILINE)
    opening = next((match for start in starts if (match := pattern.match(content, start))), None)
    if opening:
        ending = re.search(rf"^{re.escape(end)}\r?\n?", content[opening.end():], re.MULTILINE)
        if not ending:
            raise ValueError("checkpoint card end marker missing")
        if opening.group(1) == revision:
            return _confirm_replayed_note(sync, source, previous)
        updated = content[:opening.start()] + card_text + content[opening.end() + ending.end():]
    else:
        # Automatic compaction can move a completed card before its retry.
        note = sync.root / previous["source"]
        if content and (note.parent / ARCHIVE_DIRECTORY).is_dir():
            moves, _ = _archive_history(note.parent, note.name, sync.root, now=now)
            if any(marker in moved and end in moved for moved, _ in moves.values()):
                return _confirm_replayed_note(sync, source, previous)
        # A legacy eight-character suffix cannot prove ownership. Preserve those cards.
        start = starts[0] if starts else len(content)
        preamble = content[:start]
        if preamble and not preamble.endswith(newline * 2):
            preamble += newline if preamble.endswith(newline) else newline * 2
        updated = preamble + card_text + (newline + content[start:] if starts else "")
    return _update_note(sync, source, previous, updated)


def _update_threads(sync, source, threads, session):
    previous = note_read(sync, source)
    content = previous["content"] or ""
    newline = "\r\n" if "\r\n" in content else "\n"
    applied = set(re.findall(r"^<!-- beyin-checkpoint-thread:([a-f0-9]{64}) -->\r?$",
                             content, re.MULTILINE))
    markers = []
    replayed = False
    for item in threads:
        old, new = item.get("old"), item.get("new")
        if old and new:
            old = old.replace("\r\n", "\n").replace("\r", "\n")
            new = new.replace("\r\n", "\n").replace("\r", "\n")
            key = _step_key(session, "thread-replace", old, new)
            if key in applied:
                replayed = True
                continue
            if old == new:
                continue
            anchor = old.replace("\n", newline)
            start = content.find(anchor)
            if start < 0 or content.find(anchor, start + 1) >= 0:
                raise NoteConflict("Threads edit anchor must occur exactly once")
            content = content.replace(anchor, new.replace("\n", newline), 1)
        else:
            heading = (item.get("heading") or "").strip()
            next_step = (item.get("next_step") or "").strip()
            if not heading or not next_step:
                continue
            if heading.splitlines() != [heading] or next_step.splitlines() != [next_step]:
                raise ValueError("Threads heading and next step must each be one line")
            key = _step_key(session, "thread-next-step", heading, next_step)
            if key in applied:
                replayed = True
                continue
            starts = headings(content, 3)
            pattern = re.compile(rf"### {re.escape(heading)}[ \t]*(?:\r?\n|\Z)")
            matches = [match for start in starts if (match := pattern.match(content, start))]
            if len(matches) > 1:
                raise NoteConflict("Threads heading must occur at most once")
            line = f"- {next_step}"
            if matches:
                start = matches[0].end()
                boundaries = starts + headings(content, 2) + headings(content, 1)
                end = min((pos for pos in boundaries if pos >= start), default=len(content))
                body = content[start:end]
                if line in body.splitlines():
                    continue
                trimmed = body.rstrip("\r\n")
                prefix = content[:start] + trimmed
                separator = "" if prefix.endswith(newline) else newline
                content = prefix + separator + line + (body[len(trimmed):] or newline) + content[end:]
            else:
                separator = "" if not content or content.endswith(newline * 2) else (
                    newline if content.endswith(newline) else newline * 2)
                content += separator + f"### {heading}" + newline + line + newline
        applied.add(key)
        markers.append(f"<!-- beyin-checkpoint-thread:{key} -->")

    if not markers:
        return _confirm_replayed_note(sync, source, previous) if replayed else "unchanged"
    # Commit the keys and their edits together. A crash after replacement can be
    # recognized without a second file or a state directory shared across clients.
    # Keep the keys in the preamble so archiving a closed thread cannot move them.
    _, body = parse(content)
    start = len(content) - len(body)
    title = re.match(r"\ufeff?# [^\r\n]*\r?\n", body)
    if title:
        start += title.end()
    elif body.startswith("\ufeff"):
        start += 1
    content = content[:start] + newline.join(markers) + newline + content[start:]
    return _update_note(sync, source, previous, content)


def _learning_source(sync, source):
    path = sync._path(source, existing=True)
    relative = path.resolve().relative_to(sync.root).as_posix()
    sync._refuse_excluded(relative)
    if path.suffix.casefold() == ".md":
        metadata, _ = parse(path.read_text(encoding="utf-8"))
        if metadata.get("visibility", "internal") not in ("internal", "public") or not _trusted_record(metadata):
            raise ValueError("checkpoint learning source is private or untrusted")
    return relative


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

    # Use the same character limits, opt-outs and archive policy as the standalone command.
    companion_dir = locate_companion(vault, strict=False)
    try:
        preview = companion_compact(vault, state, dry_run=True, now=now_dt)
        needs_work = any(item["status"] in ("planned", "needs_rewrite") for item in preview["files"].values())
        if needs_work:
            steps["compact"] = ("planned" if dry_run else
                                companion_compact(vault, state, now=now_dt)["status"])
        elif preview["status"] == "dry_run":
            steps["compact"] = "skipped_within_limit"
        else:
            steps["compact"] = preview["status"]
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
                steps["threads"] = _update_threads(sync, threads_rel, threads_data, session)
            except Exception as exc:
                steps["threads"] = {"status": "error", "message": str(exc)}

    # 4. Learn candidates
    learn_data = request.get("learn")
    if learn_data:
        if dry_run:
            steps["learn"] = "planned"
        else:
            try:
                recorded = 0
                for item in learn_data:
                    cand = item.get("candidate")
                    src = (item.get("source") or "").strip()
                    scp = item.get("scope", "global")
                    sess_id = session or "cli"
                    if cand:
                        if src:
                            src = _learning_source(sync, src)
                        res = stage_candidate(state, sess_id, cand, source=src, scope=scp, deduplicate=True)
                        if not res.get("idempotent"):
                            recorded += 1
                steps["learn"] = {"status": "recorded" if recorded else "unchanged", "count": recorded}
            except Exception:
                steps["learn"] = {"status": "error", "message":
                                  "Learning candidate was not confirmed; check its text and source visibility."}

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
                key = _step_key(sess_id, "chat-trace", harness_val, " ".join(chat_trace_data.split()))
                trace_res = beyin_v3_chat_trace.write(
                    vault, state, harness_val, sess_id, chat_trace_data, checkpoint_key=key
                )
                steps["chat_trace"] = trace_res.get("status", "written")
            except Exception as exc:
                steps["chat_trace"] = {"status": "error", "message": str(exc)}

    failures = {"error", "conflict", "needs_attention", "needs_rewrite"}
    statuses = [value["status"] if isinstance(value, dict) else value for value in steps.values()]
    has_error = any(status in failures for status in statuses)
    has_success = any(status not in failures and status != "skipped_within_limit" for status in statuses)
    status = ("partial" if has_success else "failed") if has_error else "succeeded"

    return {
        "status": status,
        "steps": steps,
        "session": session or "cli"
    }
