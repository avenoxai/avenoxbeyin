# -*- coding: utf-8 -*-
"""Machine-local note counters and session-bound feedback."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from beyin_v3 import sensitive_fold, sensitive_source

DATABASE = "note-usage.sqlite3"
MAX_ENTRIES = 2000
DEAD_WEIGHT_THRESHOLD = 10
VERDICTS = ("ok", "wrong", "stale")
NO_SESSION = "Geri bildirim oturumu belirlenemedi; --session ile hook'un gösterdiği oturumu belirtin."


def session_key(raw):
    if isinstance(raw, str) and raw.strip() and raw != "unknown":
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return None


def _is_private_or_sensitive(record):
    if record.get("private") or record.get("is_private") or record.get("trusted") is False:
        return True
    if record.get("visibility") in ("private", "untrusted", "secret", "internal"):
        return True
    if record.get("trust") in ("untrusted", "false", "private"):
        return True
    if any(record.get(key) in ("untrusted", "private") for key in ("status", "kind")):
        return True
    source = record["source"].replace("\\", "/")
    return sensitive_source(source) or any(
        part in sensitive_fold(source) for part in (".env", "secret", "password", "credential", "private", "token", "key")
    )


def _targets(records):
    return {record["id"]: record["source"] for record in records
            if isinstance(record.get("id"), str) and record["id"].strip()
            and isinstance(record.get("source"), str) and record["source"].strip()
            and not _is_private_or_sensitive(record)}


def _connect(state, *, mode="rw", timeout=0):
    path = state / DATABASE
    if path.is_symlink():
        raise ValueError("Usage database must not be a symlink")
    uri = path.absolute().as_uri() + "?mode=" + mode
    return closing(sqlite3.connect(uri, uri=True, timeout=timeout))


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS notes (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, injected INTEGER NOT NULL DEFAULT 0,
        recent TEXT NOT NULL DEFAULT '[]', cited INTEGER NOT NULL DEFAULT 0,
        ok INTEGER NOT NULL DEFAULT 0, wrong INTEGER NOT NULL DEFAULT 0,
        stale INTEGER NOT NULL DEFAULT 0, touched REAL NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS notes_touched ON notes(touched)")
    db.execute("""CREATE TABLE IF NOT EXISTS turns (
        session TEXT PRIMARY KEY, at REAL NOT NULL, targets TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS turns_at ON turns(at)")
    db.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS citations (id TEXT PRIMARY KEY)")


def _timestamp(now):
    return (now if now is not None else datetime.now(timezone.utc)).timestamp()


def _prune(db, table, limit):
    db.execute(f"DELETE FROM {table} WHERE rowid IN ("
               f"SELECT rowid FROM {table} ORDER BY {'touched' if table == 'notes' else 'at'} DESC, rowid DESC "
               "LIMIT -1 OFFSET ?)", (limit,))


def _remember_injection(previous, at):
    values = sorted([*json.loads(previous), at])[-DEAD_WEIGHT_THRESHOLD:]
    return json.dumps(values)


def record_injected(state, records, *, session, turn, now=None):
    """Record the delivered projection in one non-waiting transaction."""
    targets = _targets(records)
    if not targets and (not session or not (state / DATABASE).is_file()):
        return
    at = _timestamp(now)
    with _connect(state, mode="rwc" if targets else "rw") as db, db:
        db.execute("BEGIN IMMEDIATE")
        if targets:
            _schema(db)
        elif not _turn(db, session):
            return  # An empty turn only supersedes an existing set of targets.
        event = hashlib.sha256((str(session) + ":" + turn).encode("utf-8")).hexdigest()
        if not db.execute("INSERT OR IGNORE INTO deliveries VALUES (?)", (event,)).rowcount:
            return
        db.create_function("remember_injection", 2, _remember_injection)
        db.executemany("""INSERT INTO notes(id, source, injected, recent, touched)
                          VALUES (?, ?, 1, remember_injection('[]', ?), ?)
                          ON CONFLICT(id) DO UPDATE SET source=excluded.source, injected=injected+1,
                          recent=remember_injection(recent, excluded.touched), touched=MAX(touched, excluded.touched)""",
                       ((note_id, source, at, at) for note_id, source in targets.items()))
        if session:
            db.execute("""INSERT INTO turns VALUES (?, ?, ?) ON CONFLICT(session) DO UPDATE
                          SET at=excluded.at, targets=excluded.targets WHERE excluded.at >= turns.at""",
                       (session, at, json.dumps(targets, ensure_ascii=False)))
            _prune(db, "turns", MAX_ENTRIES)
        _prune(db, "notes", MAX_ENTRIES)
        db.execute("DELETE FROM deliveries WHERE rowid <= (SELECT MAX(rowid) - ? FROM deliveries)",
                   (MAX_ENTRIES * 10,))


def record_cited(state, event_id, refs, sync):
    # The receipt has already validated the paths and synced the source index.
    sources = {Path(ref).as_posix() for ref in refs}
    with sync.store._connect() as db:
        records = []
        for source in sources:
            row = db.execute("SELECT payload FROM records WHERE id=?", (sync.source_ids.get(source),)).fetchone()
            if row:
                records.append(json.loads(row[0]))
    targets = _targets(records)
    if not targets:
        return
    at = _timestamp(None)
    with _connect(state, mode="rwc", timeout=1) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _schema(db)
        event = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        if not db.execute("INSERT OR IGNORE INTO citations VALUES (?)", (event,)).rowcount:
            return
        db.executemany("""INSERT INTO notes(id, source, cited, touched) VALUES (?, ?, 1, ?)
                          ON CONFLICT(id) DO UPDATE SET source=excluded.source, cited=cited+1,
                          touched=MAX(touched, excluded.touched)""",
                       ((note_id, source, at) for note_id, source in targets.items()))
        _prune(db, "notes", MAX_ENTRIES)


def _turn(db, session):
    row = db.execute("SELECT targets FROM turns WHERE session = ?", (session,)).fetchone()
    return json.loads(row[0]) if row else {}


def record_feedback(state, target, verdict, *, session):
    if not session or not (state / DATABASE).is_file():
        raise ValueError(NO_SESSION)
    if verdict not in VERDICTS:
        raise ValueError("Geri bildirim değeri ok, wrong veya stale olmalı.")
    target = target.strip()
    if not target:
        raise ValueError("Geri bildirim için bir not kimliği veya yolu belirtin.")
    with _connect(state, timeout=1) as db, db:
        db.execute("BEGIN IMMEDIATE")
        targets = _turn(db, session)
        if not targets:
            raise ValueError("Bu oturumda geri bildirim verilebilecek bir not kaydı yok; --session değerini doğrulayın.")
        normalized = target.replace("\\", "/")
        matches = [(note_id, source) for note_id, source in targets.items()
                   if note_id == target or source.replace("\\", "/") == normalized]
        if len(matches) != 1:
            raise ValueError("Geri bildirim hedefi bu oturumda sunulan tek bir notla eşleşmeli.")
        note_id, source = matches[0]
        db.execute(f"""INSERT INTO notes(id, source, {verdict}, touched) VALUES (?, ?, 1, ?)
                       ON CONFLICT(id) DO UPDATE SET {verdict}={verdict}+1,
                       touched=MAX(touched, excluded.touched)""", (note_id, source, _timestamp(None)))
        counts = db.execute("SELECT ok, wrong, stale FROM notes WHERE id=?", (note_id,)).fetchone()
        _prune(db, "notes", MAX_ENTRIES)
    return {"success": True, "target": note_id, "verdict": verdict, "feedback": dict(zip(VERDICTS, counts))}


def summarize_usage(state, *, window_days=30, now=None):
    totals = dict.fromkeys(VERDICTS, 0)
    result = {"tracked": 0, "dead_weight_count": 0, "feedback_totals": totals}
    if not (state / DATABASE).is_file():
        return result
    cutoff = _timestamp(now) - timedelta(days=window_days).total_seconds()
    with _connect(state, mode="ro") as db:
        for recent, cited, ok, wrong, stale in db.execute("SELECT recent, cited, ok, wrong, stale FROM notes"):
            result["tracked"] += 1
            if cited == 0 and ok == 0 and sum(at >= cutoff for at in json.loads(recent)) >= DEAD_WEIGHT_THRESHOLD:
                result["dead_weight_count"] += 1
            for verdict, count in zip(VERDICTS, (ok, wrong, stale)):
                totals[verdict] += count
    return result


def feedback_hint(state, query, *, session):
    if not session or not query:
        return ""
    query = query.strip()
    if not query or len(query) > 40:
        return ""
    cues = {"yanlis", "eski", "eskimis", "dogru", "wrong", "stale", "ok"}
    words = re.findall(r"[^\W_]+", sensitive_fold(query))
    if cues.isdisjoint(words) or not (state / DATABASE).is_file():
        return ""
    with _connect(state, mode="ro") as db:
        targets = _turn(db, session)
    if not targets:
        return ""
    sources = ", ".join(list(targets.values())[:10])
    return (f"[Hafıza geri bildirimi] Önceki turda sunulan kaynaklar: {sources}. "
            f"Geri bildirim vermek için: beyin.py feedback <hedef> <ok|wrong|stale> --session {session}")
