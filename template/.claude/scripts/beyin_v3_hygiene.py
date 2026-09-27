"""Local hygiene mechanisms ported from MrMerkus/MMS into the V3 shape.

Reads files and directories only: no model call, no network. Writes are limited
to the soru cooldown markers under the runtime state, never inside the vault.
The word cap reports; it never splits or moves a file. The done-task sweep
reports; V3 stores record identity in the runtime database by source path, so
moving a source out from under a record would orphan its history — that is why
there is no automatic move here (MMS kapanis.sh moves because its vault has no
index). Human lines are ASCII Turkish, matching the other modules.
"""
import json
import os
from pathlib import Path
import re
import time

# Frontmatter of these shapes is intentionally exempt from the word cap: machine
# views and archives grow without being read into context. Mirrors the MMS
# muafiyet list; archive detection is by file path, like the MMS hook.
CAP_EXCLUDED_DIRS = {'daily', 'knowledge', 'receipts', 'raw', 'tasks'}
ARCHIVE_DIR = re.compile(r'(?i)(archive|arşiv|arsiv)')
ARCHIVE_FRONT = re.compile(r'(?mi)^(type:[ \t]*gecmis|durum:[ \t]*arşiv|status:[ \t]*(done|cancelled))')
SKILL_OR_INDEX = re.compile(r'(?i)s(skill\.md?|index\.md|indeks\.md)$')
# Generated, user-instruction or code-adjacent folders never carry residence notes.
SORU_SKIP_DIRS = re.compile(r'(?ix)^(\.).|^(daily|knowledge|receipts|tasks|notes|nodes|node_modules|'
                            r"raw|tmp|out|output|bin|log|logs|Finans|Müşteriler|gptpro)$")
DEFAULT_CAP = 500
FRONT = re.compile(r'\A---\n.*?\n---\n', re.S)
STATUS = re.compile(r'(?mi)^status:[ \t]*(done|kapandı)')


def _words(text):
    """Whitespace word count without the frontmatter block."""
    return len(FRONT.sub('', text).split())


def _excluded(relative):
    parts = relative.replace('\\', '/').split('/')
    return any(part in CAP_EXCLUDED_DIRS or ARCHIVE_DIR.search(part) or SKILL_OR_INDEX.search(part)
               for part in parts[:-1]) or SKILL_OR_INDEX.search(parts[-1]) or not parts[-1].endswith('.md')


def file_over_cap(vault, path, cap=DEFAULT_CAP):
    """(words, over) for one Markdown file, or None when this file is not measured.

    Symlinks are refused: a vault link has no body of its own to cap.
    """
    path = Path(path)
    vault = Path(vault).resolve()
    if path.is_symlink() or path.suffix != '.md':
        return None
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(vault).as_posix()
    except (ValueError, OSError):
        return None
    if _excluded(relative):
        return None
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        return None
    if ARCHIVE_FRONT.search(text):
        return None  # archive bodies grow by design; the cap must not bite them
    words = _words(text)
    return words, words > cap


def cap_scan(vault, cap=DEFAULT_CAP, limit=20):
    """Over-cap user notes across the vault. Informational, like instruction_references."""
    vault = Path(vault).resolve()
    over, checked = [], 0
    for directory, folders, files in os.walk(vault):
        folders[:] = [name for name in folders if not name.startswith('.') and name not in CAP_EXCLUDED_DIRS
                      and not ARCHIVE_DIR.search(name)]
        for name in files:
            if SKILL_OR_INDEX.search(name) or not name.endswith('.md') or name.startswith('.'):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                relative = path.relative_to(vault).as_posix()
            except ValueError:
                continue
            if _excluded(relative):
                continue
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                continue
            if ARCHIVE_FRONT.search(text):
                continue
            checked += 1
            words = _words(text)
            if words > cap:
                over.append({'file': relative, 'words': words})
    over.sort(key=lambda entry: (-entry['words'], entry['file']))
    return {'cap': cap, 'checked': checked, 'over_count': len(over),
            'over': over[:limit], 'truncated': len(over) > limit}


def hook_cap_warning(vault, payload, cap=DEFAULT_CAP):
    """One-line PostToolUse warning when the just-written note crosses the cap.

    Claude and Codex deliver the edited path in tool_input; other harnesses
    carry no path, so the cap stays a doctor scan for them. A split signal, not
    a split action — the wording is MMS's on purpose.
    """
    if payload.get('hook_event_name') != 'PostToolUse':
        return ''
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict):
        return ''
    path = tool_input.get('file_path') or tool_input.get('path')
    if not isinstance(path, str) or not path.strip():
        return ''
    measured = file_over_cap(vault, path, cap)
    if not measured or not measured[1]:
        return ''
    words = measured[0]
    try:
        relative = Path(path).resolve().relative_to(Path(vault).resolve()).as_posix()
    except (ValueError, OSError):
        relative = path
    return ('Buyuk not: "%s" %d kelime — %d kelime tavan uzerinde. Bolum SINYALI, emir degil: '
            'dosya tek soruyu cevapliyorsa birak; birden fazla soruyu cevapliyorsa alt dosyaya bol '
            've notlar arasina [[wikilink]] ile bagla.\n'
            % (relative, words, words - cap))


def folder_questions(vault, state, cooldown_days=14, limit=3):
    """Questions for top-level user folders that stayed empty or silent.

    Mirrors MMS soru-sirasi: the folder is cold when no Markdown newer than the
    cooldown lives there. A per-folder marker under the runtime state keeps one
    ask per cooldown; hostiled to no schema and removed when the folder warms.
    """
    vault = Path(vault).resolve()
    state = Path(state).resolve()
    if state == vault or vault in state.parents:
        return []
    cutoff = time.time() - cooldown_days * 86400
    questions = []
    try:
        roots = sorted(entry.name for entry in vault.iterdir()
                       if entry.is_dir() and not entry.name.startswith('.') and not entry.is_symlink()
                       and not SORU_SKIP_DIRS.match(entry.name))
    except OSError:
        return []
    for name in roots:
        folder = vault / name
        try:
            findings = [path for path in folder.rglob('*.md') if not path.is_symlink()]
            newest = max((path.stat().st_mtime for path in findings), default=0)
        except OSError:
            continue
        marker = state / 'soruldu'
        stamp = marker / (name + '.stamp')
        if newest > cutoff:
            try:
                stamp.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if stamp.exists():
            continue  # asked within the cooldown; silence until it warms or expires
        try:
            marker.mkdir(parents=True, exist_ok=True)
            stamp.write_text('', encoding='utf-8')
        except OSError:
            continue
        state_word = 'bos' if not findings else 'dokunulmayan'
        questions.append('%s/ klasoru %d gundur %s. Kullaniciya sor: bu alanda yazmaya '
                         'deger bir not var mi? Varsa o klasor altina kaynakli not yaz; yoksa sadece soruyu '
                         'ilet, kendin bos icerik uretme.' % (name, cooldown_days, state_word))
        if len(questions) >= limit:
            break
    return questions


def touch_log(state, vault, payload):
    """Append one touched-path line for the promotion report; bounded single file.

    PostToolUse only, Claude/Codex-shaped payloads with an editable path inside the
    vault. The log is append-only and shows up nowhere else; promotion() reads it.
    2000 lines keep it small: eviction drops the oldest half once, then grows again.
    """
    if payload.get('hook_event_name') != 'PostToolUse':
        return
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict):
        return
    raw = tool_input.get('file_path') or tool_input.get('path')
    if not isinstance(raw, str) or not raw.strip():
        return
    vault, state = Path(vault).resolve(), Path(state).resolve()
    if state == vault or vault in state.parents:
        return
    try:
        relative = Path(raw).resolve().relative_to(vault).as_posix()
    except (ValueError, OSError):
        return
    if _excluded(relative) or relative.endswith(('.sqlite3', '.json', '.tsv')):
        return
    log = state / 'touch-log.tsv'
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as out:
            out.write('%d\t%s\t%s\n' % (int(time.time()), relative, ''))
        if log.stat().st_size > 2000 * 90:  # ~2000 lines; drop the oldest half once
            lines = log.read_text(encoding='utf-8').splitlines(True)
            log.write_text(''.join(lines[len(lines) // 2:]), encoding='utf-8')
    except OSError:
        pass


def promotion(vault, state, days=30, limit=8):
    """Hot and cold top-level usage report from the touch log, MMS terfi-like.

    Reads only: hot = most appended paths, cold = user folders with no touch in
    the window. The move decision stays with the user, exactly like MMS.
    """
    vault, state = Path(vault).resolve(), Path(state).resolve()
    cutoff = time.time() - days * 86400
    counts = {}
    log = state / 'touch-log.tsv'
    if log.exists():
        try:
            for line in log.read_text(encoding='utf-8').splitlines():
                try:
                    at, relative = line.split('\t', 2)[:2]
                except ValueError:
                    continue
                if int(at) >= cutoff:
                    top = relative.split('/')[0]
                    counts[top] = counts.get(top, 0) + 1
        except (OSError, ValueError):
            pass
    hot = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    touched = {name for name, _ in hot}
    cold = []
    try:
        for entry in sorted(vault.iterdir()):
            if not entry.is_dir() or entry.name.startswith('.') or entry.is_symlink():
                continue
            if SORU_SKIP_DIRS.match(entry.name) or entry.name in touched:
                continue
            try:
                if not entry.rglob('*.md'):
                    continue
            except OSError:
                continue
            newest = max((path.stat().st_mtime for path in entry.rglob('*.md') if not path.is_symlink()),
                         default=0)
            if newest and newest < cutoff:
                cold.append({'folder': entry.relative_to(vault).as_posix(),
                             'days_quiet': int((time.time() - newest) // 86400)})
    except OSError:
        pass
    cold.sort(key=lambda entry: (-entry['days_quiet'], entry['folder']))
    return {'window_days': days, 'hot': [{'folder': name, 'touches': count} for name, count in hot],
            'cold': cold[:limit], 'truncated': len(cold) > limit}


def boundary(vault):
    """Repository and vault-root boundary checks; information for the doctor.

    The MMS denetci guards four invariants here: one code root inside the vault
    (node_modules / venv), one nested second repo, an Obsidian index above or
    below the root, and privacy-facing leftovers. Each check reads only paths.
    """
    vault = Path(vault).resolve()
    report = {'status': 'ok', 'findings': []}
    parent = vault.parent
    if (parent / '.obsidian').is_dir():
        report['findings'].append('parent_obsidian_index: parent directory also holds a .obsidian vault root; '
                                  'Obsidian could open the parent and treat this folder as a subfolder.')
    if not (vault / '.obsidian').is_dir():
        report['findings'].append('no_root_obsidian: vault root has no .obsidian; open this exact folder in Obsidian, '
                                  'not a parent.')
    code = [entry.name for entry in vault.iterdir()
            if entry.is_dir() and not entry.is_symlink() and entry.name in ('node_modules', 'venv', '.venv')]
    if code:
        report['findings'].append('code_inside_vault: ' + ', '.join(sorted(code)) +
                                  '; keep the code repo outside the memory vault.')
    nested = []
    for directory, folders, files in os.walk(vault):
        depth = 1 + directory[len(str(vault)):].count(os.sep)
        if depth > 6:
            folders[:] = []
            continue
        folders[:] = [name for name in folders if not name.startswith('.') or name == ('.git',)[0]]
        if '.git' in folders and directory != str(vault):
            relative = Path(directory).relative_to(vault).as_posix()
            nested.append(relative)
            folders.remove('.git')
    if nested:
        report['findings'].append('nested_git_repository: ' + ', '.join(nested[:3]) +
                                  '; a subfolder is its own git repository inside the vault.')
    leftovers = [entry.name for entry in vault.iterdir()
                 if entry.is_file() and not entry.is_symlink() and (
                     entry.name.endswith(('.bak', '.orig', '.yedek')) or
                     re.match(r'^\d\.md', entry.name))]
    if leftovers:
        report['findings'].append('backup_artifacts: ' + ', '.join(sorted(leftovers)[:5]) +
                                  '; duplicate editor or sync copies may carry a private copy.')
    if report['findings']:
        report['status'] = 'attention'  # information only; doctor status is raised by sync, not here
    return report


def closed_tasks(vault, days=30, limit=20, now=None):
    """Closed work that no longer belongs at the top level — a report, not a move.

    Scans tasks/*.md frontmatter for status done/kapandı, older than the number
    of days by file mtime (metadata carries updated_at only when the writer set
    it, so mtime is the independent bound). V3 keeps source paths in the runtime
    database; a kapanis move would sever every receipt ref and revision history,
    so the decision stays with the user. Lists the oldest first.
    """
    vault = Path(vault).resolve()
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    closed = []
    folder = vault / 'tasks'
    if not folder.is_dir():
        return {'closed_count': 0, 'closed': [], 'truncated': False}
    for path in sorted(folder.glob('*.md')):
        if path.is_symlink():
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except (OSError, ValueError):
            continue
        if not STATUS.search(text):
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified > cutoff:
            continue
        closed.append({'source': path.relative_to(vault).as_posix(),
                       'days_old': int((now - modified) // 86400)})
    closed.sort(key=lambda entry: (-entry['days_old'], entry['source']))
    return {'closed_count': len(closed), 'closed': closed[:limit], 'truncated': len(closed) > limit,
            'days': days}
