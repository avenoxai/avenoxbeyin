#!/usr/bin/env python3
"""Explicitly scoped global lifecycle bridge with opt-in global hook management."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
EVENTS = ('SessionStart', 'Stop', 'PreCompact', 'SessionEnd')
# Antigravity has no SessionEnd/PreCompact; its first invocation and idle Stop are the boundaries.
ANTIGRAVITY = {'PreInvocation': 'SessionStart', 'Stop': 'Stop'}


def working_directory(payload, harness):
    if harness == 'antigravity':
        # Antigravity sends no cwd and runs the hook in the hooks.json folder, so the first
        # workspace is the project: a cwd field or the process directory never stands in for it.
        paths = payload.get('workspacePaths')
        value = paths[0] if isinstance(paths, list) and paths else None
    else:
        value = payload.get('cwd')
        if value is None:
            value = os.environ.get('CLAUDE_PROJECT_DIR') if harness == 'claude' else None
        if value is None:
            try:
                value = os.getcwd()
            except OSError:
                return None
    if not isinstance(value, str) or not value:
        return None
    try:
        path = Path(value)
        return path.resolve(strict=True) if path.is_absolute() else None
    except (RuntimeError, OSError, ValueError):
        # Deleted process directories, invalid paths and symlink loops carry no origin.
        return None


def origin(payload, harness, vault=None):
    cwd = working_directory(payload, harness)
    if cwd is None:
        return {}
    name = None
    if vault is not None:
        from beyin_v3_projects import resolve_active_project
        name = resolve_active_project(vault, cwd)
    if not name:
        name = ''.join(c for c in cwd.name if c.isalnum() or c in ' ._-')[:80] or 'project'
    return dict(project=name, project_id=hashlib.sha256(str(cwd).encode()).hexdigest()[:24])


def read_project_context(state):
    """Machine-local opt-in (default off); outside the vault so rollback never sees a new preference key."""
    path = Path(state) / 'project-context.json'
    if not path.is_file() or path.is_symlink():
        return False
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return False
    return isinstance(data, dict) and data.get('enabled') is True


def save_project_context(state, enabled):
    """Save machine-local project-context setting atomically."""
    path = Path(state) / 'project-context.json'
    from beyin_v3_sync import atomic
    atomic(path, json.dumps({'schema': 1, 'enabled': bool(enabled)}))
    return bool(enabled)


def _visible(record):
    """The default internal audience gate shared with retrieval (_eligible)."""
    return (record.get('visibility', 'internal') in ('public', 'internal') and record.get('trust') != 'untrusted' and
            record.get('trusted') is not False and record.get('status') != 'untrusted' and record.get('kind') != 'untrusted')


def _fresh(vault, record):
    """Indexed source still exists inside the vault and matches the indexed hash."""
    from beyin_v3_hygiene import sensitive_source
    if sensitive_source(record.get('source')):
        return False
    try:
        target = (vault / record['source']).resolve()
        return (target.is_relative_to(vault) and target.is_file() and
                hashlib.sha256(target.read_bytes()).hexdigest() == record.get('source_sha256'))
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _one_line(value, limit):
    text = ' '.join(str(value or '').split())
    if len(text) <= limit:
        return text
    if limit < 20:
        return ''
    cut = text[:limit - 3]
    return (cut.rsplit(' ', 1)[0] if ' ' in cut else cut) + '...'


def project_context(vault, state, project_id, project_name, budget=1200, today_iso=None):
    """Scoped, read-only project block for an opted-in external SessionStart.

    - Latest receipt of this project_id (receipt_checkpoints session match) by created_at;
      skipped when its source file is gone or it or any ref is private/untrusted. The gate is
      beyin_v3_projections.latest_receipts, shared with the vault SessionStart hook.
    - Due tasks (due_at <= local today, active/waiting) of this project, after the retrieval
      visibility/trust, supersession and source-freshness gates; sorted by due date, title.
    - Other due tasks: count only, never titles.
    - Whole lines only, within budget; no model call, no write.
    """
    if budget < 80 or not project_id:
        return ''
    import sqlite3
    from datetime import date
    from beyin_v3_projections import latest_receipts
    vault = Path(vault).resolve()
    today = str(today_iso or date.today().isoformat())[:10]
    database = Path(state) / 'memory.sqlite3'
    if database.is_symlink() or not database.is_file():
        return ''
    # Short read-only timeout: a busy worker must not push SessionStart past the host limit.
    db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        db.execute('PRAGMA query_only=ON')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        summary = ''
        if {'receipt_checkpoints', 'receipts'} <= tables:
            cols = {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}
            sessions = ({row[0] for row in db.execute('SELECT session FROM receipt_checkpoints WHERE project_id=?', (project_id,))}
                        if 'project_id' in cols else set())
            found = next(latest_receipts(db, vault, sessions), None) if sessions else None
            if found:
                summary = _one_line(found[1]['summary'], 600)
        current, other_due = [], 0
        wanted = (project_name or '').strip().casefold()
        if 'records' in tables:
            records = []
            for (payload,) in db.execute('SELECT payload FROM records ORDER BY id'):
                try:
                    record = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(record, dict) and _visible(record):
                    records.append(record)
            from beyin_v3 import resolve_supersedes  # ids, paths and [[links]] alike (#201)
            superseded = resolve_supersedes(records)[0]
            for record in records:
                if record.get('kind') != 'task' or record.get('status') not in ('active', 'waiting') or record.get('id') in superseded:
                    continue
                try:
                    due = date.fromisoformat(str(record.get('due_at') or '')[:10]).isoformat()
                except ValueError:
                    continue
                if due > today or not _fresh(vault, record):
                    continue
                if wanted and str(record.get('project') or '').strip().casefold() == wanted:
                    title = _one_line(record.get('title'), 160)
                    if title:
                        current.append((due, title, _one_line(record.get('next_action'), 200)))
                else:
                    other_due += 1
    finally:
        db.close()
    if not summary and not current and not other_due:
        return ''
    header = f'Project context ({project_name}):'
    tail = f'(bu proje disinda {other_due} tarihi gelmis gorev)' if other_due else ''
    room = budget - len(header) - (len(tail) + 1 if tail else 0)
    body = []
    if summary:
        # With due tasks present, continuity gets at most half of the room so tasks are not crowded out.
        clipped = _one_line(summary, min(600, room // 2 if current else room) - len('Son kayit: ') - 1)
        if clipped:
            body.append('Son kayit: ' + clipped)
            room -= len(body[-1]) + 1
    if current:
        heading, note_room = 'Tarihi gelen gorevler:', len('(+999 tarihi gelen gorev sigmadi)') + 1
        room -= len(heading) + 1 + note_room  # heading and a possible omission note are reserved
        task_lines = []
        for due, title, action in sorted(current):
            line = f'- {title}: {action}' if action else f'- {title}'
            if len(line) + 1 <= room:
                task_lines.append(line)
                room -= len(line) + 1
        omitted = len(current) - len(task_lines)
        if task_lines:
            body.extend([heading, *task_lines])
        if omitted:
            body.append(f'(+{omitted} tarihi gelen gorev sigmadi)' if not task_lines else f'(+{omitted} gorev sigmadi)')
    if tail:
        body.append(tail)
    if not body:
        return ''
    text = '\n'.join([header, *body])
    return text if len(text) <= budget else ''


def vault_owned(path, vault):
    # Another installed vault owns its own local events too.
    return path.is_relative_to(vault) or any((p / '.beyin-runtime.json').exists() for p in (path, *path.parents))


def eligible(cwd, vault, roots):
    if cwd is None or not cwd.is_dir() or vault_owned(cwd, vault):
        return False
    return any(cwd.is_relative_to(root) for root in roots)


def shell_command(argv):
    if any(any(c in str(value) for c in '\r\n\x00') for value in argv):
        raise ValueError('Unsupported command path')
    if os.name != 'nt':
        return shlex.join(map(str, argv))
    script = '& ' + ' '.join("'" + str(v).replace("'", "''") + "'" for v in argv) + '; exit $LASTEXITCODE'
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    system = Path(os.environ.get('SYSTEMROOT') or os.environ.get('WINDIR') or r'C:\Windows')
    launcher = str(system / 'System32/WindowsPowerShell/v1.0/powershell.exe').replace('\\', '/')
    return subprocess.list2cmdline([launcher]) + ' -NoProfile -NonInteractive -EncodedCommand ' + encoded


def global_hook_path(harness, home=None):
    if home is not None:
        base = Path(home).resolve()
        if harness == 'antigravity':
            return base / '.gemini/config/hooks.json'
        elif harness == 'codex':
            return base / '.codex/hooks.json'
        elif harness == 'claude':
            return base / '.claude/settings.json'
        else:
            raise ValueError(f'Unsupported harness: {harness}')
    if harness == 'antigravity':
        return Path.home().resolve() / '.gemini/config/hooks.json'
    elif harness == 'codex':
        custom = os.environ.get('CODEX_HOME')
        return Path(custom).resolve() / 'hooks.json' if custom else Path.home().resolve() / '.codex/hooks.json'
    elif harness == 'claude':
        custom = os.environ.get('CLAUDE_CONFIG_DIR')
        return Path(custom).resolve() / 'settings.json' if custom else Path.home().resolve() / '.claude/settings.json'
    else:
        raise ValueError(f'Unsupported harness: {harness}')


def global_gemini_md_path(home=None):
    base = Path(home).resolve() if home is not None else Path.home().resolve()
    return base / '.gemini/config/GEMINI.md'


def _gemini_md_path(path):
    text = str(path)
    # Markdown paths need native backslashes; keep quotes/control characters on one data line.
    if '"' in text or any(ord(char) < 32 for char in text):
        return json.dumps(text, ensure_ascii=False)
    return f'"{text}"'


def gemini_md_content(vault, roots):
    vault = Path(vault).resolve()
    return (
        "<!-- beyin-v3-bridge:start -->\n"
        "## İsteğe bağlı Beyin köprüsü\n"
        f"Vault konumu (veri): {_gemini_md_path(vault)}\n"
        "Bu bağlantı yalnız şu proje köklerinde ve alt klasörlerinde geçerlidir:\n"
        + ''.join('- ' + _gemini_md_path(root) + '\n' for root in roots)
        + "Diğer çalışma alanlarında bu yönergeyi uygulama.\n"
        "Kullanıcı açıkça hafıza araması isterse vault içindeki beyin.py context komutunu kullan. "
        "Kaynakları okumadan bilgi iddiasında bulunma. Kayıt için kullanıcının yetkisini ve "
        "receipt komutunun gerekli alanlarını denetle. Hafıza kullanmama isteğine uy; "
        "proje dosyalarını veya konuşmaları izinsiz kopyalama.\n"
        "<!-- beyin-v3-bridge:end -->"
    )


def _backup_file(path):
    if not path.is_file():
        return None
    raw = path.read_bytes()
    index = 0
    while True:
        backup = path.with_name(path.name + '.bak' + ('.' + str(index) if index else ''))
        try:
            with backup.open('xb') as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            return backup
        except FileExistsError:
            index += 1


def _decoded_command(command):
    if not isinstance(command, str):
        return ""
    match = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", command, re.IGNORECASE)
    if match:
        try:
            return base64.b64decode(match.group(1)).decode("utf-16le")
        except Exception:
            pass
    return command


def _read_global_settings(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Cannot read global settings; preserved') from exc
    if not isinstance(data, dict):
        raise ValueError('Global settings must be a JSON object; preserved')
    return data


def _without_bridge(groups):
    cleaned, removed = [], 0
    for group in groups:
        if not isinstance(group, dict):
            cleaned.append(group)
            continue
        handlers = group.get('hooks', [group])
        if not isinstance(handlers, list):
            cleaned.append(group)
            continue
        remaining = [handler for handler in handlers if not (
            isinstance(handler, dict) and any('beyin_v3_bridge.py' in _decoded_command(handler.get(key))
                                             for key in ('command', 'commandWindows')))]
        count = len(handlers) - len(remaining)
        removed += count
        if not count:
            cleaned.append(group)
        elif remaining:
            cleaned.append(dict(group, hooks=remaining)) if 'hooks' in group else cleaned.extend(remaining)
    return cleaned, removed


def update_gemini_md(vault, roots, home=None):
    gemini_path = global_gemini_md_path(home=home)
    gemini_path.parent.mkdir(parents=True, exist_ok=True)
    block = gemini_md_content(vault, roots)
    pattern = r"(?:\r?\n)*<!-- beyin-v3-bridge:start -->.*?<!-- beyin-v3-bridge:end -->(?:\r?\n)*"
    if gemini_path.is_file():
        original = gemini_path.read_text(encoding="utf-8")
        if re.search(pattern, original, flags=re.S):
            new_content = re.sub(pattern, lambda m: "\n\n" + block + "\n\n", original, flags=re.S).strip() + "\n"
        else:
            new_content = (original.rstrip() + "\n\n" + block + "\n").lstrip()
        if original.strip() != new_content.strip():
            _backup_file(gemini_path)
            gemini_path.write_text(new_content, encoding="utf-8")
    else:
        new_content = block + "\n"
        gemini_path.write_text(new_content, encoding="utf-8")
    return gemini_path


def remove_gemini_md(home=None):
    gemini_path = global_gemini_md_path(home=home)
    if not gemini_path.is_file():
        return 0
    original = gemini_path.read_text(encoding="utf-8")
    pattern = r"(?:\r?\n)*<!-- beyin-v3-bridge:start -->.*?<!-- beyin-v3-bridge:end -->(?:\r?\n)*"
    if not re.search(pattern, original, flags=re.S):
        return 0
    _backup_file(gemini_path)
    new_content = re.sub(pattern, "\n\n", original, flags=re.S).strip()
    if new_content:
        gemini_path.write_text(new_content + "\n", encoding="utf-8")
    else:
        gemini_path.unlink()
    return 1


def install_bridge(vault, state, harness, roots, events=None, context_chars=1500, home=None):
    vault = Path(vault).expanduser().resolve()
    if not vault.is_dir() or not 0 <= context_chars <= 4000:
        raise ValueError('Invalid vault or context budget')
    state = Path(state).expanduser().resolve()
    if not state.is_absolute() or state.is_relative_to(vault):
        raise ValueError('Runtime state must be absolute and outside vault')
    roots = [Path(p).expanduser().resolve() for p in roots]
    if not roots or any(not p.is_dir() for p in roots):
        raise ValueError('Project roots must exist')
    events = list(dict.fromkeys(events or EVENTS))
    if harness == 'antigravity':
        events = [event for event in events if event in ANTIGRAVITY.values()]
        if not events:
            raise ValueError('Antigravity offers only SessionStart and Stop')
    elif harness not in ('claude', 'codex'):
        raise ValueError(f'Unsupported harness: {harness}')

    target = global_hook_path(harness, home=home)
    target.parent.mkdir(parents=True, exist_ok=True)

    bridge_script = Path(__file__).resolve()
    command = [sys.executable, str(bridge_script), '--vault', str(vault),
               '--state', str(state), '--harness', harness, '--context-chars', str(context_chars)]
    for root in roots:
        command.extend(['--project-root', str(root)])
    for event in events:
        command.extend(['--event', event])

    data = _read_global_settings(target)
    if harness != 'antigravity':
        hooks = data.get('hooks', {})
        if not isinstance(hooks, dict) or any(not isinstance(groups, list) for groups in hooks.values()):
            raise ValueError('Invalid global hooks configuration; preserved')

    if harness == 'antigravity':
        data['beyin-v3-bridge'] = {
            native: [{
                'type': 'command',
                'command': shell_command(command + ['--native-event', native]),
                'timeout': 20 if os.name == 'nt' else 5
            }]
            for native, ev in ANTIGRAVITY.items() if ev in events
        }
    else:
        shell = shell_command(command)
        hooks_map = data.setdefault('hooks', {})
        for event in events:
            groups = hooks_map.setdefault(event, [])
            cleaned_groups, _ = _without_bridge(groups)
            timeout = 3 if event == 'SessionEnd' else (20 if os.name == 'nt' else 5)
            handler = {'type': 'command', 'command': shell, 'timeout': timeout}
            if harness == 'codex' and os.name == 'nt':
                handler['commandWindows'] = shell
            cleaned_groups.append({'hooks': [handler]})
            hooks_map[event] = cleaned_groups

    new_bytes = (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    if not target.is_file() or target.read_bytes() != new_bytes:
        _backup_file(target)
        target.write_bytes(new_bytes)

    gemini_path = None
    if harness == 'antigravity':
        gemini_path = update_gemini_md(vault, roots, home=home)

    return {
        'status': 'installed',
        'harness': harness,
        'hook_file': str(target),
        'gemini_md': str(gemini_path) if gemini_path else None,
        'events': events,
        'project_roots': [str(r) for r in roots],
    }


def uninstall_bridge(harness, home=None):
    if harness not in ('claude', 'codex', 'antigravity'):
        raise ValueError(f'Unsupported harness: {harness}')
    target = global_hook_path(harness, home=home)
    removed_count = 0
    if target.is_file():
        data = _read_global_settings(target)
        if isinstance(data, dict):
            modified = False
            if harness == 'antigravity':
                if 'beyin-v3-bridge' in data:
                    del data['beyin-v3-bridge']
                    removed_count += 1
                    modified = True
            else:
                hooks_map = data.get('hooks')
                if isinstance(hooks_map, dict):
                    for event in list(hooks_map.keys()):
                        groups = hooks_map[event]
                        if not isinstance(groups, list):
                            continue
                        cleaned_groups, removed = _without_bridge(groups)
                        if not removed:
                            continue
                        removed_count += removed
                        modified = True
                        if cleaned_groups:
                            hooks_map[event] = cleaned_groups
                        else:
                            del hooks_map[event]
                    if modified and not hooks_map:
                        del data['hooks']
            if modified:
                _backup_file(target)
                target.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))

    if harness == 'antigravity':
        removed_count += remove_gemini_md(home=home)

    return {
        'status': 'uninstalled',
        'harness': harness,
        'hook_file': str(target),
        'removed_count': removed_count,
    }


def main(argv=None):
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8-sig', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, help='Path to vault root')
    parser.add_argument('--state', type=Path, help='Defaults to the installed vault runtime locator')
    parser.add_argument('--harness', choices=('claude', 'codex', 'antigravity'), required=True)
    parser.add_argument('--native-event', choices=tuple(ANTIGRAVITY), help='Antigravity hook event; set by --print-config')
    parser.add_argument('--project-root', type=Path, action='append', help='Project root directory')
    parser.add_argument('--context-chars', type=int, default=1500, help='Startup instructions only; 0 disables injection')
    parser.add_argument('--event', choices=EVENTS, action='append', help='Allowed events; default all four lifecycle boundaries')
    parser.add_argument('--print-config', action='store_true', help='Print hook groups to merge into global JSON; changes nothing')
    parser.add_argument('--install', action='store_true', help='Install bridge into global hook settings')
    parser.add_argument('--uninstall', action='store_true', help='Uninstall bridge from global hook settings')
    args = parser.parse_args(argv)

    if sum([bool(args.print_config), bool(args.install), bool(args.uninstall)]) > 1:
        parser.error('--print-config, --install, and --uninstall are mutually exclusive')

    # Antigravity's Stop contract expects a decision; "stop" lets the agent finish as usual.
    quiet = '{"decision":"stop"}' if args.harness == 'antigravity' and args.native_event == 'Stop' else '{}'
    try:
        if args.uninstall:
            result = uninstall_bridge(args.harness)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if not args.vault:
            parser.error('--vault is required')
        if not args.project_root and (args.print_config or args.install):
            parser.error('--project-root is required')
        if not args.project_root:
            print(quiet); return 0

        vault = args.vault.expanduser().resolve()
        if not vault.is_dir() or not 0 <= args.context_chars <= 4000:
            raise ValueError('Invalid vault or context budget')
        state = args.state or Path(json.loads((vault / '.beyin-runtime.json').read_text(encoding='utf-8'))['state'])
        if not state.is_absolute():
            raise ValueError('Runtime state must be absolute')
        state = state.resolve()
        if state.is_relative_to(vault):
            raise ValueError('Runtime state must be outside vault')
        roots = [p.expanduser().resolve() for p in args.project_root]
        if any(not p.is_dir() for p in roots):
            raise ValueError('Project roots must exist')
        events = list(dict.fromkeys(args.event or EVENTS))
        if args.harness == 'antigravity':
            events = [event for event in events if event in ANTIGRAVITY.values()]
            if not events:
                raise ValueError('Antigravity offers only SessionStart and Stop')

        if args.install:
            result = install_bridge(vault, state, args.harness, roots, events, args.context_chars)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.print_config:
            command = [sys.executable, str(Path(__file__).resolve()), '--vault', str(vault),
                       '--state', str(state), '--harness', args.harness, '--context-chars', str(args.context_chars)]
            for root in roots:
                command.extend(['--project-root', str(root)])
            for event in events:
                command.extend(['--event', event])
            if args.harness == 'antigravity':
                # One named hook with flat handler lists, as in the vault's .agents/hooks.json.
                print(json.dumps({'beyin-v3-bridge': {native: [{
                    'type': 'command', 'command': shell_command(command + ['--native-event', native]),
                    'timeout': 20 if os.name == 'nt' else 5}] for native, event in ANTIGRAVITY.items()
                    if event in events}}, indent=2))
                return 0
            shell = shell_command(command)
            print(json.dumps({'hooks': {event: [{'hooks': [{'type': 'command', 'command': shell,
                              'timeout': 3 if event == 'SessionEnd' else 5}]}] for event in events}}, indent=2))
            return 0
        # A payload cut at the read limit is not JSON: the handler below answers '{}'.
        payload = json.loads(sys.stdin.read(1_000_000).lstrip('\ufeff').strip() or '{}')
        if not isinstance(payload, dict):
            raise ValueError('Invalid hook payload')
        if args.harness == 'antigravity':
            # Same boundaries as the vault adapter: first invocation of a run, fully idle Stop.
            first = (payload.get('invocationNum') == 0 if args.native_event == 'PreInvocation'
                     else payload.get('fullyIdle') is True)
            paths = payload.get('workspacePaths')
            # A vault among the workspaces runs its own local hooks; do not count the session twice.
            if (not first or not isinstance(paths, list) or not all(isinstance(p, str) and Path(p).is_absolute() for p in paths)
                    or any(vault_owned(Path(p).resolve(), vault) for p in paths)):
                print(quiet); return 0
            payload.update(hook_event_name=ANTIGRAVITY[args.native_event], session_id=payload.get('conversationId'))
        event = payload.get('hook_event_name')
        if event not in events or payload.get('no_memory') is True or os.environ.get('BEYIN_V3_INTERNAL') or os.environ.get('BEYIN_V3_SKIP') == '1':
            print(quiet); return 0
        cwd = working_directory(payload, args.harness)
        if not isinstance(payload.get('session_id'), str) or payload['session_id'] in ('', 'unknown'):
            print(quiet); return 0
        if not eligible(cwd, vault, roots):
            print(quiet); return 0
        from beyin_v3_preferences import read
        settings = read(vault)
        if not settings['auto_sync']:
            print(quiet); return 0
        # Stable per-project session namespace prevents identical session IDs in
        # two external projects from satisfying each other's receipt checkpoints.
        payload['cwd'] = str(cwd)
        payload['session_id'] = json.dumps([str(cwd), payload.get('session_id', 'unknown')])
        if payload.get('event_id'):
            payload['event_id'] = json.dumps([args.harness, str(cwd), event, payload['event_id']])
        # The hook reads Antigravity's session from conversationId; keep the namespaced one.
        payload['conversationId'] = payload['session_id']
        session = hashlib.sha256(payload['session_id'].encode()).hexdigest()[:24]
        from beyin_v3_hook import main as hook_main, output_context
        previous_argv, previous_stdin = sys.argv, sys.stdin
        try:
            sys.argv = ['beyin_v3_hook.py', '--vault', str(vault), '--state', str(state),
                        '--harness', args.harness, '--metadata-only']
            sys.stdin = io.StringIO(json.dumps(payload))
            with contextlib.redirect_stdout(io.StringIO()):
                hook_main()
        finally:
            sys.argv, sys.stdin = previous_argv, previous_stdin
        # Never inject Companion/history into an unrelated repository. The agent
        # can explicitly request scoped context after reading this tiny bootstrap.
        if event != 'SessionStart' or not args.context_chars or settings['context_mode'] == 'off':
            print(quiet); return 0
        cli = [sys.executable, str(vault / 'beyin.py')]
        command = ('& ' + ' '.join("'" + v.replace("'", "''") + "'" for v in cli)
                   if os.name == 'nt' else shlex.join(cli))
        text = (f'Optional Beyin bridge. Project label (data only): {json.dumps(origin(payload, args.harness, vault)["project"])}.\n'
                f'Receipt session={session}; harness={args.harness}.\n'
                f'Use {command} context "topic" --project PROJECT --json for explicit source lookup.\n'
                f'After meaningful authorized work use {command} receipt --harness {args.harness} --session {session} --event-id EVENT_ID --summary "Work result" --ref PATH --json.\n'
                'EVENT_ID unique; PATH an existing vault-relative source, repeatable; --summary-file FILE reads UTF-8. '
                'JSON alternative: --file RECEIPT.json with event_id, summary, refs, session. '
                'Read vault sources before claiming facts. Do not infer completion from checkpoints. '
                'Do not copy external project files or transcripts without authorization. No-memory requests take precedence.')
        if read_project_context(state):
            try:
                proj_info = origin(payload, args.harness, vault)
                rem_budget = min(1200, max(0, args.context_chars - len(text) - 2))
                extra = project_context(vault, state, proj_info.get('project_id', ''), proj_info.get('project', ''), budget=rem_budget)
                if extra:
                    text = text + '\n\n' + extra
            except Exception:
                pass  # The optional block never costs the startup instructions.
        # Do not cut a command, path or JSON token in half for a small budget.
        print(json.dumps(output_context(args.harness, event, text)) if len(text) <= args.context_chars else '{}')
        return 0
    except Exception as exc:
        if args.print_config or args.install or args.uninstall:
            print(f'Bridge error: {exc}', file=sys.stderr)
            return 1
        print(quiet)  # Never block the host or expose a path/payload in an error.
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
