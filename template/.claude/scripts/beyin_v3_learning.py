"""V3 Learning Candidate Ledger (RFC #368 P0-A/Package 2).

Maintains lightweight, source-linked learning candidates before knowledge distillation.
Persisted in state/learning-state.jsonl; zero network, zero external dependencies.
Cross-process concurrency safety via advisory file locks.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import posixpath
import time
import unicodedata

LEARNING_STATE_FILE = "learning-state.jsonl"
LEARNING_LOCK_FILE = "learning-state.lock"


@contextmanager
def _ledger_lock(state_dir: Path, timeout: float = 10.0, step: float = 0.05):
    """Serialize ledger writers with an OS advisory lock.

    Inline on purpose: the release packages only beyin_v3*.py, so the source tree's
    _portalock.py helper is missing from an installed vault (#151). The lock file stays,
    since unlinking a held lock file lets a later process lock a new inode.
    """
    lock_file = Path(state_dir) / LEARNING_LOCK_FILE
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    if lock_file.is_symlink():
        raise OSError("learning ledger lock must not be a symlink")
    handle = open(lock_file, "a+b")
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or time.monotonic() >= deadline:
                    raise
                time.sleep(step)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _atomic_append(file_path: Path, record: dict) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _ledger_lock(file_path.parent):
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def _normalize_session(session: str | None) -> str:
    if not session or session == "cli":
        return "cli"
    session_str = str(session).strip()
    if len(session_str) > 24:
        return hashlib.sha256(session_str.encode("utf-8")).hexdigest()[:24]
    return session_str[:24]


def _source_key(source: str) -> str:
    """Compare source identities across Windows separators and Unicode filename forms."""
    return unicodedata.normalize("NFC", posixpath.normpath(source.strip().replace("\\", "/"))).casefold()


def _check_source_trust(state: Path, source: str) -> None:
    """Validate that source is not marked private or untrusted."""
    if not source:
        return
    db_path = Path(state) / "memory.sqlite3"
    if db_path.is_file():
        try:
            from contextlib import closing
            import sqlite3
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                # Learning is an explicit CLI write, not a hook hot path. Compare all own
                # identities so alternate path spelling cannot bypass private metadata.
                target = _source_key(source)
                rows = db.execute("SELECT payload FROM records").fetchall()
                for (payload,) in rows:
                    rec = json.loads(payload)
                    identities = (_source_key(value) for value in (rec.get("id"), rec.get("source"))
                                  if isinstance(value, str) and value.strip())
                    if target not in identities:
                        continue
                    if (rec.get("visibility") == "private" or
                        rec.get("trust") == "untrusted" or
                        rec.get("trusted") is False or
                        rec.get("status") == "untrusted" or
                        rec.get("kind") == "untrusted"):
                        raise ValueError(f"source '{source}' is private or untrusted")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"trust check failed: {e}")


def stage_candidate(state: Path, session: str, candidate: str, source: str = "", scope: str = "global") -> dict:
    """Stage a learning candidate into learning-state.jsonl."""
    state = Path(state)
    source = str(source).strip()
    _check_source_trust(state, source)

    cand_text = str(candidate).strip()
    if not cand_text:
        raise ValueError("candidate text required")
    # Fail closed: a filter that cannot run (an invalid secret-patterns.txt) refuses the
    # candidate instead of storing it raw. Only the aggregate health counter is best effort.
    from beyin_v3_secrets import redact, record as record_redactions
    cand_text, count = redact(cand_text, state)
    if count > 0:
        try:
            record_redactions(state, count)
        except Exception:
            pass

    norm_session = _normalize_session(session)
    record_id = hashlib.sha256(f"{norm_session}:{time.time()}:{cand_text}".encode("utf-8")).hexdigest()[:16]
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "id": record_id,
        "created_at": now,
        "session": norm_session,
        "candidate": cand_text,
        "source": str(source).strip(),
        "scope": str(scope).strip() or "global",
        "status": "candidate"
    }
    ledger = state / LEARNING_STATE_FILE
    _atomic_append(ledger, record)
    return record


def _created_at(item: dict) -> float | None:
    try:
        return datetime.fromisoformat(item["created_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def list_candidates(state: Path, session: str | None = None, status: str = "candidate", scope: str | None = None,
                    since: float | None = None) -> list[dict]:
    """Read learning candidates matching session, status, and scope.

    `since` (epoch seconds) keeps only candidates staged at or after that moment. A "cli"
    candidate carries no session, so without it one old entry would match every later session.
    """
    ledger = Path(state) / LEARNING_STATE_FILE
    if not ledger.is_file():
        return []
    target_sess = _normalize_session(session) if session else None
    results = []
    try:
        with open(ledger, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if target_sess and item.get("session") not in (target_sess, "cli"):
                        continue
                    if status and item.get("status") != status:
                        continue
                    if scope and item.get("scope") not in (scope, "global"):
                        continue
                    if since is not None:
                        created = _created_at(item)
                        if created is None or created < since:
                            continue
                    results.append(item)
                except Exception:
                    continue
    except OSError:
        return []
    return results


def has_candidates(state: Path, session: str | None = None, scope: str | None = None,
                   since: float | None = None) -> bool:
    """True if active candidates exist for this session (or overall), staged at or after `since`."""
    return len(list_candidates(state, session=session, status="candidate", scope=scope, since=since)) > 0


def resolve_candidates(state: Path, session: str, target_status: str = "distilled") -> int:
    """Mark all candidates of a session as distilled or discarded."""
    ledger = Path(state) / LEARNING_STATE_FILE
    if not ledger.is_file():
        return 0
    records = []
    count = 0
    target_session = _normalize_session(session)
    try:
        with _ledger_lock(ledger.parent):
            with open(ledger, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        if item.get("session") in (target_session, "cli") and item.get("status") == "candidate":
                            item["status"] = target_status
                            item["resolved_at"] = datetime.now(timezone.utc).isoformat()
                            count += 1
                        records.append(item)
                    except Exception:
                        records.append(line)
            # Atomic rewrite
            temp = ledger.parent / (ledger.name + ".tmp")
            with open(temp, "w", encoding="utf-8") as out:
                for rec in records:
                    if isinstance(rec, str):
                        out.write(rec + "\n")
                    else:
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, ledger)
    except OSError:
        return 0
    return count
