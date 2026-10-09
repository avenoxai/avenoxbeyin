"""Read-preserving V2 cutover guards and receipts; no legacy replay/model calls."""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil

LEGACY_RUNNERS = ['.claude/scripts/flush.py', '.claude/scripts/compile.py'] + ['.claude/hooks/'+name+suffix for name in ('session-start', 'session-end', 'pre-compact', 'prompt-counter') for suffix in ('.sh', '.ps1')]


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_root(vault):
    path = vault/'.claude/scripts/.state'
    cursor = path
    while cursor != vault:
        if cursor.is_symlink():
            raise RuntimeError('legacy state symlink requires explicit reconciliation')
        cursor = cursor.parent
    return path


def _inventory(vault):
    from beyin_v3_sync import EXCLUDED_FILES
    sources = {}
    for directory, dirs, files in os.walk(vault, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith('.') and name != 'node_modules' and not (Path(directory)/name).is_symlink()]
        for name in files:
            p = Path(directory)/name
            if name.lower().endswith('.md') and name.casefold() not in EXCLUDED_FILES and not p.is_symlink():
                sources[p.relative_to(vault).as_posix()] = _hash(p)
    legacy = _legacy_root(vault)
    # Lock files are coordination artifacts, not user state. On Windows their
    # mandatory locks also make reopening them for hashing fail against the
    # installer process's own migration guard.
    states = {p.relative_to(vault).as_posix(): _hash(p) for p in legacy.rglob('*')
              if p.is_file() and not p.is_symlink() and p.suffix.casefold() != '.lock'} if legacy.exists() else {}
    return sources, states


@contextmanager
def _legacy_lock(path):
    with path.open('r+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise RuntimeError('legacy writer lock is active; wait for completion') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def migration_guard(vault_root, state_dir):
    vault, state = Path(vault_root).resolve(), Path(state_dir).resolve()
    if not vault.is_dir():
        raise ValueError('existing vault required')
    if state.is_relative_to(vault):
        raise ValueError('migration state must be outside vault')
    legacy = _legacy_root(vault)
    orphaned = []
    with ExitStack() as stack:
        if legacy.exists():
            held = set()
            for path in sorted(legacy.glob('*.lock')):
                if path.is_symlink():
                    raise RuntimeError('legacy writer lock symlink requires review')
                stack.enter_context(_legacy_lock(path))
                held.add(path.name)
            for path in legacy.glob('*.json'):
                if not path.name.startswith(('flush-', 'compile', 'antigravity-')):
                    continue
                try:
                    item = json.loads(path.read_text(encoding='utf-8'))
                except (ValueError, UnicodeError):
                    raise RuntimeError('legacy writer state unreadable; review locally')
                if isinstance(item, dict) and any(item.get(key) in ('inflight', 'running', 'pending') for key in ('status', 'state')):
                    relative = path.relative_to(vault).as_posix()
                    # A V2 flush takes flush-<key>.lock before it writes status "inflight" to
                    # flush-<key>.json and keeps it until its terminal status. Holding that lock
                    # here is proof this writer is gone (a logout or shutdown mid-summary), which
                    # the sentinel's age alone never was. No other record or state has that
                    # guarantee: a "pending" one, for instance, waits without holding any lock.
                    if (path.name.startswith('flush-') and item.get('status') == 'inflight'
                            and item.get('state') not in ('inflight', 'running', 'pending')
                            and path.with_suffix('.lock').name in held):
                        orphaned.append(relative)
                        continue
                    raise RuntimeError('legacy writer inflight sentinel in ' + relative +
                                       '; verify the prior write completed, then reconcile that record before migration:'
                                       ' with no V2 writer running, set its status to a terminal value such as "fail" and rerun')
        sources, states = _inventory(vault)
        prior = state/'v2-migration.json'
        plan = {'migration': 'v2-to-v3-source-cutover-1', 'cutover_at': datetime.now(timezone.utc).isoformat(),
                'sources': sources, 'legacy_state': states,
                'historical_receipts': sorted(p.relative_to(vault).as_posix() for p in (vault/'receipts').glob('*.md'))}
        if orphaned:
            plan['orphaned_legacy_state'] = sorted(orphaned)
        if prior.exists():
            plan['previous'] = json.loads(prior.read_text(encoding='utf-8'))
        # Runners the user kept at install time stay theirs on every later update.
        manifest = state/'v3-install.json'
        if manifest.exists():
            kept = json.loads(manifest.read_text(encoding='utf-8')).get('kept_legacy', [])
            if kept:
                plan['kept_legacy'] = sorted(kept)
        yield plan


def finalize_migration(vault_root, state_dir, plan):
    from beyin_v3_sync import atomic
    vault, state = Path(vault_root).resolve(), Path(state_dir).resolve()
    kept = set(plan.get('kept_legacy', []))
    for name in LEGACY_RUNNERS:
        path = vault/name
        if path.is_symlink():
            raise RuntimeError('legacy runner symlink requires explicit reconciliation')
        if name in kept:
            continue
        if path.exists() and ('BEYIN_V3_LEGACY_RETIRED' not in path.read_text(encoding='utf-8')):
            raise RuntimeError('legacy runner not retired; use supported installer cutover')
    if plan.get('previous'):
        # An earlier cutover already left its success receipt, so there is nothing to prove or
        # write here. The plan's source digests may be old (recovering an interrupted update),
        # and a note edited since must not make recovery impossible.
        return plan['previous']
    sources, states = _inventory(vault)
    if any(sources.get(name) != digest for name, digest in plan['sources'].items()) or states != plan['legacy_state']:
        raise RuntimeError('source or legacy state changed during migration; no success receipt')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    written = []
    try:
        for name in plan['legacy_state']:
            destination = state/'v2-preserved-state'/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Recorded before the copy: a copy that fails half way leaves a partial file too.
            written.append(destination)
            shutil.copyfile(vault/name, destination)
            destination.chmod(0o600)
        result = dict(plan, status='succeeded', historical_replay=False,
                      compiler_policy='New outcomes use explicit semantic receipts and knowledge notes; no automatic model compiler. External schedules require operator review.')
        atomic(state/'v2-migration.json', json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except Exception:
        # Rollback partial state copy
        for path in written:
            path.unlink(missing_ok=True)
        raise


def migrate_v2(vault_root, state_dir):
    with migration_guard(vault_root, state_dir) as plan:
        return finalize_migration(vault_root, state_dir, plan)
