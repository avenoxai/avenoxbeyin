"""Offline mutation queue and multi-device local-first synchronization support (#205).

Provides anonymous device/machine identity, append-only offline mutation queue
(state/mutation-queue.jsonl), idempotent replay against local sync engine, and
conflict isolation adhering to .sync-conflict-*.md file-sync semantics.
Standard library only; zero external dependencies.

Opt-in and machine-local: the switch lives in <state>/mutation-queue-settings.json,
never in .beyin-preferences.json (an older release rejects unknown preference fields,
so a rollback would break). While it is off, enqueue and replay refuse and nothing is
written; doctor reports only {"enabled": false}.

Crash consistency: every replayed mutation is acknowledged in the queue right after it
is applied. A process that dies between the apply and its acknowledgement leaves the
record pending; the next replay first checks whether the target already reflects the
mutation and then acknowledges it as applied (reconciled) instead of writing twice or
reporting a false conflict.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

MUTATION_QUEUE_FILE = "mutation-queue.jsonl"
SETTINGS_FILE = "mutation-queue-settings.json"
DEVICE_FILE = "device.json"
PRESENCE_DEVICE_FILE = "parallel-presence-device.json"
VALID_ACTIONS = ("note_create", "note_update", "task_create", "task_update", "receipt")
VALID_STATUSES = ("pending", "applied", "conflict", "failed", "dismissed")
OFF_MESSAGE = "mutation queue is off on this machine; enable it with: beyin.py mutation-queue --enable"
_FULL_HASH = re.compile(r"[0-9a-f]{64}")
_DEVICE_ID = re.compile(r"[0-9a-f]{32}")
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
# Engine results carry whole-vault sync warnings and task bodies; the queue keeps a bounded summary.
_RESULT_FIELDS = ("status", "id", "event_id", "source", "revision", "sha256", "source_sha256", "backup",
                  "secrets_redacted")


def read_settings(state):
    """(settings, valid). A missing file means off; a damaged one reads as off and is never rewritten."""
    path = Path(state) / SETTINGS_FILE
    try:
        from beyin_v3_notes import _unlinked
        _unlinked(path)
        if not path.exists():
            return {"enabled": False}, True
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or set(value) - {"schema", "enabled"} or
                value.get("schema", 1) != 1 or type(value.get("enabled")) is not bool):
            raise ValueError("invalid mutation queue settings")
        return {"enabled": value["enabled"]}, True
    except (ValueError, OSError):
        return {"enabled": False}, False


def set_enabled(state, enabled):
    _, valid = read_settings(state)
    if not valid:
        raise ValueError(SETTINGS_FILE + " in the runtime state is invalid; fix or remove it first")
    from beyin_v3_sync import atomic
    Path(state).mkdir(parents=True, exist_ok=True)
    atomic(Path(state) / SETTINGS_FILE, json.dumps({"schema": 1, "enabled": bool(enabled)}) + "\n")
    return status(state)


def _require_enabled(state):
    settings, valid = read_settings(state)
    if not valid:
        raise ValueError(SETTINGS_FILE + " in the runtime state is invalid; the queue stays off until it is fixed")
    if not settings["enabled"]:
        raise ValueError(OFF_MESSAGE)


def _read_device(path):
    try:
        from beyin_v3_notes import _unlinked
        _unlinked(path)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    dev = data.get("device") if isinstance(data, dict) else None
    return dev if isinstance(dev, str) and _DEVICE_ID.fullmatch(dev) else None


def get_machine_id(state: str | Path, *, create=True) -> str | None:
    """Return persistent, anonymous 32-hex machine identifier.

    Random (uuid4); never derived from user, host or path names. It is kept only in the
    machine-local state, which must stay outside the vault, and never written into a
    vault file. Order: the queue's own device.json, then the opt-in presence identity
    (pinned into device.json so it stays stable), then a new one. Writers call this
    while holding the queue lock; doctor passes create=False and never writes.
    """
    state_dir = Path(state)
    device_file = state_dir / DEVICE_FILE
    dev = _read_device(device_file)
    if dev:
        return dev
    presence = _read_device(state_dir / PRESENCE_DEVICE_FILE)
    if not create:
        return presence
    dev = presence or uuid.uuid4().hex
    from beyin_v3_sync import atomic
    state_dir.mkdir(parents=True, exist_ok=True)
    atomic(device_file, json.dumps({"schema": 1, "device": dev}) + "\n")
    return dev


def _queue_path(state: str | Path) -> Path:
    from beyin_v3_notes import _unlinked
    path = Path(state) / MUTATION_QUEUE_FILE
    _unlinked(path)
    return path


@contextmanager
def _queue_lock(state):
    # Reuse the cross-platform, process-aware cooperative writer lock in local state.
    from beyin_v3_notes import locked
    root = Path(state).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with locked(SimpleNamespace(root=root)):
        yield


def _check_payload(action, source, payload):
    if action not in VALID_ACTIONS:
        raise ValueError(f"invalid mutation action: {action}; must be one of {VALID_ACTIONS}")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string path")
    if Path(source).is_absolute() or ".." in Path(source).parts or "\\" in source or ":" in source:
        raise ValueError("relative vault source required")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dictionary")
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("payload metadata must be an object")
    if action in ("note_create", "task_create") and (not isinstance(payload.get("text"), str) or
                                                     not payload["text"].strip()):
        raise ValueError(action + " requires nonempty text")
    if action == "task_create" and not isinstance(metadata, dict):
        raise ValueError("task_create requires metadata")
    if action == "note_update":
        if not isinstance(payload.get("text"), str):
            raise ValueError("note_update requires text")
        if not isinstance(payload.get("expected_hash"), str) or not _FULL_HASH.fullmatch(payload["expected_hash"]):
            raise ValueError("note_update requires a full SHA-256 expected_hash")
    if action == "task_update":
        revision = payload.get("expected_revision")
        if not isinstance(payload.get("id"), str) or not payload["id"].strip():
            raise ValueError("task_update requires a task id")
        if type(revision) is not int or revision < 1:
            raise ValueError("task_update requires an integer expected_revision")
        if not isinstance(payload.get("changes"), dict) or not payload["changes"]:
            raise ValueError("task_update requires nonempty changes")
    if action == "receipt":
        refs = payload.get("refs")
        if not all(isinstance(payload.get(key), str) and payload[key].strip() for key in ("event_id", "summary")):
            raise ValueError("receipt requires event_id and summary")
        if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref for ref in refs):
            raise ValueError("receipt requires refs")


def enqueue_mutation(state, action, source, payload, vault=None, mutation_id=None):
    _require_enabled(state)
    _check_payload(action, source, payload)
    if mutation_id is not None and (not isinstance(mutation_id, str) or not _CLIENT_ID.fullmatch(mutation_id)):
        raise ValueError("mutation_id must be 8-64 letters, digits, '_' or '-'")
    with _queue_lock(state):
        return _enqueue_mutation(state, action, source, payload, vault, mutation_id)


def _enqueue_mutation(
    state: str | Path,
    action: str,
    source: str,
    payload: dict,
    vault: str | Path | None = None,
    client_id: str | None = None,
) -> dict:
    """Enqueue an offline mutation to state/mutation-queue.jsonl."""
    state_dir = Path(state)
    state_dir.mkdir(parents=True, exist_ok=True)
    if client_id is not None:
        # A caller-chosen id makes a resend of the same mutation a no-op.
        for item in list_mutations(state_dir):
            if item["mutation_id"] == client_id:
                if (item.get("action"), item.get("source"), item.get("payload")) != (action, source, payload):
                    raise ValueError("mutation_id already used for a different mutation")
                return dict(item, duplicate=True)
    machine_id = get_machine_id(state_dir)

    now = time.time()
    iso_time = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
    raw_hash_input = f"{machine_id}:{now}:{action}:{source}:{json.dumps(payload, sort_keys=True)}"
    mutation_id = client_id or hashlib.sha256(raw_hash_input.encode("utf-8")).hexdigest()[:24]

    mutation = {
        "schema": 1,
        "mutation_id": mutation_id,
        "machine_id": machine_id,
        "timestamp": now,
        "iso_time": iso_time,
        "action": action,
        "source": source,
        "payload": payload,
        "status": "pending",
        "applied_at": None,
        "error": None,
    }

    queue_file = _queue_path(state_dir)
    line = json.dumps(mutation, ensure_ascii=False) + "\n"
    # Queued payloads hold note text: owner-only from the first append, like the rewritten file.
    fd = os.open(queue_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", newline="") as f:
        f.write(line)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass

    return mutation


def list_mutations(state: str | Path, status: str | None = None) -> list[dict]:
    """List queued mutations, optionally filtered by status."""
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"invalid status filter: {status}; must be one of {VALID_STATUSES}")

    queue_file = _queue_path(state)
    if not queue_file.is_file():
        return []

    mutations = []
    with open(queue_file, 'r', encoding='utf-8') as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise ValueError(f'invalid mutation queue line {number}; preserve and repair it') from exc
            if not isinstance(item, dict) or not isinstance(item.get('mutation_id'), str):
                raise ValueError(f'invalid mutation queue line {number}; preserve and repair it')
            if status is None or item.get('status') == status:
                mutations.append(item)
    return mutations


def _save_all_mutations(state: str | Path, mutations: list[dict]) -> None:
    """Atomically rewrite mutation-queue.jsonl with updated statuses."""
    from beyin_v3_sync import _safe_replace
    state_dir = Path(state)
    state_dir.mkdir(parents=True, exist_ok=True)
    queue_file = _queue_path(state_dir)

    fd, tmp = tempfile.mkstemp(prefix=".mqueue-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            for item in mutations:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        # Retries a brief Windows sharing violation (a reader, an indexer) like other state writes.
        _safe_replace(tmp, queue_file)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _ack(state_dir, mutation):
    """Persist one mutation's outcome before the next one runs (crash window: one mutation)."""
    latest = list_mutations(state_dir)
    _save_all_mutations(state_dir, [mutation if item['mutation_id'] == mutation['mutation_id'] else item
                                    for item in latest])


def _compact(result):
    return {key: result[key] for key in _RESULT_FIELDS if key in result} if isinstance(result, dict) else {}


def isolate_conflict(vault: str | Path, mutation: dict, sync_engine=None) -> str:
    """Create a conflict copy matching the file-sync conflict pattern:

    .*\\.sync-conflict-\\d{8}(?:-\\d{6})?-[0-9A-Za-z]+\\.md
    """
    from beyin_v3_sync import render, parse
    from beyin_v3_notes import source_path, locked
    engine = sync_engine
    if engine is None:
        raise ValueError('conflict isolation requires the validated replay engine')
    source = mutation['source']
    if source.startswith('tasks/'):
        from beyin_v3_notes import _unlinked
        original = engine._path(source)
        cursor = original
        while cursor != engine.root:
            _unlinked(cursor)
            cursor = cursor.parent
    else:
        original = source_path(engine, source)
    payload = mutation.get('payload', {})
    text, _ = engine._protect(payload.get('text', ''))
    metadata = parse(original.read_text(encoding='utf-8'))[0] if original.is_file() else {}
    metadata.update(payload.get('metadata') or {})
    metadata, _ = engine._protect_metadata(metadata)
    content = render(metadata, text.rstrip('\n') + '\n')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
    # An exclusive create plus a fresh suffix prevents same-second overwrites.
    with locked(engine):
        for _ in range(10):
            relative = original.relative_to(engine.root).with_name(
                f'{original.stem}.sync-conflict-{stamp}-{uuid.uuid4().hex[:8]}.md').as_posix()
            target = engine._path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target = engine._path(relative)
            from beyin_v3_notes import _unlinked
            cursor = target
            while cursor != engine.root:
                _unlinked(cursor)
                cursor = cursor.parent
            try:
                with target.open('x', encoding='utf-8', newline='') as out:
                    out.write(content)
                    out.flush()
                    os.fsync(out.fileno())
                return relative
            except FileExistsError:
                continue
    raise ValueError('could not allocate a unique conflict copy')


def _created_note(engine, payload):
    """The bytes SyncEngine.note_create writes for this payload, or None if it would refuse it."""
    from beyin_v3_sync import render
    from beyin_v3_notes import evidence_refs
    metadata = dict(payload.get('metadata') or {})
    # As note_create: the retry key never reaches frontmatter and evidence is stored canonically.
    metadata.pop('idempotency_key', None)
    try:
        refs = evidence_refs(engine, metadata.pop('evidence_refs', None), existing=False)
    except ValueError:
        return None  # note_create refuses these refs, so an existing file is not this mutation's
    if refs:
        metadata['evidence_refs'] = refs
    for field in ('project', 'kind', 'status', 'updated_at'):
        if metadata.get(field) is None:
            metadata.pop(field, None)
    text, _ = engine._protect(payload.get('text', ''))
    metadata, _ = engine._protect_metadata(metadata)
    return render(metadata, text + '\n').encode('utf-8')


def _updated_note(engine, raw, payload):
    """The content a note_update produces from the current source text `raw`."""
    from beyin_v3_sync import parse, render
    meta, _ = parse(raw)
    meta.update(payload.get('metadata') or {})
    text, _ = engine._protect(payload.get('text', ''))
    meta, _ = engine._protect_metadata(meta)
    return render(meta, text.rstrip('\n') + '\n')


def _task_reflects(engine, payload):
    """True when the task already carries this update: an interrupted replay applied it
    (or another writer made the same change) before the queue acknowledgement."""
    # sync() first completes an interrupted source write from the engine's journal.
    engine.sync()
    with engine.store._connect() as db:
        row = db.execute('SELECT payload FROM records WHERE id=?', (payload['id'],)).fetchone()
    if not row:
        return False
    record = json.loads(row[0])
    changes, _ = engine._protect_metadata(dict(payload.get('changes') or {}))
    return (record.get('kind') == 'task' and type(record.get('revision')) is int and
            record['revision'] > payload['expected_revision'] and
            all(record.get(key) == value for key, value in changes.items()))


def replay_mutations(vault, state, sync_engine=None):
    _require_enabled(state)
    if not _queue_path(state).is_file():
        return {'status': 'succeeded', 'total': 0, 'replayed': 0, 'applied': 0,
                'conflicts': 0, 'failed': 0, 'mutations': []}
    with _queue_lock(state):
        return _replay_mutations(vault, state, sync_engine)


def _replay_mutations(vault: str | Path, state: str | Path, sync_engine=None) -> dict:
    """Replay pending mutations against vault and sync engine.

    Idempotent: matching content is marked applied without modification.
    Conflicts: isolated via .sync-conflict-*.md copies without overwriting original.
    """
    vpath = Path(vault)
    state_dir = Path(state)
    all_mutations = list_mutations(state_dir)
    pending = [m for m in all_mutations if m.get("status") == "pending"]

    if not pending:
        return {
            "status": "succeeded",
            "total": len(all_mutations),
            "replayed": 0,
            "applied": 0,
            "conflicts": 0,
            "failed": 0,
            "mutations": [],
        }

    if sync_engine is None:
        import beyin_v3_sync
        sync_engine = beyin_v3_sync.SyncEngine(vpath, state_dir)

    from beyin_v3_notes import source_path, NoteConflict
    from beyin_v3_sync import parse, ReceiptConflict, RevisionConflict

    applied_count = 0
    conflict_count = 0
    failed_count = 0
    processed = []

    for mut in pending:
        action = mut["action"]
        source = mut["source"]
        payload = mut.get("payload", {})
        try:
            # Revalidate: the queue file is local state, but it is not trusted input.
            _check_payload(action, source, payload)
            target_path = sync_engine._path(source)
            if action in ('note_create', 'note_update'):
                target_path = source_path(sync_engine, source)
            if action == "note_create":
                if target_path.is_file():
                    if target_path.read_bytes() == _created_note(sync_engine, payload):
                        mut["status"] = "applied"
                        mut["idempotent"] = True
                        applied_count += 1
                    else:
                        conflict_rel = isolate_conflict(vpath, mut, sync_engine)
                        mut["status"] = "conflict"
                        mut["conflict_path"] = conflict_rel
                        conflict_count += 1
                else:
                    res = sync_engine.note_create(source, payload.get("text", ""), payload.get("metadata"))
                    mut["status"] = "applied"
                    mut["result"] = _compact(res)
                    applied_count += 1

            elif action == "note_update":
                expected = payload['expected_hash']
                snapshot = sync_engine.note_read(source)
                raw = snapshot['content']
                reflected = False
                if raw is not None and snapshot['sha256'] != expected:
                    try:
                        reflected = _updated_note(sync_engine, raw, payload) == raw
                    except ValueError:
                        reflected = False
                if reflected:
                    mut.update(status='applied', idempotent=True, reconciled=True)
                    applied_count += 1
                elif raw is None or snapshot['sha256'] != expected:
                    conflict_rel = isolate_conflict(vpath, mut, sync_engine)
                    mut.update(status='conflict', conflict_path=conflict_rel)
                    conflict_count += 1
                else:
                    desired = _updated_note(sync_engine, raw, payload)
                    try:
                        res = sync_engine.note_edit({'source': source, 'expected_sha256': expected,
                            'edits': [{'old': raw, 'new': desired}]})
                        mut.update(status='applied', result=_compact(res))
                        applied_count += 1
                    except NoteConflict as exc:
                        if getattr(exc, 'write_occurred', False):
                            raise  # written but unverified: never pile a conflict copy on top
                        conflict_rel = isolate_conflict(vpath, mut, sync_engine)
                        mut.update(status='conflict', conflict_path=conflict_rel, error=str(exc))
                        conflict_count += 1

            elif action == "task_create":
                if target_path.is_file():
                    stored_meta, stored_text = parse(target_path.read_bytes().decode('utf-8'))
                    meta, _ = sync_engine._protect_metadata(payload.get('metadata') or {})
                    text, _ = sync_engine._protect(payload.get('text', ''))
                    if (meta.get('id') and stored_meta.get('kind') == 'task' and
                            stored_meta.get('revision') == 1 and
                            all(stored_meta.get(k) == v for k, v in meta.items()) and
                            stored_text == text + '\n'):
                        mut.update(status='applied', idempotent=True)
                        applied_count += 1
                    else:
                        conflict_rel = isolate_conflict(vpath, mut, sync_engine)
                        mut.update(status='conflict', conflict_path=conflict_rel)
                        conflict_count += 1
                else:
                    res = sync_engine.task_create(source, payload.get("text", ""), payload.get("metadata"))
                    mut["status"] = "applied"
                    mut["result"] = _compact(res)
                    applied_count += 1

            elif action == "task_update":
                try:
                    res = sync_engine.update_task(
                        payload["id"],
                        payload["expected_revision"],
                        payload["changes"],
                    )
                    mut["status"] = "applied"
                    mut["result"] = _compact(res)
                    applied_count += 1
                except RevisionConflict as task_exc:
                    # A task_update has no conflict copy; the record stays for review.
                    if _task_reflects(sync_engine, payload):
                        mut.update(status='applied', idempotent=True, reconciled=True)
                        applied_count += 1
                    else:
                        mut["status"] = "conflict"
                        mut["error"] = str(task_exc)
                        conflict_count += 1

            elif action == "receipt":
                try:
                    res = sync_engine.receipt(
                        payload["event_id"],
                        payload["summary"],
                        payload["refs"],
                        payload.get("harness", "manual"),
                        session=payload.get("session"),
                        links=payload.get("links"),
                    )
                    mut["status"] = "applied"
                    mut["result"] = _compact(res)
                    applied_count += 1
                except ReceiptConflict as receipt_exc:
                    mut["status"] = "conflict"
                    mut["error"] = str(receipt_exc)
                    conflict_count += 1

        except Exception as exc:
            mut["status"] = "failed"
            mut["error"] = f"{type(exc).__name__}: {str(exc)}"[:240]
            failed_count += 1

        mut["applied_at"] = time.time()
        _ack(state_dir, mut)
        processed.append(mut)

    if applied_count > 0:
        try:
            sync_engine.sync()
        except Exception:
            pass

    return {
        "status": "succeeded",
        "total": len(all_mutations),
        "replayed": len(processed),
        "applied": applied_count,
        "conflicts": conflict_count,
        "failed": failed_count,
        "mutations": processed,
    }


def clear_applied(state: str | Path) -> dict:
    if not _queue_path(state).is_file():
        return {"status": "succeeded", "pruned": 0, "remaining": 0}
    with _queue_lock(state):
        return _clear_applied(state)


def _clear_applied(state: str | Path) -> dict:
    """Remove applied and dismissed mutations, leaving pending, conflict and failed entries."""
    state_dir = Path(state)
    all_mutations = list_mutations(state_dir)
    remaining = [m for m in all_mutations if m.get("status") not in ("applied", "dismissed")]
    pruned = len(all_mutations) - len(remaining)
    if pruned:
        _save_all_mutations(state_dir, remaining)
    return {"status": "succeeded", "pruned": pruned, "remaining": len(remaining)}


def resolve(state, mutation_id, retry=False):
    """Retry (back to pending) or dismiss one reviewed conflict/failed mutation."""
    if not _queue_path(state).is_file():
        raise ValueError("unknown mutation id")
    with _queue_lock(state):
        mutations = list_mutations(state)
        item = next((m for m in mutations if m["mutation_id"] == mutation_id), None)
        if item is None:
            raise ValueError("unknown mutation id")
        if item.get("status") not in ("conflict", "failed"):
            raise ValueError("only conflict or failed mutations can be retried or dismissed")
        item["status"] = "pending" if retry else "dismissed"
        item["resolved_at"] = time.time()
        if retry:
            item["error"] = None  # a conflict copy from the earlier attempt stays listed in conflict_path
        _save_all_mutations(state, mutations)
        return {key: item.get(key) for key in ("mutation_id", "status", "action", "source", "conflict_path")}


def status(state: str | Path) -> dict:
    """Read-only status for the mutation-queue command and doctor; never creates anything."""
    settings, valid = read_settings(state)
    result = {"enabled": settings["enabled"], "valid": valid,
              "identity": get_machine_id(state, create=False) is not None,
              "queue_exists": False, "pending_count": 0, "conflict_count": 0,
              "applied_count": 0, "failed_count": 0, "dismissed_count": 0}
    try:
        queue_file = _queue_path(state)
        result["queue_exists"] = queue_file.is_file()
        all_mutations = list_mutations(state) if result["queue_exists"] else []
    except ValueError as exc:
        result.update(status="invalid", error=str(exc)[:240])
        return result
    for name in ("pending", "conflict", "applied", "failed", "dismissed"):
        result[name + "_count"] = sum(1 for m in all_mutations if m.get("status") == name)
    result["status"] = ("needs_attention" if result["conflict_count"] or result["failed_count"] or not valid
                        else "pending" if result["pending_count"] else "ok")
    return result


def doctor(state: str | Path) -> dict:
    """Read-only diagnostic for doctor: only the switch while the queue is off and absent."""
    settings, valid = read_settings(state)
    try:
        exists = _queue_path(state).is_file()
    except ValueError:
        exists = True  # a linked queue file is reported as invalid below
    if valid and not settings["enabled"] and not exists:
        return {"enabled": False}
    return status(state)
