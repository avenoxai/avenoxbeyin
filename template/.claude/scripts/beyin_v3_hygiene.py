"""Local hygiene mechanisms ported from MrMerkus/MMS into the V3 shape.

Reads files and directories only: no model call, no network. Writes are limited
to the hook path the user opted into (soru cooldown markers and the touch log)
and to the settings file the CLI saves, all under the runtime state, never
inside the vault. The word cap reports; it never splits or moves a file. The
done-task sweep reports; V3 stores record identity in the runtime database by
source path, so moving a source out from under a record would orphan its
history, which is why there is no automatic move here (MMS kapanis.sh moves
because its vault has no index). Human lines are ASCII Turkish, matching the
other modules.

Opt-in per the #130 design decision: every hook signal is silent unless the
user turns it on with `beyin.py preferences --word-cap-warning on` (and
--folder-questions, --promotion, --max-words). The choice lives in
`<state>/hygiene.json`, machine-local like companion-limits.json and
project-context.json, never in `.beyin-preferences.json`: an older release
rejects unknown preference keys, so a rollback would break its doctor and
hooks. Performance profiles do not touch it. The doctor reads, never writes.

Unicode care: folder and file names in a real vault are Turkish ("Arşiv",
"Şifre", "MÜŞTERİLER"), may carry an emoji or number prefix, and can arrive
NFD-encoded from macOS or iCloud. Names are folded (NFC, Turkish İ/ı, ASCII)
before any word match so a rule cannot silently stop applying on a real
folder name.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata

# Companion memory files grow by design and are compacted by the
# companion-compact protocol, so they are fully exempt from every scan here
# (#130 decision: tam muafiyet). A personalized companion directory name comes
# from the runtime bootstrap marker; this default matches the template.
DEFAULT_COMPANION = '🔮 850-Companion'

SETTINGS_FILE = 'hygiene.json'
SETTINGS_DEFAULTS = {'word_cap_warning': False, 'max_words': 500, 'folder_questions': False, 'promotion': False}
MAX_WORDS_RANGE = (10, 100000)
# The inbox report keeps its own file: a release before it rejects unknown hygiene.json keys and
# would read every hygiene signal as off after a rollback (same reason as project-context.json).
INBOX_SETTINGS_FILE = 'inbox-report.json'
INBOX_DEFAULTS = {'enabled': False, 'max_items': 10, 'max_days': 7, 'folders': []}
INBOX_RANGES = {'max_items': (1, 100000), 'max_days': (1, 3650)}
INBOX_MAX_FOLDERS = 20
# Without a folder list, a top-level folder is an inbox by generic product words on its folded
# name: "📥 000-Inbox", "00_INBOX", "Gelen Kutusu", "GelenKutusu". "gelen" alone is an ordinary
# word ("Gelen Belgeler"), so only the phrase counts. Any other layout is named by the user
# (`--inbox-folder`); a personal folder name never enters the code.
INBOX_WORDS = re.compile(r'(?:inbox|inboxes|gelenkutusu|gelenkutum)')
INBOX_FILE_LIMIT = 5000  # notes counted per inbox folder; the report says when it stopped early

# Machine views and archives grow without being read into context, so they are
# exempt from the word cap. Mirrors the MMS muafiyet list.
CAP_EXCLUDED_DIRS = {'daily', 'knowledge', 'receipts', 'raw', 'tasks'}
ARCHIVE_WORDS = re.compile(r'(?:arsiv|archive|archives|arsivler)')
SKILL_OR_INDEX = re.compile(r'(?i)(skill\.md|index\.md|indeks\.md)$')
# Generated, template, archive or code-adjacent folders never carry residence
# notes; matched against the folded words of a top-level name, so "📋 Templates"
# and "📦 900-Archive" are skipped too. Generic product names only: a personal
# vault layout never enters the code (#130 decision); kasa-class names are
# covered by sensitive_excluded().
SORU_SKIP_DIRS = re.compile(r'(?:daily|knowledge|receipts|tasks|notes|nodes|node_modules|raw|tmp|out|output|bin|'
                            r'log|logs|scripts|tests|docs|templates?|sablon|sablonlar|attachments?|ekler|'
                            r'inbox|archives?|arsiv|arsivler)')
# Kasa-class folders: personal or financial content that must never reach the
# session context through the hygiene channels. The MMS vault answered this with
# a dedicated 🔐 kasa/ folder its loader never opens; a V3 vault has no such
# folder, so the same guarantee is enforced by name and by finding: any
# top-level folder whose name carries one of these words is skipped by every
# scan here and reported in boundary() so the owner knows the guarantee is active.
#
# Matching is word-based on a folded name, not an anchored regex over the raw
# name: real folders are "🔐 Kasa", "410-Şifreler", "MÜŞTERİLER" or arrive NFD
# from macOS/iCloud, and none of those matched `^(şifre|...)$`. Python's
# re.IGNORECASE does not fold Turkish İ/ı either, so the name is folded first.
# Archives are not kasa-class: they are quiet by design, not private, and the
# template ships "📦 900-Archive".
SENSITIVE_WORDS = re.compile(
    r'(?:kasa|sifre|parola|kimlik|kimlig|finans|finansal|musteri|vergi|fatura|maas|ozel|gizli|gizlilik)'
    r'(?:ler|lar)?(?:i|im|in|imiz|leri|lari)?'
    r'|(?:private|secret|password|credential)s?')
_TR_FOLD = str.maketrans({'ş': 's', 'ğ': 'g', 'ü': 'u', 'ö': 'o', 'ç': 'c', 'ı': 'i', 'â': 'a', 'î': 'i', 'û': 'u'})
TERMINAL_STATUSES = {'done', 'cancelled', 'kapandi'}
# Directories a walk must never descend into: they are code, not notes.
CODE_DIRS = {'node_modules', 'venv', '.venv', '__pycache__'}
WALK_LIMIT = 20000  # directories; a doctor report must stay bounded on any vault
SHALLOW_LIMIT = 2000  # direct children stat'ed per folder on the SessionStart path
DEFAULT_CAP = 500
FRONT = re.compile(r'\A---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)', re.S)
# Codex reports apply_patch edits as tool_input.command; the file names are in the patch headers.
PATCH_PATHS = re.compile(r'(?m)^\*\*\* (?:Add File|Update File|Move to): (.+?)\s*$')


def fold(name):
    """NFC, Turkish-aware casefold and ASCII-fold a name: 'MÜŞTERİLER' and NFD 'Şifre' both fold."""
    text = unicodedata.normalize('NFC', str(name)).replace('İ', 'i').replace('I', 'i').lower()
    return unicodedata.normalize('NFC', text.replace('̇', '')).translate(_TR_FOLD)


def _name_words(name):
    return re.findall(r'[^\W\d_]+', fold(name))


def _nfc(name):
    return unicodedata.normalize('NFC', str(name))


def sensitive_excluded(name):
    """True when a top-level folder name carries a kasa-class word (any position, any case/normal form)."""
    return any(SENSITIVE_WORDS.fullmatch(word) for word in _name_words(name))


def _archive(name):
    return any(ARCHIVE_WORDS.fullmatch(word) for word in _name_words(name))


def _skip_top_folder(name, companions):
    return (name.startswith('.') or name in CODE_DIRS or _nfc(name) in companions or sensitive_excluded(name) or
            SORU_SKIP_DIRS.fullmatch(fold(name)) is not None or
            any(SORU_SKIP_DIRS.fullmatch(word) for word in _name_words(name)))


def check_settings(value):
    """Validated hygiene settings; unknown keys, wrong types and out-of-range caps are refused."""
    if not isinstance(value, dict) or set(value) - set(SETTINGS_DEFAULTS) - {'schema'} or value.get('schema', 1) != 1:
        raise ValueError('hygiene settings accept only ' + ', '.join(sorted(SETTINGS_DEFAULTS)))
    result = dict(SETTINGS_DEFAULTS, **{key: item for key, item in value.items() if key != 'schema'})
    for key in ('word_cap_warning', 'folder_questions', 'promotion'):
        if type(result[key]) is not bool:
            raise ValueError(f'hygiene {key} must be on or off')
    low, high = MAX_WORDS_RANGE
    if type(result['max_words']) is not int or not low <= result['max_words'] <= high:
        raise ValueError(f'max_words must be an integer between {low} and {high}')
    return result


def check_inbox_settings(value):
    """Validated inbox report settings; unknown keys, wrong types and out-of-range thresholds are refused."""
    if not isinstance(value, dict) or set(value) - set(INBOX_DEFAULTS) - {'schema'} or value.get('schema', 1) != 1:
        raise ValueError('inbox report settings accept only ' + ', '.join(sorted(INBOX_DEFAULTS)))
    result = dict(INBOX_DEFAULTS, **{key: item for key, item in value.items() if key != 'schema'})
    if type(result['enabled']) is not bool:
        raise ValueError('inbox report must be on or off')
    folders = result['folders']
    if (not isinstance(folders, list) or len(folders) > INBOX_MAX_FOLDERS or
            any(not isinstance(name, str) for name in folders)):
        raise ValueError(f'inbox folders must be a list of at most {INBOX_MAX_FOLDERS} top-level folder names')
    for name in folders:
        if (not name.strip() or name != name.strip() or len(name) > 255 or '/' in name or '\\' in name or
                name in ('.', '..') or name.startswith('.') or sensitive_excluded(name)):
            raise ValueError('inbox folder must be one visible top-level folder name, not a kasa-class folder: ' +
                             repr(name))
    if len({fold(name) for name in folders}) != len(folders):
        raise ValueError('inbox folders repeat a name')
    result['folders'] = list(folders)
    for key, (low, high) in INBOX_RANGES.items():
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f'inbox {key} must be an integer between {low} and {high}')
    return result


def read_inbox_settings(state):
    """(settings, valid), like read_settings: missing is off, damaged reads as off and is reported."""
    path = Path(state) / INBOX_SETTINGS_FILE
    if not path.exists() and not path.is_symlink():
        return dict(INBOX_DEFAULTS), True
    try:
        if path.is_symlink():
            raise ValueError('symlink')
        return check_inbox_settings(json.loads(path.read_text(encoding='utf-8'))), True
    except (ValueError, OSError):
        return dict(INBOX_DEFAULTS), False


def save_inbox_settings(state, changes):
    current, valid = read_inbox_settings(state)
    if not valid:
        raise ValueError(INBOX_SETTINGS_FILE + ' in the runtime state is invalid; fix or remove it first')
    result = check_inbox_settings(dict(current, **changes))
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.inbox-', dir=state)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(dict(schema=1, **result), out, ensure_ascii=False, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, state / INBOX_SETTINGS_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return result


def read_settings(state):
    """(settings, valid). Machine-local beside the runtime state, never in .beyin-preferences.json.

    A missing file is the default (everything off). A damaged file also reads as
    everything off, is reported invalid and is never silently rewritten.
    """
    path = Path(state) / SETTINGS_FILE
    if not path.exists() and not path.is_symlink():
        return dict(SETTINGS_DEFAULTS), True
    try:
        if path.is_symlink():
            raise ValueError('symlink')
        return check_settings(json.loads(path.read_text(encoding='utf-8'))), True
    except (ValueError, OSError):
        return dict(SETTINGS_DEFAULTS), False


def save_settings(state, changes):
    current, valid = read_settings(state)
    if not valid:
        raise ValueError(SETTINGS_FILE + ' in the runtime state is invalid; fix or remove it first')
    result = check_settings(dict(current, **changes))
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.hygiene-', dir=state)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(dict(schema=1, **result), out, ensure_ascii=False, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, state / SETTINGS_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return result


def companion_names(vault, state=None):
    """Top-level folder names (NFC) that hold companion memory and stay fully exempt.

    The runtime companion-bootstrap marker names the directory. Without a marker
    (no state, or bootstrap not run yet) the template default and the same
    name rule the companion bootstrap uses (a top-level name ending in
    'companion' or 'echo') keep a personalized folder exempt anyway.
    """
    names = {DEFAULT_COMPANION}
    try:
        marker = Path(state) / 'companion-bootstrap.json' if state else None
        if marker and marker.is_file():
            directory = json.loads(marker.read_text(encoding='utf-8')).get('directory')
            if isinstance(directory, str) and directory.strip():
                return {_nfc(directory.strip().replace('\\', '/').strip('/'))}
        for entry in Path(vault).iterdir():
            if entry.is_dir() and entry.name.casefold().endswith(('companion', 'echo')):
                names.add(entry.name)
    except (OSError, ValueError, AttributeError):
        pass
    return {_nfc(name) for name in names}


def _words(text):
    """Whitespace word count without the frontmatter block."""
    return len(FRONT.sub('', text, count=1).split())


def _front_header(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return None, []
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == '---'), None)
    if end is None:
        return None, []
    return '\n'.join(lines[1:end]).strip(), lines[1:end]


def _front_values(text, keys):
    """Folded frontmatter string values for keys, from JSON (what V3 writes) or flat YAML."""
    header, lines = _front_header(text)
    if header is None:
        return {}
    if header.startswith('{'):
        try:
            data = json.loads(header)
        except ValueError:
            return {}
        return {key: fold(data[key]).strip() for key in keys if isinstance(data, dict) and isinstance(data.get(key), str)}
    found = {}
    for line in lines:
        match = re.fullmatch(r'([^\W\d][\w-]*):[ \t]*(["\']?)([^"\'#]*?)\2[ \t]*(?:#.*)?', line.rstrip())
        if match and match[1] in keys and match[1] not in found:
            found[match[1]] = fold(match[3]).strip()
    return found


def _front_status(text):
    """The frontmatter status, folded, from JSON (what V3 writes) or flat YAML frontmatter; else None."""
    return _front_values(text, ('status',)).get('status')


def _archived_body(text):
    """Frontmatter marks an archive or a closed record: its body grows by design."""
    values = _front_values(text, ('type', 'durum', 'status'))
    return (values.get('type') == 'gecmis' or values.get('durum') in ('arsiv', 'archive') or
            values.get('status') in ('done', 'cancelled'))


def _excluded(relative, companions=frozenset()):
    parts = _nfc(relative).replace('\\', '/').split('/')
    return (any(part in CAP_EXCLUDED_DIRS or part in companions or _archive(part) or
                SKILL_OR_INDEX.search(part) or part in CODE_DIRS or part.startswith('.') for part in parts[:-1]) or
            sensitive_excluded(parts[0]) and len(parts) > 1 or
            parts[-1] in companions or SKILL_OR_INDEX.search(parts[-1]) or not parts[-1].endswith('.md'))


def _shallow_newest(folder, cutoff=None):
    """Newest mtime among the folder itself and its direct children; 0 when unreadable.

    Deliberately shallow (#130 decision): no rglob on the SessionStart path. A
    deep tree that only gained a nested file keeps the folder quiet here; the
    cooldown marker bounds how often that can turn into a question. Stops as
    soon as one entry is newer than the cutoff and never stats more than
    SHALLOW_LIMIT children, so a huge attachment folder cannot stall a hook.
    """
    try:
        newest = folder.stat().st_mtime
        if cutoff is not None and newest > cutoff:
            return newest
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index >= SHALLOW_LIMIT:
                    break
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                if cutoff is not None and newest > cutoff:
                    break
    except OSError:
        return 0
    return newest


def edited_paths(payload):
    """Vault candidates from one PostToolUse payload: Claude file_path/path, Codex apply_patch headers.

    Codex reports apply_patch as tool_input.command with the patch text; its file
    headers are relative to the session cwd. At most five paths, in order.
    """
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict):
        return []
    found = [value for value in (tool_input.get('file_path'), tool_input.get('path'))
             if isinstance(value, str) and value.strip()]
    command = tool_input.get('command')
    if isinstance(command, str) and '*** Begin Patch' in command:
        found += PATCH_PATHS.findall(command)
    cwd = payload.get('cwd')
    base = Path(cwd) if isinstance(cwd, str) and cwd.strip() and Path(cwd).is_absolute() else None
    paths = []
    for value in found:
        path = Path(value.strip())
        if not path.is_absolute():
            if base is None:
                continue
            path = base / path
        if path not in paths:
            paths.append(path)
    return paths[:5]


def _vault_relative(vault, path):
    try:
        return Path(path).resolve().relative_to(Path(vault).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def file_over_cap(vault, path, cap=DEFAULT_CAP, state=None):
    """(words, over) for one Markdown file, or None when this file is not measured.

    Symlinks are refused: a vault link has no body of its own to cap.
    """
    path = Path(path)
    if path.is_symlink() or path.suffix != '.md':
        return None
    relative = _vault_relative(vault, path)
    if relative is None or _excluded(relative, companion_names(vault, state)):
        return None
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        return None
    if _archived_body(text):
        return None  # archive bodies grow by design; the cap must not bite them
    words = _words(text)
    return words, words > cap


def cap_scan(vault, cap=DEFAULT_CAP, limit=20, state=None):
    """Over-cap user notes across the vault. Informational; the doctor runs it only when opted in."""
    vault = Path(vault).resolve()
    companions = companion_names(vault, state)
    over, checked, visited, truncated = [], 0, 0, False
    for directory, folders, files in os.walk(vault):
        visited += 1
        if visited > WALK_LIMIT:
            truncated = True
            break
        top = directory == str(vault)
        if not top and ('.git' in folders or '.git' in files):
            folders[:] = []  # a nested repository is code, not notes
            continue
        folders[:] = [name for name in folders if not name.startswith('.') and name not in CAP_EXCLUDED_DIRS
                      and name not in CODE_DIRS and _nfc(name) not in companions and not _archive(name)
                      and not (top and sensitive_excluded(name))]
        for name in files:
            if SKILL_OR_INDEX.search(name) or not name.endswith('.md') or name.startswith('.'):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                continue
            relative = path.relative_to(vault).as_posix()
            if _excluded(relative, companions):
                continue
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                continue
            if _archived_body(text):
                continue
            checked += 1
            words = _words(text)
            if words > cap:
                over.append({'file': relative, 'words': words})
    over.sort(key=lambda entry: (-entry['words'], entry['file']))
    return {'cap': cap, 'checked': checked, 'over_count': len(over),
            'over': over[:limit], 'truncated': len(over) > limit or truncated}


def hook_cap_warning(vault, payload, cap=DEFAULT_CAP, harness=None, state=None):
    """PostToolUse warning lines for the just-written notes that cross the cap.

    Opt-in: the caller only invokes this when word_cap_warning is on.
    Harness gate (#130 decision): only Claude and Codex are wired; their
    PostToolUse `additionalContext` shape is tested in v3_hook_test. Other
    harnesses stay silent until their adapter behavior is proven.
    A split signal, not a split action - the wording is MMS's on purpose.
    """
    if harness is not None and harness not in ('claude', 'codex'):
        return ''
    if payload.get('hook_event_name') != 'PostToolUse':
        return ''
    lines = []
    for path in edited_paths(payload):
        measured = file_over_cap(vault, path, cap, state=state)
        if not measured or not measured[1]:
            continue
        words = measured[0]
        relative = _vault_relative(vault, path) or str(path)
        lines.append('Buyuk not: "%s" %d kelime - %d kelime tavan uzerinde. Bolum SINYALI, emir degil: '
                     'dosya tek soruyu cevapliyorsa birak; birden fazla soruyu cevapliyorsa alt dosyaya bol '
                     've notlar arasina [[wikilink]] ile bagla.\n' % (relative, words, words - cap))
    return ''.join(lines)


def folder_questions(vault, state, cooldown_days=14, limit=3):
    """Questions for top-level user folders that stayed empty or silent.

    Mirrors MMS soru-sirasi, with the #130 budget decision: only the folder's
    own mtime and its direct children are read, no rglob and no deep tree walk.
    A per-folder marker under the runtime state keeps one ask per quiet
    period; it is removed when the folder warms. Hook path only: this writes
    markers, so the doctor must never call it. Companion, kasa-class, archive,
    template and system folders are never asked about.
    """
    vault = Path(vault).resolve()
    state = Path(state).resolve()
    if state == vault or vault in state.parents:
        return []
    companions = companion_names(vault, state)
    now = time.time()
    cutoff = now - cooldown_days * 86400
    questions = []
    try:
        roots = sorted(entry.name for entry in vault.iterdir()
                       if entry.is_dir() and not entry.is_symlink() and not _skip_top_folder(entry.name, companions))
    except OSError:
        return []
    for name in roots:
        folder = vault / name
        newest = _shallow_newest(folder, cutoff)
        stamp = state / 'soruldu' / (name + '.stamp')
        if not newest or newest > cutoff:
            if newest:
                try:
                    stamp.unlink(missing_ok=True)
                except OSError:
                    pass
            continue
        if stamp.exists():
            continue  # already asked in this quiet period; silence until the folder warms
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text('', encoding='utf-8')
        except OSError:
            continue
        try:
            with os.scandir(folder) as entries:
                empty = next(entries, None) is None
        except OSError:
            empty = False
        questions.append('%s/ klasoru %d gundur %s. Kullaniciya sor: bu alanda yazmaya '
                         'deger bir not var mi? Varsa o klasor altina kaynakli not yaz; yoksa sadece soruyu '
                         'ilet, kendin bos icerik uretme.' % (name, int((now - newest) // 86400),
                                                              'bos' if empty else 'dokunulmadi'))
        if len(questions) >= limit:
            break
    return questions


def touch_log(state, vault, payload):
    """Append touched-path lines for the promotion report; bounded single file.

    Opt-in (promotion): the hook only calls this when the user turned the terfi
    report on. PostToolUse only, paths inside the vault (Claude file_path or
    Codex apply_patch headers). Companion, kasa-class, archive and machine
    folders are never logged. About 2000 lines: the oldest half is dropped
    atomically once the file grows past the bound.
    """
    if payload.get('hook_event_name') != 'PostToolUse':
        return
    vault, state = Path(vault).resolve(), Path(state).resolve()
    if state == vault or vault in state.parents:
        return
    companions = companion_names(vault, state)
    rows = []
    for path in edited_paths(payload):
        relative = _vault_relative(vault, path)
        if relative is None or '/' not in relative or _excluded(relative, companions):
            continue  # a root-level file has no folder to promote
        rows.append('%d\t%s\n' % (int(time.time()), re.sub(r'[\t\r\n]', ' ', relative)))
    if not rows:
        return
    log = state / 'touch-log.tsv'
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as out:
            out.write(''.join(rows))
        if log.stat().st_size > 2000 * 90:
            lines = log.read_text(encoding='utf-8').splitlines(True)
            temporary = log.with_name(log.name + '.' + str(os.getpid()) + '.tmp')
            temporary.write_text(''.join(lines[len(lines) // 2:]), encoding='utf-8')
            os.replace(temporary, log)
    except OSError:
        pass


def promotion(vault, state, days=30, limit=8):
    """Hot and cold top-level usage report from the touch log, MMS terfi-like.

    Reads only: hot = most appended folders, cold = user folders with no touch
    in the window (shallow mtimes, same budget and skip rules as
    folder_questions). The move decision stays with the user, exactly like MMS.
    """
    vault, state = Path(vault).resolve(), Path(state).resolve()
    companions = companion_names(vault, state)
    now = time.time()
    cutoff = now - days * 86400
    counts = {}
    log = state / 'touch-log.tsv'
    if log.is_file() and not log.is_symlink():
        try:
            for line in log.read_text(encoding='utf-8').splitlines():
                parts = line.split('\t')
                if len(parts) < 2 or not parts[0].isdigit() or '/' not in parts[1]:
                    continue
                top = parts[1].split('/')[0]
                if int(parts[0]) >= cutoff and not _skip_top_folder(top, companions):
                    counts[top] = counts.get(top, 0) + 1
        except (OSError, ValueError):
            pass
    hot = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    touched = set(counts)
    cold = []
    try:
        for entry in sorted(vault.iterdir()):
            if not entry.is_dir() or entry.is_symlink() or _skip_top_folder(entry.name, companions):
                continue
            if entry.name in touched:
                continue
            newest = _shallow_newest(entry, cutoff)
            if newest and newest < cutoff:
                cold.append({'folder': entry.name, 'days_quiet': int((now - newest) // 86400)})
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
    The kasa check reports the sensitive folders the hygiene scans already
    exclude, naming the active guarantee instead of leaving it implicit.
    """
    vault = Path(vault).resolve()
    report = {'status': 'ok', 'findings': [], 'sensitive_excluded': [], 'code_dirs': [],
              'nested_repositories': [], 'backup_artifacts': [], 'walk_truncated': False}
    top = sorted(vault.iterdir(), key=lambda entry: entry.name)
    kasa = [entry.name for entry in top
            if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith('.')
            and sensitive_excluded(entry.name)]
    if kasa:
        report['sensitive_excluded'] = kasa
        report['findings'].append('kasa_excluded: ' + ', '.join(kasa) +
                                  '; hygiene scans skip these folders, automatic context still follows'
                                  ' visibility metadata - mark their sources visibility: private for the full guarantee.')
    parent = vault.parent
    if (parent / '.obsidian').is_dir():
        report['findings'].append('parent_obsidian_index: parent directory also holds a .obsidian vault root; '
                                  'Obsidian could open the parent and treat this folder as a subfolder.')
    if not (vault / '.obsidian').is_dir():
        report['findings'].append('no_root_obsidian: vault root has no .obsidian; open this exact folder in Obsidian, '
                                  'not a parent.')
    nested, code, visited = [], [], 0
    for directory, folders, files in os.walk(vault):
        visited += 1
        relative_dir = Path(directory).relative_to(vault).as_posix()
        if visited > WALK_LIMIT:
            report['walk_truncated'] = True
            folders[:] = []
            break
        if relative_dir != '.' and ('.git' in folders or '.git' in files):
            # A .git file is a worktree or submodule link: still a second repository here.
            # Its tree belongs to that repository, so the walk stops at its root.
            nested.append(relative_dir)
            folders[:] = []
            continue
        for name in folders:
            if name in CODE_DIRS:
                code.append(name if relative_dir == '.' else relative_dir + '/' + name)
        # Never descend into hidden folders or code trees: they are the finding, and a
        # node_modules walk would cost the doctor its latency on exactly the vault it warns about.
        folders[:] = [name for name in folders if not name.startswith('.') and name not in CODE_DIRS
                      and relative_dir.count('/') < 6]
    if code:
        report['code_dirs'] = sorted(code)
        report['findings'].append('code_inside_vault: ' + ', '.join(report['code_dirs'][:5]) +
                                  '; keep the code repo outside the memory vault.')
    if nested:
        report['nested_repositories'] = sorted(nested)
        report['findings'].append('nested_git_repository: ' + ', '.join(report['nested_repositories'][:3]) +
                                  '; a subfolder is its own git repository inside the vault.')
    names = {entry.name for entry in top}
    leftovers = []
    for entry in top:
        if not entry.is_file() or entry.is_symlink():
            continue
        # A sync conflict copy ("Plan 2.md") counts only beside its original ("Plan.md");
        # a note that merely ends in a number is not a leftover.
        copy = re.fullmatch(r'(.+) \d{1,2}(\.md)', entry.name)
        if entry.name.endswith(('.bak', '.orig', '.yedek')) or (copy and copy[1] + copy[2] in names):
            leftovers.append(entry.name)
    if leftovers:
        report['backup_artifacts'] = leftovers
        report['findings'].append('backup_artifacts: ' + ', '.join(leftovers[:5]) +
                                  '; duplicate editor or sync copies may carry a private copy.')
    if report['findings']:
        report['status'] = 'attention'  # information only; doctor status is raised by sync, not here
    return report


def closed_tasks(vault, days=30, limit=20, now=None):
    """Closed work that no longer belongs at the top level - a report, not a move.

    Scans task sources under tasks/ for a done/cancelled (or kapandi) frontmatter
    status, older than the number of days by file mtime (metadata carries
    updated_at only when the writer set it, so mtime is the independent bound).
    The status is read from the frontmatter only: V3 itself writes JSON
    frontmatter (`"status": "done"`), Obsidian writes flat YAML, and a body line
    that happens to start with "status: done" is not a task status. V3 keeps
    source paths in the runtime database; a kapanis move would sever every
    receipt ref and revision history, so nothing here moves or writes a file.
    Lists the oldest first.
    """
    vault = Path(vault).resolve()
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    closed = []
    folder = vault / 'tasks'
    if not folder.is_dir() or folder.is_symlink():
        return {'closed_count': 0, 'closed': [], 'truncated': False, 'days': days}
    for directory, folders, files in os.walk(folder):
        folders[:] = sorted(name for name in folders if not name.startswith('.'))
        for name in sorted(files):
            path = Path(directory) / name
            if not name.endswith('.md') or name.startswith('.') or path.is_symlink():
                continue
            try:
                modified = path.stat().st_mtime
                if modified > cutoff:
                    continue
                text = path.read_text(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                continue
            if _front_status(text) not in TERMINAL_STATUSES:
                continue
            closed.append({'source': path.relative_to(vault).as_posix(),
                           'days_old': int((now - modified) // 86400)})
    closed.sort(key=lambda entry: (-entry['days_old'], entry['source']))
    return {'closed_count': len(closed), 'closed': closed[:limit], 'truncated': len(closed) > limit,
            'days': days}


def _inbox_folder(name, configured):
    if configured is not None:
        return fold(name) in configured
    words = _name_words(name)
    return any(INBOX_WORDS.fullmatch(word) for word in words) or any(
        pair == ('gelen', 'kutusu') or pair == ('gelen', 'kutum') for pair in zip(words, words[1:]))


def _capture_time(path):
    """When a note was captured: a valid frontmatter `created` date, else the file mtime; None for a
    processed yakala card (`tur: yakala`, `durum: islendi`), which no longer waits.

    A clone, a sync client or a restore rewrites mtimes, so a week-old capture would read as new;
    the note's own date survives that. A missing or malformed date falls back to the mtime.
    """
    modified = path.stat().st_mtime
    try:
        with path.open(encoding='utf-8', errors='replace') as handle:
            head = handle.read(4096)
    except OSError:
        return modified
    values = _front_values(head, ('created', 'tur', 'durum', 'yakalandi'))
    if values.get('tur') == 'yakala' and values.get('durum') == 'islendi':
        return None  # a capture card the agent already processed is no longer waiting
    created = values.get('created') or (values.get('yakalandi', '') if values.get('tur') == 'yakala' else '')
    match = re.match(r'(\d{4})-(\d{2})-(\d{2})', created)
    if not match:
        return modified
    try:
        return min(modified, datetime(int(match[1]), int(match[2]), int(match[3])).timestamp())
    except (OverflowError, ValueError, OSError):  # an impossible date (2026-13-40) is malformed, not normalized
        return modified


def inbox_report(vault, state=None, max_items=10, max_days=7, now=None, folders=None):
    """How much capture is waiting in top-level inbox folders - a report, not a processing run.

    `folders` (the user's list, compared NFC and Turkish-case folded) names the inbox folders;
    without it a top-level folder is an inbox by the generic words in INBOX_WORDS ("📥 000-Inbox",
    "00_INBOX", "Gelen Kutusu"). Counts the Markdown notes under it (dot folders and symlinks
    skipped) and the age of the oldest by its frontmatter `created` date, else its file mtime.
    A folder over either threshold is marked `attention`; nothing is moved, classified or handed
    to an agent. A folder that could not be read, or a named folder that is missing, is listed
    with an error, never reported as empty.
    """
    vault = Path(vault).resolve()
    now = time.time() if now is None else now
    companions = companion_names(vault, state)
    configured = {fold(name): name for name in folders} if folders else None
    rows = []
    try:
        entries = sorted(vault.iterdir())
    except OSError as exc:
        return {'folders': [], 'attention': False, 'error': type(exc).__name__,
                'max_items': max_items, 'max_days': max_days}
    found = set()
    for entry in entries:
        name = entry.name
        if (not entry.is_dir() or entry.is_symlink() or name.startswith('.') or _nfc(name) in companions or
                sensitive_excluded(name) or not _inbox_folder(name, configured)):
            continue
        found.add(fold(name))
        count, oldest, partial, error = 0, None, False, None

        def unreadable(exc):
            nonlocal error
            error = error or type(exc).__name__

        for directory, subfolders, files in os.walk(entry, onerror=unreadable):
            subfolders[:] = sorted(sub for sub in subfolders
                                   if not sub.startswith('.') and not (Path(directory) / sub).is_symlink())
            for file in sorted(files):
                path = Path(directory) / file
                if not file.lower().endswith('.md') or file.startswith('.') or path.is_symlink():
                    continue
                if count >= INBOX_FILE_LIMIT:
                    partial = True
                    break
                try:
                    modified = _capture_time(path)
                except OSError as exc:
                    unreadable(exc)
                    continue
                if modified is None:
                    continue
                count += 1
                oldest = modified if oldest is None else min(oldest, modified)
            if partial:
                break
        oldest_days = None if oldest is None else max(0, int((now - oldest) // 86400))
        row = {'folder': name, 'notes': count, 'oldest_days': oldest_days,
               'attention': count >= max_items or (oldest_days is not None and oldest_days >= max_days)}
        if partial:
            row['truncated'] = True
        if error:
            row['error'] = error
        rows.append(row)
    for key, name in (configured or {}).items():
        if key not in found:
            rows.append({'folder': name, 'notes': 0, 'oldest_days': None, 'attention': False, 'error': 'not_found'})
    return {'folders': rows, 'attention': any(row['attention'] or 'error' in row for row in rows),
            'max_items': max_items, 'max_days': max_days}
