"""Shared vault skills; no third-party packages or global skill imports."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid


def _tree(path, vault):
    if not path.resolve().is_relative_to(vault):
        raise ValueError('skill escapes vault')
    if not path.is_dir() or not (path / 'SKILL.md').is_file():
        raise ValueError('skill must contain SKILL.md')
    digest = hashlib.sha256()
    for p in sorted(path.rglob('*')):
        if p.is_symlink():
            raise ValueError('nested skill symlink requires manual review')
        if p.is_file():
            digest.update(p.relative_to(path).as_posix().encode());digest.update(b'\0');digest.update(p.read_bytes())
    return digest.hexdigest()


# Harnesses load every folder under a skills root that holds a SKILL.md, dot-named or
# not, so staging and reversible backups live in a sibling of the root (#157).
BACKUP_DIR = '.skill-backups'
PARKED = ('.v3-backup-', '.v3-skill-')


def _backup_root(skills_root):
    root = Path(skills_root).parent / BACKUP_DIR
    root.mkdir(parents=True, exist_ok=True)
    # Backups pile up with every mirrored edit; keep them out of a versioned vault.
    ignore = root / '.gitignore'
    if not ignore.exists():
        try: ignore.write_text('*\n', encoding='utf-8')
        except OSError: pass
    return root


def _relocate_parked(skills_root):
    """Move backups and stale stages older releases left inside a skills root.

    Renamed, never deleted: a backup is the user's reversible original.
    """
    moved = []
    try: entries = [p for p in skills_root.iterdir() if p.name.startswith(PARKED)]
    except OSError: return moved
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            destination = _backup_root(skills_root) / entry.name
            if destination.exists() or destination.is_symlink():
                continue
            entry.rename(destination)
            moved.append(entry.name)
        except OSError:
            continue
    return moved


def _copy(source, target, expected, vault, source_root=None):
    if target.exists() and _tree(target, vault) != expected:
        raise ValueError('skill changed concurrently')
    backup_root = _backup_root(target.parent)
    stage = Path(tempfile.mkdtemp(prefix='.v3-skill-', dir=backup_root))
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True)
        if _tree(stage, vault) != _tree(source, source_root or vault):
            raise ValueError('source changed during copy')
        if target.exists():
            if target.is_symlink() or _tree(target, vault) != expected:
                raise ValueError('skill changed concurrently')
            backup = backup_root / ('.v3-backup-' + target.name + '-' + uuid.uuid4().hex)
            target.rename(backup)  # Preserved reversible original, never discarded.
            try: stage.rename(target)
            except BaseException:
                backup.rename(target);raise
        else: stage.rename(target)
    finally:
        if stage.exists():shutil.rmtree(stage)


def sync_skills(vault, state, mode=None):
    vault=Path(vault).resolve();state=Path(state).resolve()
    if state.is_relative_to(vault):raise ValueError('skill state must be outside vault')
    mode=mode or ('copy' if os.name=='nt' else 'symlink')
    if mode not in ('copy','symlink'):raise ValueError('invalid skill mode')
    state.mkdir(parents=True,exist_ok=True)
    roots=(vault/'.agents/skills',vault/'.claude/skills',vault/'.kiro/skills')
    for p in roots:
        if not p.resolve().is_relative_to(vault):raise ValueError('skill root escapes vault')
        p.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(state/'skills.sqlite3',timeout=10)
    result={'synced':[],'conflicts':[],'unmanaged':[],'mode':mode}
    try:
        db.execute('CREATE TABLE IF NOT EXISTS skills(name TEXT PRIMARY KEY, hash TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        # Under the lock, so two syncs never race for the same parked folder.
        relocated=[name for root in roots for name in _relocate_parked(root)]
        if relocated:result['relocated_backups']=relocated
        existing=dict(db.execute('SELECT name, hash FROM skills').fetchall())
        names=sorted({p.name for root in roots for p in root.iterdir() if not p.name.startswith('.')})
        for name in names:
            paths=[root/name for root in roots]
            try:
                present=[p.exists() or p.is_symlink() for p in paths]
                external=[exists and not p.resolve().is_relative_to(vault) for p,exists in zip(paths,present)]
                if any(external):
                    # External project skills are intentionally outside this
                    # vault's ownership. Never traverse, copy, or mirror them.
                    # Matching links and one-sided links remain usable by
                    # their host without wedging the V3 source queue.
                    if (not any(exists and not outside for exists,outside in zip(present,external)) and
                            len({p.resolve() for p,outside in zip(paths,external) if outside}) == 1):
                        result['unmanaged'].append(name)
                        continue
                    raise ValueError('external skill collides with managed skill')
                previous=existing.get(name)
                if not previous and not any((p.is_dir() and (p/'SKILL.md').is_file()) for p in paths):
                    # Licenses, shared helper folders and caches live beside skills.
                    # Never owned, so never a conflict; a skill that LOST its
                    # SKILL.md still has a row and stays a conflict below.
                    result['unmanaged'].append(name)
                    continue
                hashes=[_tree(p,vault) if exists else None for p,exists in zip(paths,present)]
                if previous and any(h is None for h in hashes[:2]):
                    raise ValueError('one-sided removal requires review')
                # Kiro is a derived mirror. Recreate a missing copy from a reviewed
                # shared source, including after rollback from a Kiro-aware release.
                changed={h for h in hashes if h and h!=previous}
                if len(changed)>1:
                    raise ValueError('multiple skill versions changed')
                h=next(iter(changed), previous)
                if not h:
                    raise ValueError('skill source missing')
                source=next(p for p,value in zip(paths,hashes) if value==h)
                for target in paths:
                    current=_tree(target,vault) if target.exists() or target.is_symlink() else None
                    if current==h:continue
                    if current is None and mode=='symlink' and target!=paths[0]:
                        try:target.symlink_to(os.path.relpath(source,target.parent),target_is_directory=True)
                        except OSError:_copy(source,target,None,vault)
                    else:_copy(source,target,current,vault)
                if h:
                    db.execute('INSERT OR REPLACE INTO skills VALUES (?,?)',(name,h));existing[name]=h;result['synced'].append(name)
            except (ValueError,OSError):result['conflicts'].append(name)
        db.commit()
    finally:db.close()
    return result


def import_skill(vault, state, source, name=None, mode=None):
    """Copy only an explicitly selected skill; never scan global skill stores."""
    vault=Path(vault).resolve();source=Path(source).expanduser().resolve()
    name=name or source.name
    if not name or name.startswith('.') or '/' in name or '\\' in name or name in ('.','..'):
        raise ValueError('invalid skill name')
    _tree(source,source)  # Reject all nested symlinks before importing assets.
    target=vault/'.agents/skills'/name
    if not target.parent.resolve().is_relative_to(vault):raise ValueError('skill root escapes vault')
    if target.exists() or target.is_symlink():raise ValueError('skill name already exists')
    for root in ('.claude', '.kiro'):
        other=vault/root/'skills'/name
        if other.exists() or other.is_symlink():raise ValueError('skill name already exists in '+root)
    target.parent.mkdir(parents=True,exist_ok=True)
    _copy(source,target,None,vault,source_root=source)
    return sync_skills(vault,state,mode=mode)
