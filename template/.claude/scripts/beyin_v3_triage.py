"""Triage and digestion routines: inbox scanning, stale tasks, and weekly knowledge digest."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import time

try:
    from beyin_v3_hygiene import (
        fold,
        sensitive_excluded,
        companion_names,
        INBOX_WORDS,
        TERMINAL_STATUSES,
        _inbox_folder,
        CODE_DIRS,
        inbox_report,
        read_inbox_settings,
    )
except ImportError:
    _TR_FOLD = str.maketrans({'ş': 's', 'ğ': 'g', 'ü': 'u', 'ö': 'o', 'ç': 'c', 'ı': 'i', 'â': 'a', 'î': 'i', 'û': 'u'})

    def fold(text):
        import unicodedata
        t = unicodedata.normalize('NFC', str(text or '')).replace('İ', 'i').replace('I', 'i').lower()
        return unicodedata.normalize('NFC', t.replace('\u0307', '')).translate(_TR_FOLD)

    def sensitive_excluded(name):
        return False

    def companion_names(vault, state=None):
        return {'🔮 850-Companion', 'Companion', '850-Companion'}

    INBOX_WORDS = re.compile(r'(?:inbox|inboxes|gelenkutusu|gelenkutum)')
    TERMINAL_STATUSES = {'done', 'cancelled', 'kapandi'}
    CODE_DIRS = {'node_modules', 'venv', '.venv', '__pycache__'}

    def _inbox_folder(name, configured=None):
        f = fold(name)
        return 'inbox' in f or 'gelen' in f or 'kaynak' in f or 'source' in f or 'yakala' in f

    def read_inbox_settings(state):
        return {'enabled': False, 'max_items': 10, 'max_days': 7, 'folders': []}, False

    def inbox_report(vault, state=None, max_items=10, max_days=7, now=None, folders=None):
        return {'folders': [], 'attention': False, 'max_items': max_items, 'max_days': max_days}

EXTENDED_TERMINAL_STATUSES = frozenset(TERMINAL_STATUSES | {'completed', 'tamamlandi', 'iptal', 'closed'})

DATE_PATTERN = re.compile(r'(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?')


def _is_safe_file(path: Path) -> bool:
    name = path.name
    if name.startswith('.') or name.endswith('~') or name == '.gitkeep':
        return False
    return path.is_file() and not path.is_symlink()


def _front_values(text: str, keys: tuple[str, ...], fold_values: bool = True) -> dict[str, str]:
    """Frontmatter values for given keys from JSON or flat YAML."""
    if not text.startswith('---'):
        return {}
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        return {}
    header = '\n'.join(lines[1:end]).strip()
    if header.startswith('{'):
        try:
            data = json.loads(header)
            if isinstance(data, dict):
                return {k: (fold(str(data[k])).strip() if fold_values else str(data[k]).strip())
                        for k in keys if k in data and data[k] is not None}
        except Exception:
            return {}
    found = {}
    for line in lines[1:end]:
        m = re.fullmatch(r'([^\W\d][\w-]*):[ \t]*(["\']?)([^"\'#]*?)\2[ \t]*(?:#.*)?', line.rstrip())
        if m and m[1] in keys and m[1] not in found:
            found[m[1]] = fold(m[3]).strip() if fold_values else m[3].strip()
    return found


def _parse_timestamp(val: str) -> float | None:
    """Parse a date or timestamp.

    Date-only strings (e.g. 2026-10-12) evaluate against the local calendar day,
    matching recap (#229). Timestamps with time use UTC unless offset specified.
    Also accepts Turkish calendar formats (DD.MM.YYYY, DD/MM/YYYY, DD-MM-YYYY).
    """
    if not val or not isinstance(val, str):
        return None
    val_str = val.strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?', val_str):
        try:
            dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
            return dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()
        except ValueError:
            return None
    match = DATE_PATTERN.fullmatch(val_str)
    if not match:
        m_tr = re.search(r'(\d{2})[-/. ](\d{2})[-/. ](\d{4})', val_str)
        if m_tr:
            d, m, y = int(m_tr[1]), int(m_tr[2]), int(m_tr[3])
            try:
                return datetime(y, m, d).timestamp()
            except (ValueError, OverflowError):
                return None
        m_slash = re.search(r'(\d{4})/(\d{2})/(\d{2})', val_str)
        if m_slash:
            y, m, d = int(m_slash[1]), int(m_slash[2]), int(m_slash[3])
            try:
                return datetime(y, m, d).timestamp()
            except (ValueError, OverflowError):
                return None
        return None

    year, month, day = int(match[1]), int(match[2]), int(match[3])
    has_time = bool(match[4])
    hour = int(match[4]) if match[4] else 0
    minute = int(match[5]) if match[5] else 0
    second = int(match[6]) if match[6] else 0
    try:
        if has_time:
            dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
            return dt.timestamp()
        else:
            # Naive datetime evaluates to start of local calendar day (#229 compatibility)
            dt = datetime(year, month, day)
            return dt.timestamp()
    except (ValueError, OverflowError):
        return None


def _note_frontmatter_timestamp(path: Path, front_keys: tuple[str, ...]) -> float | None:
    """Timestamp from frontmatter keys, prioritizing explicit metadata for sync immunity."""
    try:
        with path.open(encoding='utf-8', errors='replace') as handle:
            head = handle.read(4096)
        values = _front_values(head, front_keys, fold_values=False)
        for k in front_keys:
            raw = values.get(k)
            if raw:
                ts = _parse_timestamp(raw)
                if ts is not None:
                    return ts
    except Exception:
        pass
    return None


def _note_age_days(path: Path, now: float, front_keys: tuple[str, ...]) -> float:
    """Age of a note in days, prioritized by frontmatter date keys for sync immunity,

    falling back to file mtime if no valid frontmatter date is present (used for knowledge notes/outcomes).
    """
    ts = _note_frontmatter_timestamp(path, front_keys)
    if ts is not None:
        return max(0.0, (now - ts) / 86400.0)

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0

    return max(0.0, (now - mtime) / 86400.0)


def _note_age_and_source(path: Path, now: float) -> tuple[float, str]:
    """Age of a knowledge note in days and the date source used ('created', 'updated', or 'mtime').
    Prioritizes creation date for knowledge production rhythm (#469).
    """
    ts_created = _note_frontmatter_timestamp(path, ('created', 'created_at', 'date', 'tarih'))
    if ts_created is not None:
        return max(0.0, (now - ts_created) / 86400.0), 'created'

    ts_updated = _note_frontmatter_timestamp(path, ('updated_at', 'updated', 'modified'))
    if ts_updated is not None:
        return max(0.0, (now - ts_updated) / 86400.0), 'updated'

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0, 'mtime'

    return max(0.0, (now - mtime) / 86400.0), 'mtime'


def _path_key(path: Path) -> tuple:
    """Canonical identifier for a filesystem path across case-insensitive and symlinked systems."""
    try:
        resolved = Path(path).resolve()
        st = resolved.stat()
        return (st.st_dev, st.st_ino, str(resolved).lower())
    except OSError:
        try:
            return (str(Path(path).resolve()).lower(),)
        except OSError:
            return (str(path).lower(),)


def _find_top_matching_dirs(vault: Path, matcher, extra_exact: tuple[str, ...] = ()) -> list[Path]:
    vault = Path(vault).resolve()
    matched = []
    seen = set()
    try:
        entries = sorted(vault.iterdir())
    except OSError:
        return []

    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        name = entry.name
        if name.startswith('.') or sensitive_excluded(name) or name in CODE_DIRS:
            continue
        folded = fold(name)
        if matcher(name, folded) or folded in extra_exact:
            try:
                resolved = entry.resolve()
                key = _path_key(resolved)
                if key not in seen:
                    seen.add(key)
                    matched.append(resolved)
            except OSError:
                pass
    return matched


def scan_inbox(vault: Path, now: float, state: Path | None = None) -> dict:
    """Scan inbox items using the unified hygiene inbox report (#253).

    Respects user-configured --inbox-folder / inbox-report.json settings and
    index-excluded lists (#275). Only counts Markdown capture notes, ignoring
    configuration or asset files (such as web clipper JSONs).
    """
    vault = Path(vault).resolve()
    folders = None
    if state is not None:
        try:
            cfg, valid = read_inbox_settings(state)
            if valid and cfg.get('folders'):
                folders = cfg['folders']
        except Exception:
            folders = None

    try:
        rep = inbox_report(vault, state=state, now=now, folders=folders)
        folder_rows = rep.get('folders', [])
        total_notes = sum(f.get('notes', 0) for f in folder_rows)
        oldest_days = None
        for f in folder_rows:
            od = f.get('oldest_days')
            if od is not None:
                if oldest_days is None or od > oldest_days:
                    oldest_days = od

        has_yakala = any((vault / p / 'SKILL.md').is_file() for p in ('.agents/skills/beyin-yakala', '.claude/skills/beyin-yakala', '.kiro/skills/beyin-yakala'))
        return {
            'count': total_notes,
            'oldest_days': oldest_days,
            'attention': rep.get('attention', False),
            'folders': folder_rows,
            'has_yakala': has_yakala,
        }
    except Exception as exc:
        return {
            'count': 0,
            'oldest_days': None,
            'attention': False,
            'folders': [],
            'error': str(exc),
        }


def _scan_keys(vault, state):
    if state is None:
        return frozenset()
    from beyin_v3_index import exclusions
    return exclusions(state, vault)[0]


def _scan_allowed(path, vault, excluded):
    from beyin_v3_index import dir_key
    try:
        parts = path.relative_to(vault).parts[:-1]
        if any(sensitive_excluded(part) or dir_key(part) in excluded for part in parts):
            return False
        if not _is_safe_file(path):
            return False
        with path.open(encoding='utf-8', errors='replace') as handle:
            values = _front_values(handle.read(4096), ('visibility', 'trust', 'trusted'))
        return values.get('visibility') != 'private' and values.get('trust') != 'untrusted' and values.get('trusted') != 'false'
    except (ValueError, OSError):
        return False


def scan_stale_tasks(vault: Path, now: float, stale_days: int = 14, state: Path | None = None) -> dict:
    """Scan open tasks for inactivity.

    Follows core sync invariant (beyin_v3.py:637-639: 'file mtime is not a date:
    synced vaults rewrite it'). Tasks without frontmatter date are listed as
    undated (age_days: None) rather than inventing an age from file mtime.
    Open tasks include active, waiting, and blocked tasks.
    """
    vault = Path(vault).resolve()
    excluded = _scan_keys(vault, state)
    open_tasks = []
    stale_tasks = []
    undated_tasks = []
    seen_dirs = set()
    task_folders = []
    seen_files = set()

    def is_task_dir(name: str, folded: str) -> bool:
        return 'task' in folded or 'gorev' in folded

    for folder in _find_top_matching_dirs(vault, is_task_dir, extra_exact=('tasks', '500-tasks', 'gorevler')):
        try:
            res = folder.resolve()
            key = _path_key(res)
            if key not in seen_dirs:
                seen_dirs.add(key)
                task_folders.append(res)
        except OSError:
            pass

    for starter in ('tasks', '500-Tasks', 'Görevler', '✅ 500-Tasks'):
        f = vault / starter
        if f.is_dir() and not f.is_symlink():
            try:
                res = f.resolve()
                key = _path_key(res)
                if key not in seen_dirs:
                    seen_dirs.add(key)
                    task_folders.append(res)
            except OSError:
                pass

    for folder in task_folders:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in CODE_DIRS
                       and not sensitive_excluded(d) and not (Path(root) / d).is_symlink()]
            for f in files:
                file_path = Path(root) / f
                if not _scan_allowed(file_path, vault, excluded) or file_path.suffix.lower() != '.md':
                    continue

                f_key = _path_key(file_path)
                if f_key in seen_files:
                    continue
                seen_files.add(f_key)

                try:
                    text = file_path.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue

                status_val = _front_values(text[:4096], ('status', 'durum'), fold_values=True)
                status = status_val.get('status') or status_val.get('durum') or 'open'
                if not status:
                    status = 'open'

                # Check markdown content for status / checkboxes if not terminal from frontmatter
                if status not in EXTENDED_TERMINAL_STATUSES:
                    for line in text.splitlines():
                        m = re.match(r'(?i)^status:\s*([a-zA-Z_-]+)', line.strip())
                        if m:
                            st = m.group(1).lower()
                            if st in EXTENDED_TERMINAL_STATUSES:
                                status = st
                                break

                if status in EXTENDED_TERMINAL_STATUSES:
                    continue

                # Title
                raw_title_val = _front_values(text[:4096], ('title', 'baslik'), fold_values=False)
                title = raw_title_val.get('title') or raw_title_val.get('baslik') or ''
                if not title:
                    for line in text.splitlines():
                        sline = line.strip()
                        if sline.startswith('#'):
                            title = sline.lstrip('#').strip()
                            break
                if not title:
                    title = f.replace('.md', '')

                # Date priority: frontmatter ONLY (sync immunity).
                # Follow core invariant (beyin_v3.py:639): "file mtime is not a date: synced vaults rewrite it".
                ts = _note_frontmatter_timestamp(
                    file_path,
                    ('updated_at', 'updated', 'modified', 'last_modified', 'date_modified', 'created_at', 'created', 'date', 'tarih')
                )

                try:
                    rel = file_path.relative_to(vault).as_posix()
                except ValueError:
                    rel = str(file_path)

                if ts is None:
                    # Date unknown; do not invent age from mtime
                    task_info = {
                        'path': rel,
                        'title': title,
                        'status': status,
                        'age_days': None,
                    }
                    undated_tasks.append(task_info)
                    open_tasks.append(task_info)
                else:
                    age = max(0.0, (now - ts) / 86400.0)
                    task_info = {
                        'path': rel,
                        'title': title,
                        'status': status,
                        'age_days': round(age, 1),
                    }
                    open_tasks.append(task_info)
                    if age >= stale_days:
                        stale_tasks.append(task_info)

    stale_tasks.sort(key=lambda x: x['age_days'], reverse=True)
    return {
        'open_count': len(open_tasks),
        'stale_count': len(stale_tasks),
        'undated_count': len(undated_tasks),
        'stale_days_threshold': stale_days,
        'stale_tasks': stale_tasks,
        'undated_tasks': undated_tasks,
    }


def _threads_source(vault, state):
    """Choose the configured identity or the same unambiguous bootstrap discovery."""
    from beyin_v3_companion import directory
    marker = Path(state) / 'companion-bootstrap.json' if state is not None else None
    try:
        if marker is not None and (marker.exists() or marker.is_symlink()):
            if marker.is_symlink():
                return None, 'invalid_directory_marker'
            data = json.loads(marker.read_text(encoding='utf-8'))
            name = data.get('directory') if isinstance(data, dict) else None
            if (not isinstance(name, str) or not name.strip() or name != name.strip()
                    or '\\' in name or '\0' in name or PureWindowsPath(name).drive
                    or Path(name).is_absolute() or any(p in ('.', '..', '') for p in name.split('/'))):
                return None, 'invalid_directory_marker'
            target = vault / name
        else:
            target = directory(vault)
            if target is None:
                return None, 'ambiguous_directory'
        source = target / 'Threads.md'
        current = vault
        for part in source.relative_to(vault).parts:
            current = current / part
            if current.is_symlink():
                return None, 'unsafe_source'
        if not source.resolve().is_relative_to(vault):
            return None, 'unsafe_source'
        return source, None
    except (OSError, ValueError, RuntimeError):
        return None, 'unavailable_source'


def _thread_topics(text):
    """Yield ### topics in active containers, excluding fenced examples/history."""
    from beyin_v3_compact import FENCE, _frontmatter_end
    lines = text.splitlines()
    active, fence, title, body = True, None, None, []
    for line in lines[_frontmatter_end(lines):]:
        if fence:
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*', line):
                fence = None
            continue
        opened = FENCE.match(line)
        if opened and not (opened[1][0] == '`' and '`' in line[opened.end():]):
            fence = opened[1]
            continue
        heading = re.match(r'^(#{1,6})[ \t]+(\S.*?)[ \t]*$', line)
        if heading and len(heading[1]) <= 3:
            if title is not None:
                yield title, '\n'.join(body)
            title, body = None, []
            level, label = len(heading[1]), heading[2]
            if level < 3:
                word = fold(label).split()[0]
                active = (word in ('active', 'aktif', 'acik', 'open') if level == 2
                          else not (word == 'closed' or word.startswith(('kapan', 'kapali'))))
            elif active:
                title, body = label, [label]
        elif title is not None:
            body.append(line)
    if title is not None:
        yield title, '\n'.join(body)


def scan_stale_threads(vault: Path, now: float, stale_days: int = 14, state: Path | None = None) -> dict:
    """Read-only freshness hints from newest valid ISO mentions, not verified updates.

    Dates inside fences are ignored. No mtime fallback or status inference; a future
    mention has age zero, and an undated topic remains age None.
    """
    vault = Path(vault).resolve()
    report = {'status': 'ok', 'open_count': 0, 'stale_count': 0, 'undated_count': 0,
              'stale_days_threshold': stale_days, 'date_basis': 'latest_iso_mention',
              'stale_threads': [], 'undated_threads': []}
    source, reason = _threads_source(vault, state)
    if reason:
        return dict(report, status='unavailable', reason=reason)
    try:
        if not source.exists():
            return dict(report, status='absent')
        if not _scan_allowed(source, vault, _scan_keys(vault, state)):
            return dict(report, status='unavailable', reason='excluded_or_unreadable_source')
        text = source.read_text(encoding='utf-8', errors='replace').lstrip('\ufeff')
        # The fast scanner reads 4096 chars. Recheck the full normalized header so
        # BOM-prefixed or long privacy metadata cannot leak topic titles/dates.
        values = _front_values(text, ('visibility', 'trust', 'trusted'))
        if values.get('visibility') == 'private' or values.get('trust') == 'untrusted' or values.get('trusted') == 'false':
            return dict(report, status='unavailable', reason='excluded_or_unreadable_source')
    except (OSError, ValueError):
        return dict(report, status='unavailable', reason='unreadable_source')
    for title, body in _thread_topics(text):
        dates = []
        for match in re.finditer(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', body):
            value = match[0]
            timestamp = _parse_timestamp(value)
            if timestamp is not None:
                dates.append((value, timestamp))
        latest, stamp = max(dates) if dates else (None, None)
        age = max(0.0, (now - stamp) / 86400.0) if stamp is not None else None
        row = {'path': source.relative_to(vault).as_posix(), 'title': title,
               'last_date': latest, 'age_days': round(age, 1) if age is not None else None}
        report['open_count'] += 1
        if age is None:
            report['undated_threads'].append(row)
        elif age >= stale_days:
            report['stale_threads'].append(row)
    report['stale_threads'].sort(key=lambda row: row['age_days'], reverse=True)
    report['stale_count'] = len(report['stale_threads'])
    report['undated_count'] = len(report['undated_threads'])
    return report


def scan_knowledge_digest(vault: Path, now: float, digest_days: int = 7, state: Path | None = None) -> dict:
    vault = Path(vault).resolve()
    excluded = _scan_keys(vault, state)
    recent_items = []
    seen_dirs = set()
    knowledge_folders = []
    seen_files = set()

    def is_knowledge_dir(name: str, folded: str) -> bool:
        return 'knowledge' in folded or 'bilgi' in folded or 'decision' in folded or 'karar' in folded

    for folder in _find_top_matching_dirs(
        vault, is_knowledge_dir,
        extra_exact=('knowledge', '500-knowledge', 'decisions', 'bilgiler', 'kararlar')
    ):
        try:
            res = folder.resolve()
            key = _path_key(res)
            if key not in seen_dirs:
                seen_dirs.add(key)
                knowledge_folders.append(res)
        except OSError:
            pass

    for starter in ('knowledge', '🧠 500-Knowledge', '500-Knowledge', 'decisions'):
        f = vault / starter
        if f.is_dir() and not f.is_symlink():
            try:
                res = f.resolve()
                key = _path_key(res)
                if key not in seen_dirs:
                    seen_dirs.add(key)
                    knowledge_folders.append(res)
            except OSError:
                pass

    for folder in knowledge_folders:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in CODE_DIRS
                       and not sensitive_excluded(d) and not (Path(root) / d).is_symlink()]
            for f in files:
                file_path = Path(root) / f
                if not _scan_allowed(file_path, vault, excluded) or file_path.suffix.lower() != '.md':
                    continue

                f_key = _path_key(file_path)
                if f_key in seen_files:
                    continue
                seen_files.add(f_key)

                age, date_source = _note_age_and_source(file_path, now)
                if age <= digest_days:
                    try:
                        rel = file_path.relative_to(vault).as_posix()
                    except ValueError:
                        rel = str(file_path)

                    title = ''
                    try:
                        with file_path.open(encoding='utf-8', errors='replace') as handle:
                            head = handle.read(2048)
                        val = _front_values(head, ('title', 'baslik'), fold_values=False)
                        title = val.get('title') or val.get('baslik') or ''
                        if not title:
                            for line in head.splitlines():
                                sline = line.strip()
                                if sline.startswith('#'):
                                    title = sline.lstrip('#').strip()
                                    break
                    except OSError:
                        pass

                    if not title:
                        title = f.replace('.md', '')

                    recent_items.append({
                        'path': rel,
                        'title': title,
                        'age_days': round(age, 1),
                        'date_source': date_source,
                    })

    outcomes_file = vault / 'outcomes.md'
    if _scan_allowed(outcomes_file, vault, excluded):
        age, date_source = _note_age_and_source(outcomes_file, now)
        if age <= digest_days:
            recent_items.append({
                'path': 'outcomes.md',
                'title': 'Kazanimlar ve Sonuclar',
                'age_days': round(age, 1),
                'date_source': date_source,
            })

    recent_items.sort(key=lambda x: x['age_days'])
    mtime_count = sum(1 for it in recent_items if it.get('date_source') == 'mtime')
    return {
        'digest_days': digest_days,
        'count': len(recent_items),
        'mtime_count': mtime_count,
        'items': recent_items,
    }


def scan_companion(vault: Path, state: Path | None = None, now: float | None = None) -> dict:
    """Scan companion folder: rule count, active threads, and latest session stamp.

    Active threads are counted from '### ' headings under ## Active Threads section,
    matching both the template structure and companion-compact conventions.
    """
    vault = Path(vault).resolve()
    info = {'rules_count': 0, 'active_threads': 0, 'last_session': None, 'active_presence': 0}
    comp_dir = None
    companions = companion_names(vault, state)

    for name in companions:
        c = vault / name
        if c.is_dir() and not c.is_symlink():
            comp_dir = c
            break

    if not comp_dir:
        for d in ('🔮 850-Companion', 'Companion', '850-Companion', 'companion'):
            c = vault / d
            if c.is_dir() and not c.is_symlink():
                comp_dir = c
                break

    if comp_dir:
        rules_file = comp_dir / 'Kurallar.md'
        if _is_safe_file(rules_file):
            try:
                text = rules_file.read_text(encoding='utf-8', errors='replace')
                info['rules_count'] = sum(1 for line in text.splitlines() if re.match(r'^(?:[-*+]|\d+[.)])[ \t]', line.strip()))
            except OSError:
                pass

        threads_file = comp_dir / 'Threads.md'
        if _is_safe_file(threads_file):
            try:
                text = threads_file.read_text(encoding='utf-8', errors='replace')
                lines = text.splitlines()
                in_active = False
                has_active_section = False
                active_lines = []
                for line in lines:
                    sline = line.strip()
                    if re.match(r'(?i)^##\s+(?:active|aktif|acik|açık)', sline):
                        in_active = True
                        has_active_section = True
                        continue
                    elif in_active and re.match(r'(?i)^##\s+(?:closed|kapan|onceki|önceki|arsiv|arşiv)', sline):
                        in_active = False
                        break
                    elif in_active:
                        active_lines.append(line)

                if not has_active_section:
                    for line in lines:
                        sline = line.strip()
                        if re.match(r'(?i)^##\s+(?:closed|kapan|onceki|önceki|arsiv|arşiv)', sline):
                            break
                        active_lines.append(line)

                active_text = '\n'.join(active_lines)
                headers = re.findall(r'(?m)^###\s+', active_text)
                if headers:
                    info['active_threads'] = len(headers)
                else:
                    info['active_threads'] = len(re.findall(
                        r'(?i)\*\*(?:status|durum):\*\*[ \t]*(?:[^\w\n]*)[ \t]*(?:active|aktif)',
                        active_text or text
                    ))
            except OSError:
                pass

        session_file = comp_dir / 'Last-Session.md'
        if _is_safe_file(session_file):
            try:
                text = session_file.read_text(encoding='utf-8', errors='replace')
                m = re.search(r'##\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', text)
                if m:
                    info['last_session'] = m.group(1)
            except OSError:
                pass

    # Presence check for active sessions across devices
    presence_dir = vault / '.presence'
    if presence_dir.is_dir() and not presence_dir.is_symlink():
        active = 0
        now_ts = time.time() if now is None else now
        try:
            for p_file in presence_dir.glob('*.json'):
                if not _is_safe_file(p_file):
                    continue
                try:
                    data = json.loads(p_file.read_text(encoding='utf-8'))
                    heartbeat = data.get('heartbeat') or p_file.stat().st_mtime
                    if now_ts - heartbeat < 2700:  # < 45 minutes
                        active += 1
                except Exception:
                    pass
        except OSError:
            pass
        info['active_presence'] = active

    return info


def generate_recommendations(inbox: dict, tasks: dict, digest: dict, companion: dict, vault: Path | None = None, has_yakala: bool | None = None, threads: dict | None = None) -> list[str]:
    recs = []
    if inbox.get('count', 0) > 0:
        yakala_ready = has_yakala if has_yakala is not None else inbox.get('has_yakala', False)
        if not yakala_ready and vault is not None:
            try:
                v = Path(vault)
                yakala_ready = any((v / p / 'SKILL.md').is_file() for p in ('.agents/skills/beyin-yakala', '.claude/skills/beyin-yakala', '.kiro/skills/beyin-yakala'))
            except OSError:
                yakala_ready = False
        if yakala_ready:
            recs.append(
                f"Gelen kutusunda bekleyen {inbox['count']} kaynak var. Kalici bilgi notlarina donusturmek icin 'beyin-yakala' kullanin."
            )
        else:
            recs.append(
                f"Gelen kutusunda bekleyen {inbox['count']} kaynak var. Kalici bilgi notlarina donusturmek icin 'note-create' / beyin skill'inin ham notu bilgi notuna cevirme akisini kullanin."
            )
    if tasks.get('stale_count', 0) > 0:
        recs.append(
            f"{tasks['stale_count']} gorev {tasks['stale_days_threshold']} gunden uzun suredir hareketsiz (waiting/blocked dahil). Bu gorevleri tamamlayin, takibini yapin veya guncelleyin."
        )
    if tasks.get('undated_count', 0) > 0:
        recs.append(
            f"{tasks['undated_count']} gorevde frontmatter tarih bilgisi (updated_at/created_at) eksik. Senkron guvenligi icin tarih ekleyin."
        )
    if threads and threads.get('stale_count', 0):
        recs.append(f"{threads['stale_count']} acik konunun son ISO tarih anmasi eski; gercek durumu kaynagindan dogrulayin.")
    if companion.get('active_threads', 0) > 4:
        recs.append(
            f"Threads.md icinde {companion['active_threads']} aktif konu var. Tamamlananlari 'beyin.py companion-compact' ile arsivleyin."
        )
    if companion.get('rules_count', 0) > 35:
        recs.append(
            f"Kurallar.md dosyasinda {companion['rules_count']} kural birikmis. Ajanin dikkat penceresini rahatlatmak icin eski veya genel kurallari sadelestirin."
        )
    if digest.get('count', 0) == 0:
        recs.append(
            f"Son {digest['digest_days']} gunde yeni bilgi/karar notu uretilmedi. Ogrenilenleri knowledge/ altina damitin."
        )
    else:
        mtime_count = digest.get('mtime_count', 0)
        mtime_note = f" ({mtime_count} notta mtime'a dayandi)" if mtime_count > 0 else ""
        recs.append(
            f"Son {digest['digest_days']} gunde {digest['count']} bilgi notu tarih onceligine gore yeni sayildi{mtime_note}; ritim saglikli."
        )
    return recs


def triage(
    vault: Path,
    state: Path | None = None,
    stale_days: int = 14,
    digest_days: int = 7,
    now: float | None = None,
) -> dict:
    now_ts = time.time() if now is None else now
    vault = Path(vault).resolve()
    inbox_report_res = scan_inbox(vault, now_ts, state=state)
    tasks_report = scan_stale_tasks(vault, now_ts, stale_days=stale_days, state=state)
    digest_report = scan_knowledge_digest(vault, now_ts, digest_days=digest_days, state=state)
    companion_report = scan_companion(vault, state, now=now_ts)
    threads_report = scan_stale_threads(vault, now_ts, stale_days=stale_days, state=state)
    recs = generate_recommendations(inbox_report_res, tasks_report, digest_report, companion_report, vault=vault, threads=threads_report)

    return {
        'schema': 1,
        'vault': str(vault),
        'generated_at': datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        'inbox': inbox_report_res,
        'tasks': tasks_report,
        'digest': digest_report,
        'companion': companion_report,
        'threads': threads_report,
        'recommendations': recs,
    }


def _strip_terminal_controls(text: str) -> str:
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', '', text)


def format_human(report: dict) -> str:
    lines = ['=== Ikinci Beyin Sindirim ve Durum Raporu (Triage) ===\n']
    inbox = report.get('inbox', {})
    lines.append('[Gelen Kutusu / Yakalananlar]')
    lines.append(f"  Bekleyen kaynak: {inbox.get('count', 0)}")
    folders = inbox.get('folders', [])
    if folders:
        for f in folders:
            fn = f.get('folder', '')
            cnt = f.get('notes', 0)
            od = f.get('oldest_days')
            if od is not None:
                lines.append(f"  - {fn}: {cnt} not (en eski: {od} gun once)")
            else:
                lines.append(f"  - {fn}: {cnt} not")
    elif inbox.get('oldest_days') is not None:
        lines.append(f"  En eski: {inbox.get('oldest_days')} gun once")
    lines.append('')

    tasks = report.get('tasks', {})
    stale_count = tasks.get('stale_count', 0)
    undated_count = tasks.get('undated_count', 0)
    stale_thresh = tasks.get('stale_days_threshold', 14)
    lines.append(f"[Hareketsiz Gorevler] (>{stale_thresh} gun)")
    summary_parts = [f"Acik gorev: {tasks.get('open_count', 0)}", f"Hareketsiz: {stale_count}"]
    if undated_count > 0:
        summary_parts.append(f"Tarihsiz: {undated_count}")
    lines.append('  ' + ', '.join(summary_parts))

    for t in tasks.get('stale_tasks', [])[:5]:
        title = _strip_terminal_controls(t.get('title', ''))
        st = t.get('status', 'open')
        st_label = f" [{st}]" if st not in ('open', 'active') else ""
        lines.append(f"  - {t.get('path')}: {title}{st_label} ({t.get('age_days', 0)} gun once)")
    if stale_count > 5:
        lines.append(f"  ... ve {stale_count - 5} gorev daha")
    if undated_count > 0:
        lines.append(f"  (Not: {undated_count} gorevde frontmatter tarih alani yok; mtime yerine tarihsiz sayildi)")
    lines.append('')

    if 'threads' in report:
        threads = report['threads']
        lines.append('[Acik Konular / Threads]')
        lines.append(f"  Acik: {threads.get('open_count', 0)}, Hareketsiz: {threads.get('stale_count', 0)}, "
                     f"Tarihsiz: {threads.get('undated_count', 0)}")
        lines.append('  En yeni ISO tarih anmasi kullanilir; durumun dogrulandigi anlamina gelmez. mtime kullanilmaz.')
        if threads.get('status') == 'unavailable':
            lines.append('  Taranamadi: ' + _strip_terminal_controls(threads.get('reason', 'unavailable')))
        for key in ('stale_threads', 'undated_threads'):
            for item in threads.get(key, [])[:5]:
                title = _strip_terminal_controls(item.get('title', ''))
                path = _strip_terminal_controls(item.get('path', ''))
                age = f"{item['last_date']}, {item['age_days']} gun once" if item.get('last_date') else 'tarihsiz'
                lines.append(f"  - {path}: {title} ({age})")
            if len(threads.get(key, [])) > 5:
                lines.append(f"  ... ve {len(threads[key]) - 5} konu daha")
        lines.append('')

    digest = report.get('digest', {})
    digest_days = digest.get('digest_days', 7)
    digest_count = digest.get('count', 0)
    mtime_count = digest.get('mtime_count', 0)
    lines.append(f"[Bilgi ve Karar Ozeti] (son {digest_days} gun)")
    lines.append("  Tarih onceligi: created, yoksa updated, yoksa mtime; eski created varsa yeni updated sayilmaz.")
    if mtime_count > 0:
        lines.append(f"  Tarih onceligine gore yeni sayilan not: {digest_count} ({mtime_count} notta mtime'a dayanildi)")
    else:
        lines.append(f"  Tarih onceligine gore yeni sayilan not: {digest_count}")
    for k in digest.get('items', [])[:5]:
        title = _strip_terminal_controls(k.get('title', ''))
        lines.append(f"  - {k.get('path')}: {title}")
    if digest_count > 5:
        lines.append(f"  ... ve {digest_count - 5} not daha")
    lines.append('')

    comp = report.get('companion', {})
    lines.append('[Eslikci ve Akis]')
    lines.append(f"  Kurallar: {comp.get('rules_count', 0)} kural")
    lines.append(f"  Acik is parcaciklari: {comp.get('active_threads', 0)} thread")
    if comp.get('last_session'):
        lines.append(f"  Son oturum: {comp.get('last_session')}")
    if comp.get('active_presence', 0) > 0:
        lines.append(f"  Aktif diger cihaz oturumlari: {comp.get('active_presence')}")
    lines.append('')

    recs = report.get('recommendations', [])
    if recs:
        lines.append('[Sindirim Onerileri]')
        for i, r in enumerate(recs, 1):
            lines.append(f"  {i}. {r}")

    return '\n'.join(lines)
