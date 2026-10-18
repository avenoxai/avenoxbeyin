"""Offline mutation queue and multi-device local-first synchronization support (#205).

Provides anonymous device/machine identity, append-only offline mutation queue
(state/mutation-queue.jsonl), idempotent replay against local sync engine, and
conflict isolation adhering to .sync-conflict-*.md file-sync semantics.
Standard library only; zero external dependencies.
"""
from __future__ import annotations

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


def get_machine_id(state: str | Path) -> str:
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
    return Path(state) / MUTATION_QUEUE_FILE


def enqueue_mutation(
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
    try:
        with open(queue_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and "mutation_id" in item:
                        if status is None or item.get("status") == status:
                            mutations.append(item)
                except ValueError:
                    continue
    except OSError:
        return []
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


def isolate_conflict(vault: str | Path, mutation: dict) -> str:
    """Create a conflict copy matching the file-sync conflict pattern:

    .*\\.sync-conflict-\\d{8}(?:-\\d{6})?-[0-9A-Za-z]+\\.md
    """
    vpath = Path(vault)
    source = mutation["source"]
    orig_path = Path(source)
    stem = orig_path.stem
    parent = orig_path.parent

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    machine_prefix = mutation.get("machine_id", "local")[:8]
    conflict_name = f"{stem}.sync-conflict-{stamp}-{machine_prefix}.md"
    conflict_rel = parent / conflict_name
    conflict_target = vpath / conflict_rel
    conflict_target.parent.mkdir(parents=True, exist_ok=True)

    payload = mutation.get("payload", {})
    text = payload.get("text", "")
    metadata = payload.get("metadata")

    if metadata and isinstance(metadata, dict):
        content = "---\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n---\n" + text
    else:
        content = text

    if not content.endswith("\n"):
        content += "\n"

    conflict_target.write_text(content, encoding="utf-8")
    return conflict_rel.as_posix()


def replay_mutations(vault: str | Path, state: str | Path, sync_engine=None) -> dict:
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
        target_path = vpath / source

        try:
            if action == "note_create":
                if target_path.is_file():
                    text = payload.get("text", "")
                    meta = payload.get("metadata")
                    expected_content = ("---\n" + json.dumps(meta, ensure_ascii=False, indent=2) + "\n---\n" + text) if meta else text
                    if not expected_content.endswith("\n"):
                        expected_content += "\n"
                    if target_path.read_text(encoding="utf-8") == expected_content:
                        mut["status"] = "applied"
                        mut["idempotent"] = True
                        applied_count += 1
                    else:
                        conflict_rel = isolate_conflict(vpath, mut)
                        mut["status"] = "conflict"
                        mut["conflict_path"] = conflict_rel
                        conflict_count += 1
                else:
                    res = sync_engine.note_create(source, payload.get("text", ""), payload.get("metadata"))
                    mut["status"] = "applied"
                    mut["result"] = res
                    applied_count += 1

            elif action == "note_update":
                if hasattr(sync_engine, "note_update"):
                    try:
                        res = sync_engine.note_update(
                            source,
                            payload.get("expected_hash", ""),
                            text=payload.get("text"),
                            metadata=payload.get("metadata"),
                        )
                        mut["status"] = "applied"
                        mut["result"] = res
                        applied_count += 1
                    except Exception as conflict_exc:
                        conflict_rel = isolate_conflict(vpath, mut)
                        mut["status"] = "conflict"
                        mut["conflict_path"] = conflict_rel
                        mut["error"] = str(conflict_exc)
                        conflict_count += 1
                else:
                    if not target_path.is_file():
                        conflict_rel = isolate_conflict(vpath, mut)
                        mut["status"] = "conflict"
                        mut["conflict_path"] = conflict_rel
                        conflict_count += 1
                    else:
                        cur_raw = target_path.read_bytes()
                        cur_hash = hashlib.sha256(cur_raw).hexdigest()
                        exp = payload.get("expected_hash", "").strip().lower()
                        if cur_hash.startswith(exp):
                            text = payload.get("text", "")
                            meta = payload.get("metadata")
                            new_content = ("---\n" + json.dumps(meta, ensure_ascii=False, indent=2) + "\n---\n" + text) if meta else text
                            if not new_content.endswith("\n"):
                                new_content += "\n"
                            target_path.write_text(new_content, encoding="utf-8")
                            mut["status"] = "applied"
                            applied_count += 1
                        else:
                            conflict_rel = isolate_conflict(vpath, mut)
                            mut["status"] = "conflict"
                            mut["conflict_path"] = conflict_rel
                            conflict_count += 1

            elif action == "task_create":
                if target_path.is_file():
                    conflict_rel = isolate_conflict(vpath, mut)
                    mut["status"] = "conflict"
                    mut["conflict_path"] = conflict_rel
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

    _save_all_mutations(state_dir, all_mutations)

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
            "machine_id": get_machine_id(state_dir),
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
        "machine_id": get_machine_id(state_dir),
        "queue_exists": True,
        "pending_count": pending,
        "conflict_count": conflicts,
        "applied_count": applied,
        "failed_count": failed,
    }
