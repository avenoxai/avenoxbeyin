"""Compact old hook acknowledgements without losing deduplication or doctor history (#304).

Fresh acknowledgements remain JSON files for the existing fast path. Once a file is
old enough, its event ID and lifecycle metadata are committed to the runtime SQLite
database BEFORE the file is removed. The ledger is append-only: forgetting event IDs
would make a delayed duplicate eligible again.
"""
from contextlib import contextmanager, closing
import json
from pathlib import Path
import re
import sqlite3
import stat
import time

RETENTION_SECONDS = 7 * 86400
CHECK_INTERVAL_SECONDS = 86400
BATCH_SIZE = 256
MAX_EVENT_BYTES = 65536
_KEY_RE = re.compile(r"[a-f0-9]{64}\.json\Z")
_CREATE = ("CREATE TABLE IF NOT EXISTS hook_done_ledger ("
           "key TEXT PRIMARY KEY, harness TEXT NOT NULL, event TEXT NOT NULL)")


@contextmanager
def _reader(state):
    """Read only an existing bound runtime DB; never create one from a hook/doctor."""
    database = Path(state) / "memory.sqlite3"
    if database.is_symlink():
        raise ValueError("hook ledger database must not be a symlink")
    if not database.is_file():
        yield None
        return
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=1)) as db:
        yield db


def _missing_table(error):
    return "no such table: hook_done_ledger" in str(error)


def acknowledged(state, key):
    """True when an old event's durable ledger entry replaces its deleted JSON."""
    with _reader(state) as db:
        if db is None:
            return False
        try:
            return db.execute("SELECT 1 FROM hook_done_ledger WHERE key=?", (key,)).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):  # older installations have only hook-done/ files
                return False
            raise


def prune(store, now=None):
    """Archive at most one batch of expired, valid JSON markers.

    Serialize migrations on the existing memory DB writer lock. SQLite commits
    before any unlink, so a crash preserves either the old file or the ledger
    entry (possibly both). A completed empty scan is throttled to once per day;
    backlog batches continue on subsequent successful drains.
    """
    now = time.time() if now is None else now
    done = store.state_dir / "hook-done"
    if done.is_symlink() or not done.is_dir():
        return 0
    batch = []
    try:
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT value FROM metadata WHERE key='hook_done_last_scan'").fetchone()
            if previous is not None and now - float(previous[0]) < CHECK_INTERVAL_SECONDS:
                return 0
            db.execute(_CREATE)
            for path in done.glob("*.json"):
                if len(batch) >= BATCH_SIZE:
                    break
                if not _KEY_RE.fullmatch(path.name):
                    continue
                try:
                    info = path.lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EVENT_BYTES
                            or now - info.st_mtime < RETENTION_SECONDS):
                        continue
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        continue
                    harness, event = data.get("harness"), data.get("event")
                    if not isinstance(harness, str) or not isinstance(event, str):
                        continue
                    batch.append((path, path.stem, harness, event))
                except (OSError, ValueError, UnicodeError):
                    continue  # A bad or disappearing file is retained, never silently deleted.
            if batch:
                db.executemany("INSERT OR IGNORE INTO hook_done_ledger VALUES (?,?,?)",
                               [(key, harness, event) for _path, key, harness, event in batch])
            else:
                # No eligible files: avoid a full directory scan on every drain.
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('hook_done_last_scan',?)",
                           (str(now),))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            batch = []
        else:
            raise
    # The transaction is committed and the connection closed before removing anything.
    removed = 0
    for path, _key, _harness, _event in batch:
        try:
            if not path.is_symlink():
                path.unlink()
                removed += 1
        except (FileNotFoundError, PermissionError):
            pass  # Another cleanup worker already removed the committed marker, or Windows kept it open.
    return removed


def summary(state, harnesses):
    """Report unique acknowledgements and lifetime lifecycle events for doctor.

    During an interrupted migration, a marker may exist both on disk and in
    SQLite. COUNT(*) plus live files minus overlapping keys avoids double counts.
    SQL DISTINCT avoids loading the whole historical ledger into Python.
    """
    observed = {harness: set() for harness in harnesses}
    paths = []
    done = Path(state) / "hook-done"
    for path in (() if done.is_symlink() else done.glob("*.json")):
        try:
            if path.is_symlink() or path.stat().st_size > MAX_EVENT_BYTES:
                continue
            event = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                continue
        except (OSError, ValueError, UnicodeError):
            continue  # Corrupt, outside, or concurrently removed markers are not acknowledgements.
        paths.append(path)
        if event.get("harness") in observed:
            observed[event["harness"]].add(event.get("event", "unknown"))
    count = len(paths)
    with _reader(state) as db:
        if db is not None:
            try:
                archived = db.execute("SELECT COUNT(*) FROM hook_done_ledger").fetchone()[0]
                count += archived
                for harness, event in db.execute("SELECT DISTINCT harness,event FROM hook_done_ledger"):
                    if harness in observed:
                        observed[harness].add(event)
                keys = [p.stem for p in paths]
                if archived > 0 and keys:
                    for offset in range(0, len(keys), 500):
                        chunk = keys[offset:offset + 500]
                        placeholders = ",".join("?" for _ in chunk)
                        count -= db.execute(
                            f"SELECT COUNT(*) FROM hook_done_ledger WHERE key IN ({placeholders})", chunk
                        ).fetchone()[0]
            except sqlite3.OperationalError as exc:
                if not _missing_table(exc):
                    raise
    lifecycle = {name: {"status": "observed_metadata" if events else "never_seen",
                        "events": sorted(events)} for name, events in observed.items()}
    return {"acknowledged_events": count, "lifecycle": lifecycle}
