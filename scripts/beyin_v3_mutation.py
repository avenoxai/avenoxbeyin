"""Offline mutation queue and multi-device local-first synchronization support (#205).

Provides anonymous device/machine identity, append-only offline mutation queue
(state/mutation-queue.jsonl), idempotent replay against local sync engine, and
conflict isolation adhering to .sync-conflict-*.md file-sync semantics.
Standard library only; zero external dependencies.
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
VALID_ACTIONS = ("note_create", "note_update", "task_create", "task_update", "receipt")
VALID_STATUSES = ("pending", "applied", "conflict", "failed")


def get_machine_id(state: str | Path, *, create=True) -> str | None:
    """Return persistent, anonymous 32-hex machine identifier.

    Reuses existing parallel presence device id if available, or reads/generates
    state/device.json. Never leaks user names, hostnames, or paths.
    """
    state_dir = Path(state)
    presence_device = state_dir / "parallel-presence-device.json"
    if presence_device.is_file() and not presence_device.is_symlink():
        try:
            data = json.loads(presence_device.read_text(encoding="utf-8"))
            dev = data.get("device")
            if isinstance(dev, str) and re.fullmatch(r"[0-9a-f]{32}", dev):
                return dev
        except (OSError, ValueError):
            pass

    device_file = state_dir / "device.json"
    if device_file.is_file() and not device_file.is_symlink():
        try:
            data = json.loads(device_file.read_text(encoding="utf-8"))
            dev = data.get("device")
            if isinstance(dev, str) and re.fullmatch(r"[0-9a-f]{32}", dev):
                return dev
        except (OSError, ValueError):
            pass

    if not create:
        return None
    new_id = uuid.uuid4().hex
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dev-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "device": new_id}, f)
        os.replace(tmp, device_file)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return new_id
    return new_id


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


def enqueue_mutation(state, action, source, payload, vault=None):
    with _queue_lock(state):
        return _enqueue_mutation(state, action, source, payload, vault)


def _enqueue_mutation(
    state: str | Path,
    action: str,
    source: str,
    payload: dict,
    vault: str | Path | None = None,
) -> dict:
    """Enqueue an offline mutation to state/mutation-queue.jsonl."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"invalid mutation action: {action}; must be one of {VALID_ACTIONS}")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string path")
    if Path(source).is_absolute() or ".." in Path(source).parts or "\\" in source:
        raise ValueError("relative vault source required")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dictionary")

    state_dir = Path(state)
    state_dir.mkdir(parents=True, exist_ok=True)
    machine_id = get_machine_id(state_dir)

    now = time.time()
    iso_time = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
    raw_hash_input = f"{machine_id}:{now}:{action}:{source}:{json.dumps(payload, sort_keys=True)}"
    mutation_id = hashlib.sha256(raw_hash_input.encode("utf-8")).hexdigest()[:24]

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
    with open(queue_file, "a", encoding="utf-8") as f:
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
    state_dir = Path(state)
    state_dir.mkdir(parents=True, exist_ok=True)
    queue_file = _queue_path(state_dir)

    fd, tmp = tempfile.mkstemp(prefix=".mqueue-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for item in mutations:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, queue_file)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


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


def replay_mutations(vault, state, sync_engine=None):
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
        try:
            import beyin_v3_sync
            sync_engine = beyin_v3_sync.SyncEngine(vpath, state_dir)
        except Exception as exc:
            return {
                "status": "failed",
                "error": type(exc).__name__,
                "message": str(exc),
                "total": len(all_mutations),
                "replayed": 0,
            }

    applied_count = 0
    conflict_count = 0
    failed_count = 0
    processed = []

    for mut in pending:
        action = mut["action"]
        source = mut["source"]
        payload = mut.get("payload", {})
        try:
            if action not in VALID_ACTIONS:
                raise ValueError("unknown mutation action")
            from beyin_v3_notes import source_path, NoteConflict
            from beyin_v3_sync import parse, render
            target_path = sync_engine._path(source)
            if action in ('note_create', 'note_update'):
                target_path = source_path(sync_engine, source)
            if action == "note_create":
                if target_path.is_file():
                    text, _ = sync_engine._protect(payload.get('text', ''))
                    meta, _ = sync_engine._protect_metadata(payload.get('metadata') or {})
                    expected_content = render(meta, text + '\n')
                    if target_path.read_text(encoding="utf-8") == expected_content:
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
                    mut["result"] = res
                    applied_count += 1

            elif action == "note_update":
                expected = payload.get('expected_hash')
                if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                    raise ValueError('note_update requires a full SHA-256 expected_hash')
                snapshot = sync_engine.note_read(source)
                raw = snapshot['content']
                if raw is None or snapshot['sha256'] != expected:
                    conflict_rel = isolate_conflict(vpath, mut, sync_engine)
                    mut.update(status='conflict', conflict_path=conflict_rel)
                    conflict_count += 1
                else:
                    meta, _ = parse(raw)
                    meta.update(payload.get('metadata') or {})
                    text, _ = sync_engine._protect(payload.get('text', ''))
                    meta, _ = sync_engine._protect_metadata(meta)
                    desired = render(meta, text.rstrip('\n') + '\n')
                    try:
                        res = sync_engine.note_edit({'source': source, 'expected_sha256': expected,
                            'edits': [{'old': raw, 'new': desired}]})
                        mut.update(status='applied', result=res)
                        applied_count += 1
                    except NoteConflict as exc:
                        conflict_rel = isolate_conflict(vpath, mut, sync_engine)
                        mut.update(status='conflict', conflict_path=conflict_rel, error=str(exc))
                        conflict_count += 1

            elif action == "task_create":
                if target_path.is_file():
                    stored_meta, stored_text = parse(target_path.read_text(encoding='utf-8'))
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
                    mut["result"] = res
                    applied_count += 1

            elif action == "task_update":
                try:
                    res = sync_engine.update_task(
                        payload["id"],
                        payload["expected_revision"],
                        payload.get("changes", {}),
                    )
                    mut["status"] = "applied"
                    mut["result"] = res
                    applied_count += 1
                except Exception as task_exc:
                    mut["status"] = "conflict"
                    mut["error"] = str(task_exc)
                    conflict_count += 1

            elif action == "receipt":
                res = sync_engine.receipt(
                    payload["event_id"],
                    payload["summary"],
                    payload["refs"],
                    payload.get("harness", "manual"),
                    session=payload.get("session"),
                    links=payload.get("links"),
                )
                mut["status"] = "applied"
                mut["result"] = res
                applied_count += 1

        except Exception as exc:
            mut["status"] = "failed"
            mut["error"] = f"{type(exc).__name__}: {str(exc)}"
            failed_count += 1

        mut["applied_at"] = time.time()
        processed.append(mut)

    updates = {m['mutation_id']: m for m in all_mutations}
    latest = list_mutations(state_dir)
    _save_all_mutations(state_dir, [updates.get(m['mutation_id'], m) for m in latest])

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
    with _queue_lock(state):
        return _clear_applied(state)


def _clear_applied(state: str | Path) -> dict:
    """Remove applied mutations from queue, leaving pending and conflict entries."""
    state_dir = Path(state)
    all_mutations = list_mutations(state_dir)
    remaining = [m for m in all_mutations if m.get("status") != "applied"]
    pruned = len(all_mutations) - len(remaining)
    _save_all_mutations(state_dir, remaining)
    return {"status": "succeeded", "pruned": pruned, "remaining": len(remaining)}


def doctor(state: str | Path) -> dict:
    """Read-only diagnostic inspection for doctor."""
    state_dir = Path(state)
    queue_file = _queue_path(state_dir)
    if not queue_file.is_file():
        return {
            "status": "ok",
            "machine_id": get_machine_id(state_dir, create=False),
            "queue_exists": False,
            "pending_count": 0,
            "conflict_count": 0,
            "applied_count": 0,
            "failed_count": 0,
        }

    all_mutations = list_mutations(state_dir)
    pending = sum(1 for m in all_mutations if m.get("status") == "pending")
    conflicts = sum(1 for m in all_mutations if m.get("status") == "conflict")
    applied = sum(1 for m in all_mutations if m.get("status") == "applied")
    failed = sum(1 for m in all_mutations if m.get("status") == "failed")

    status = "needs_attention" if (conflicts > 0 or failed > 0) else "pending" if pending > 0 else "ok"
    return {
        "status": status,
        "machine_id": get_machine_id(state_dir, create=False),
        "queue_exists": True,
        "pending_count": pending,
        "conflict_count": conflicts,
        "applied_count": applied,
        "failed_count": failed,
    }
