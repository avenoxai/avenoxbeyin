"""Shared vault skills; no third-party packages or global skill imports."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid


def _validate_manifest(content):
    if b'\0' in content:
        raise ValueError('skill contains binary files')
    text = content.decode('utf-8', errors='replace')
    m = re.match(r'^---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.DOTALL)
    if not m:
        raise ValueError('skill missing frontmatter')
    fm = m.group(1)
    if not re.search(r'^name\s*:', fm, re.MULTILINE) or not re.search(r'^description\s*:', fm, re.MULTILINE):
        raise ValueError('skill missing name or description')
    for line in fm.splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        if not re.match(r'^([^\W\d][\w-]*):', line) and not line.startswith((' ', '\t', '-')):
            raise ValueError(f'invalid YAML frontmatter line: {line!r}')


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
            data = p.read_bytes()
            if p.name == 'SKILL.md':
                _validate_manifest(data)
            elif b'\0' in data:
                raise ValueError('skill contains binary files')
            digest.update(p.relative_to(path).as_posix().encode());digest.update(b'\0');digest.update(data)
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
    left=vault/'.agents/skills';right=vault/'.claude/skills'
    for p in (left,right):
        if not p.resolve().is_relative_to(vault):raise ValueError('skill root escapes vault')
        p.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(state/'skills.sqlite3',timeout=10)
    result={'synced':[],'conflicts':[],'unmanaged':[],'mode':mode}
    try:
        db.execute('CREATE TABLE IF NOT EXISTS skills(name TEXT PRIMARY KEY, hash TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        # Under the lock, so two syncs never race for the same parked folder.
        relocated=[name for root in (right,left) for name in _relocate_parked(root)]
        if relocated:result['relocated_backups']=relocated
        names=sorted({p.name for root in (left,right) for p in root.iterdir() if not p.name.startswith('.')})
        for name in names:
            a=left/name;b=right/name
            try:
                a_present=a.exists() or a.is_symlink();b_present=b.exists() or b.is_symlink()
                a_external=a_present and not a.resolve().is_relative_to(vault)
                b_external=b_present and not b.resolve().is_relative_to(vault)
                if a_external or b_external:
                    # External project skills are intentionally outside this
                    # vault's ownership. Never traverse, copy, or mirror them.
                    # Matching dual links and one-sided links remain usable by
                    # their host without wedging the V3 source queue.
                    if ((a_external and b_external and a.resolve() == b.resolve()) or
                            (a_external and not b_present) or (b_external and not a_present)):
                        result['unmanaged'].append(name)
                        continue
                    raise ValueError('external skill collides with managed skill')
                old=db.execute('SELECT hash FROM skills WHERE name=?',(name,)).fetchone()
                previous=old[0] if old else None
                if not previous and not any((p.is_dir() and (p/'SKILL.md').is_file()) for p in (a,b)):
                    # Licenses, shared helper folders and caches live beside skills.
                    # Never owned, so never a conflict; a skill that LOST its
                    # SKILL.md still has a row and stays a conflict below.
                    result['unmanaged'].append(name)
                    continue
                ah=_tree(a,vault) if a.exists() or a.is_symlink() else None
                bh=_tree(b,vault) if b.exists() or b.is_symlink() else None
                if ah and bh and a.resolve()==b.resolve():h=ah
                elif ah==bh:h=ah
                elif not ah or not bh:
                    if previous:raise ValueError('one-sided removal requires review')
                    source,target=(a,b) if ah else (b,a)
                    if target==b and mode=='symlink':
                        try:target.symlink_to(os.path.relpath(source,target.parent),target_is_directory=True)
                        except OSError:_copy(source,target,None,vault)
                    else:_copy(source,target,None,vault)
                    h=ah or bh
                elif previous==ah and previous!=bh:
                    _copy(b,a,ah,vault);h=bh
                elif previous==bh and previous!=ah:
                    _copy(a,b,bh,vault);h=ah
                else:raise ValueError('both skill versions changed')
                if h:
                    db.execute('INSERT OR REPLACE INTO skills VALUES (?,?)',(name,h));result['synced'].append(name)
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
    other=vault/'.claude/skills'/name
    if other.exists() or other.is_symlink():raise ValueError('skill name already exists in Claude')
    target.parent.mkdir(parents=True,exist_ok=True)
    _copy(source,target,None,vault,source_root=source)
    return sync_skills(vault,state,mode=mode)
