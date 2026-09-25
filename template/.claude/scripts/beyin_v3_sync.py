"""Explicit, in-process Markdown synchronization. No services or model calls."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import sys
import time

# Keep adjacent installed modules importable when this file is loaded by path.
_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

from beyin_v3 import HARNESSES, MemoryStore, ReceiptConflict, RevisionConflict, _json
from beyin_v3_projections import project_receipts
from beyin_v3_preferences import read as read_preferences
from beyin_v3_secrets import redact as redact_secrets, record as record_redactions


def _hash(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.beyin-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            # Windows does not expose directory fsync through this API.
            pass
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _yaml_scalar(value):
    """Decode a bounded YAML string; plain list items are never coerced."""
    value = value.strip()
    if value.startswith('"'):
        result = json.loads(value)
        if isinstance(result, str):
            return result
    elif re.fullmatch(r"'(?:[^']|'')*'", value):
        return value[1:-1].replace("''", "'")
    elif (value and value[0] not in '|>&*!#[]{}"\'`@%'
          and not re.match(r'[-?:](?:\s|$)', value)
          and not re.search(r':(?:\s|$)|\s#', value)):
        return value
    raise ValueError('unsupported YAML metadata; use JSON frontmatter')


def _quoted_scalar(value):
    """A quoted scalar ends at its closing quote; only a comment may follow it."""
    match = re.match(r'"(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\'', value)
    if not match or not re.fullmatch(r'\s+#.*', value[match.end():]):
        raise ValueError('unsupported YAML metadata; use JSON frontmatter')
    return _yaml_scalar(match[0])


def _plain_scalar(value):
    """A plain scalar ends at an unquoted `#` that follows whitespace or opens the value."""
    scalar = re.split(r'(?:^|\s)#', value, maxsplit=1)[0].rstrip()
    if not scalar:
        raise ValueError('unsupported YAML metadata; use JSON frontmatter')
    if scalar != value.rstrip():
        # A trailing comment must not change the type: `priority: 2 # high` stays a number.
        try:
            return json.loads(scalar)
        except json.JSONDecodeError:
            pass
    return scalar


def _yaml_sequence(value):
    if not value.endswith(']'):
        raise ValueError('unsupported YAML metadata; use JSON frontmatter')
    remaining, items = value[1:-1].strip(), []
    while remaining:
        match = re.match(r'''"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^,]+''', remaining)
        if not match:
            raise ValueError('unsupported YAML metadata; use JSON frontmatter')
        item = match[0].strip()
        if item[0] not in '"\'' and any(char in item for char in '[]{}'):
            raise ValueError('unsupported YAML metadata; use JSON frontmatter')
        items.append(_yaml_scalar(item))
        remaining = remaining[match.end():].strip()
        if remaining:
            if not remaining.startswith(',') or not remaining[1:].strip():
                raise ValueError('unsupported YAML metadata; use JSON frontmatter')
            remaining = remaining[1:].strip()
    return items


def parse(text):
    """JSON frontmatter or deliberately bounded flat scalar/list YAML."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        raise ValueError('unterminated frontmatter')
    header = ''.join(lines[1:end]).strip()
    body = ''.join(lines[end + 1:])
    if header.startswith('{'):
        metadata = json.loads(header)
        if not isinstance(metadata, dict):
            raise ValueError('frontmatter must be an object')
        return metadata, body
    metadata = {}
    header_lines = header.splitlines()
    index = 0
    while index < len(header_lines):
        line = header_lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        # Python's Unicode-aware word class lets localized Obsidian property
        # names start with a letter while still rejecting digits/punctuation.
        match = re.fullmatch(r'([^\W\d][\w-]*):[ \t]*(.*)', line)
        if not match or match[1] in metadata:
            raise ValueError('unsupported YAML metadata; use JSON frontmatter')
        key, value = match.groups()
        if not value.strip():
            # Obsidian writes empty properties as `key:` and lists as indented `- item` blocks.
            items, indent = [], None
            while index < len(header_lines) and header_lines[index].startswith((' ', '\t')):
                item = re.fullmatch(r'( +)-[ ]+(.+)', header_lines[index])
                if not item or (indent is not None and item[1] != indent):
                    raise ValueError('unsupported YAML metadata; use JSON frontmatter')
                indent = item[1]
                items.append(_yaml_scalar(item[2]))
                index += 1
            metadata[key] = items if items else None
            continue
        if value[0] in '|>&*!':
            raise ValueError('unsupported YAML metadata; use JSON frontmatter')
        try:
            metadata[key] = json.loads(value)
        except json.JSONDecodeError:
            if value.startswith("'") and value.endswith("'"):
                metadata[key] = value[1:-1].replace("''", "'")
            elif value.startswith('['):
                metadata[key] = _yaml_sequence(value.rstrip())
            elif re.fullmatch(r'\{\{[^{}\n]*\}\}', value.rstrip()):
                # A Templater placeholder is literal text until Obsidian expands it.
                metadata[key] = value.rstrip()
            elif value[0] == '{':
                raise ValueError('unsupported YAML metadata; use JSON frontmatter')
            elif value[0] in '"\'':
                metadata[key] = _quoted_scalar(value)
            else:
                metadata[key] = _plain_scalar(value)
    return metadata, body


def render(metadata, body):
    return '---\n' + json.dumps(metadata, ensure_ascii=False, indent=2) + '\n---\n' + body


EXCLUDED_FILES = {'agents.md', 'claude.md', 'gemini.md', 'skill.md', 'hooks.md', 'config.md', 'settings.md', 'instructions.md', 'codex.md', 'setup.md', 'install.md'}
EXCLUDED_DIRS = {'node_modules', 'receipts', '__pycache__'}
COMPLETION_FIELDS = {'completion_contract', 'completion_criterion', 'evidence_refs'}


class SyncEngine:
    def projection_helpers(self):
        return _hash, atomic, render

    def __init__(self, vault_root, state_dir):
        self.store = MemoryStore(state_dir, vault_root)
        self.root = self.store.vault_root
        self.state = self.store.state_dir
        self.journal = self.state / 'markdown-journal'
        self.journal.mkdir(exist_ok=True, mode=0o700)
        with self.store._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS markdown_sources(id TEXT PRIMARY KEY, source TEXT NOT NULL)')

    @classmethod
    def reader(cls, store):
        """Read-only view over an open MemoryStore for doctor: no journal directory, no schema write."""
        engine = cls.__new__(cls)
        engine.store = store
        engine.root = store.vault_root
        engine.state = store.state_dir
        engine.journal = engine.state / 'markdown-journal'
        return engine

    def snapshot_context(self, **kwargs):
        return self.store.snapshot_context(**kwargs)

    def _protect(self, text):
        if not read_preferences(self.root)['secret_filter']:
            return text, 0
        return redact_secrets(text, self.state)

    def _protect_metadata(self, metadata):
        if not read_preferences(self.root)['secret_filter'] or not metadata:
            return metadata, 0
        total_redacted = 0
        protected = dict(metadata)

        for field in ('title', 'next_action', 'completion_criterion'):
            if field in protected and isinstance(protected[field], str):
                redacted_text, count = redact_secrets(protected[field], self.state)
                total_redacted += count
                protected[field] = redacted_text

        if 'facts' in protected and isinstance(protected['facts'], dict):
            def _walk_facts(facts_dict):
                nonlocal total_redacted
                result = {}
                for k, v in facts_dict.items():
                    if isinstance(v, str):
                        v_redacted, count = redact_secrets(v, self.state)
                        if count > 0:
                            result[k] = v_redacted
                            total_redacted += count
                        else:
                            cand_redacted, c2 = redact_secrets(f'{k}: {json.dumps(v, ensure_ascii=False)}', self.state)
                            if c2 > 0:
                                result[k] = '[REDACTED]'
                                total_redacted += c2
                            else:
                                result[k] = v
                    elif isinstance(v, dict):
                        result[k] = _walk_facts(v)
                    elif isinstance(v, list):
                        new_list = []
                        for item in v:
                            if isinstance(item, str):
                                item_redacted, count = redact_secrets(item, self.state)
                                if count > 0:
                                    new_list.append(item_redacted)
                                    total_redacted += count
                                else:
                                    cand_redacted, c2 = redact_secrets(f'{k}: {json.dumps(item, ensure_ascii=False)}', self.state)
                                    if c2 > 0:
                                        new_list.append('[REDACTED]')
                                        total_redacted += c2
                                    else:
                                        new_list.append(item)
                            elif isinstance(item, dict):
                                new_list.append(_walk_facts(item))
                            elif isinstance(item, list):
                                new_list.append(_walk_facts({k: item})[k])
                            else:
                                new_list.append(item)
                        result[k] = new_list
                    else:
                        result[k] = v
                return result

            protected['facts'] = _walk_facts(protected['facts'])

        return protected, total_redacted

    def _record_redactions(self, count):
        if count:
            record_redactions(self.state, count)

    @staticmethod
    def _source_issues(result, source, record_id=None):
        return [item for key in ('warnings', 'conflicts') for item in result.get(key, [])
                if isinstance(item, dict) and (item.get('source') == source or
                                               (record_id is not None and item.get('id') == record_id))]

    def _path(self, relative, existing=False):
        if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('relative source required')
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('source outside vault')
        cursor = path
        while cursor != self.root:
            if cursor.is_symlink():
                raise ValueError('symlink source rejected')
            cursor = cursor.parent
        if existing and not path.is_file():
            raise ValueError('source missing')
        return path

    @staticmethod
    def _source_identity(path):
        """One file under another spelling (case-insensitive volume) is one source."""
        try:
            stat = path.stat()
        except (OSError, ValueError):
            stat = None
        if stat is not None and stat.st_ino:
            return stat.st_dev, stat.st_ino
        return os.path.normcase(str(path))

    def _canonical_refs(self, metadata):
        """Store validated refs as vault-relative POSIX paths, like receipt refs."""
        refs = metadata.get('evidence_refs')
        if isinstance(refs, list):
            metadata['evidence_refs'] = [self._path(ref).relative_to(self.root).as_posix() for ref in refs]

    def _completion_issue(self, metadata, task_source=None):
        """Validate an opt-in task contract without changing its evidence sources."""
        contract = metadata.get('completion_contract')
        if contract is not None and contract != 'strict':
            return 'completion_contract must be strict when set'
        criterion = metadata.get('completion_criterion')
        if criterion is not None and (not isinstance(criterion, str) or not criterion.strip()):
            return 'completion_criterion must be nonempty text'
        refs = metadata.get('evidence_refs', [])
        if not isinstance(refs, list) or not all(isinstance(ref, str) and ref.strip() for ref in refs):
            return 'evidence_refs must be a list of source paths'
        if contract == 'strict' and not criterion:
            return 'strict task requires completion_criterion'
        if contract == 'strict' and metadata.get('status') == 'done' and not refs:
            return 'strict done task requires evidence_refs'
        task_identity = None
        if task_source is not None and refs:
            try:
                task_identity = self._source_identity(self._path(task_source))
            except ValueError:
                return 'task source is unavailable or outside vault'
        seen = set()
        for ref in refs:
            try:
                path = self._path(ref, existing=metadata.get('status') != 'cancelled')
            except ValueError:
                return 'evidence ref must be an existing vault source'
            identity = self._source_identity(path)
            if identity in seen:
                return 'evidence_refs must not repeat a source'
            seen.add(identity)
            if task_identity is not None and identity == task_identity:
                return 'evidence ref cannot be the task source itself'
        return None

    def completion_health(self):
        """Report legacy done tasks and invalid strict contracts without editing sources."""
        with self.store._connect() as db:
            records = [json.loads(row[0]) for row in db.execute('SELECT payload FROM records ORDER BY id')]
        legacy_done, strict_issues = [], []
        for record in records:
            if record.get('kind') != 'task':
                continue
            if record.get('completion_contract') is not None:
                issue = self._completion_issue(record, record.get('source'))
                if issue:
                    strict_issues.append({'id': record.get('id'), 'source': record.get('source'), 'reason': issue})
            elif record.get('status') == 'done':
                legacy_done.append({'id': record.get('id'), 'source': record.get('source')})
        return {'strict_issue_count': len(strict_issues), 'strict_issues': strict_issues[:20],
                'legacy_done_count': len(legacy_done), 'legacy_done': legacy_done[:20],
                'truncated': len(strict_issues) > 20 or len(legacy_done) > 20}

    def validity_health(self):
        """Report `validity: rejected` that has no effect because the record is not an inference or preference."""
        with self.store._connect() as db:
            records = [json.loads(row[0]) for row in db.execute('SELECT payload FROM records ORDER BY id')]
        ignored = [{'id': record.get('id'), 'source': record.get('source'), 'kind': record.get('kind')}
                   for record in records
                   if record.get('validity') == 'rejected' and record.get('kind') not in ('inference', 'preference')]
        return {'ignored_rejection_count': len(ignored), 'ignored_rejections': ignored[:20], 'truncated': len(ignored) > 20}

    def _scan(self):
        records, warnings, conflicts = {}, [], []
        duplicate = set()
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.casefold() not in EXCLUDED_DIRS and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                if name.startswith('.') or not name.lower().endswith('.md') or name.casefold() in EXCLUDED_FILES or name.casefold().startswith('setup-'):
                    continue
                path = Path(directory) / name
                relative = path.relative_to(self.root).as_posix()
                try:
                    self._path(relative, existing=True)
                    raw = path.read_bytes()
                    metadata, body = parse(raw.decode('utf-8'))
                    if metadata.get('kind') == 'task' and body.lstrip().startswith('---'):
                        raise ValueError('task has embedded frontmatter; reconcile metadata and body explicitly')
                    if metadata.get('kind') == 'receipt' or metadata.get('generated') is True:
                        continue
                    record = dict(metadata, source=relative, text=body)
                    record.setdefault('id', 'md-' + _hash(relative)[:24])
                    record.setdefault('kind', 'note')
                    record = self.store._validate(record)
                    if record['source_sha256'] != _hash(raw):
                        raise ValueError('source changed while scanning')
                    if record['id'] in records:
                        duplicate.add(record['id'])
                    records[record['id']] = record
                except (ValueError, OSError, UnicodeError) as exc:
                    warnings.append({'source': relative, 'reason': str(exc)})
        for id in sorted(duplicate):
            records.pop(id, None)
            conflicts.append({'id': id, 'reason': 'duplicate source id; all copies quarantined'})
        return records, warnings, conflicts

    def _recover(self, db):
        completed, conflicts = [], []
        for entry in sorted(self.journal.glob('*.json')):
            try:
                intent = json.loads(entry.read_text(encoding='utf-8'))
                path = self._path(intent['source'])
                actual = _hash(path.read_bytes()) if path.exists() else None
                desired = _hash(intent['content'])
                if actual == intent['old_hash']:
                    atomic(path, intent['content'])
                elif actual != desired:
                    conflicts.append({'source': intent['source'], 'reason': 'recovery conflict; manual edit preserved'})
                    continue
                if intent['kind'] == 'receipt':
                    event = intent['event']
                    for ref in event['refs']:
                        self.store._source(ref)
                    row = db.execute('SELECT payload FROM receipts WHERE id=?', (event['event_id'],)).fetchone()
                    if row:
                        previous = json.loads(row[0])
                        if previous['summary'] != event['summary'] or previous['refs'] != event['refs']:
                            raise ReceiptConflict('event id collision during recovery')
                    db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)', (event['event_id'], _json(event)))
                completed.append(entry)
            except (ValueError, OSError, KeyError) as exc:
                conflicts.append({'journal': entry.name, 'reason': str(exc)})
        return completed, conflicts

    def _receipt_event(self, relative):
        """Rebuild a receipts row from its immutable source; ValueError when the file is not a valid receipt."""
        path = self._path(relative, existing=True)
        # Bytes, not read_text: universal newlines would turn '\r' into '\n' and fake an event id collision.
        metadata, body = parse(path.read_bytes().decode('utf-8'))
        event_id, harness, refs = metadata.get('event_id'), metadata.get('harness', 'manual'), metadata.get('refs')
        if metadata.get('kind') != 'receipt' or not isinstance(event_id, str) or not event_id.strip():
            raise ValueError('missing kind receipt or event_id')
        if path.name != _hash(event_id) + '.md':
            raise ValueError('filename does not match event_id hash')
        if harness not in HARNESSES + ('manual',):
            raise ValueError(f'invalid harness: {harness}')
        # Refs were validated when the receipt was written; a later rename must not block recovery.
        if not isinstance(refs, list) or not refs or any(
                not isinstance(ref, str) or not ref or Path(ref).is_absolute() or '..' in Path(ref).parts for ref in refs):
            raise ValueError('missing or invalid refs')
        summary = body[:-1] if body.endswith('\n') else body
        if not summary.strip():
            raise ValueError('empty receipt summary')
        event = {'event_id': event_id, 'summary': summary, 'refs': [Path(ref).as_posix() for ref in refs], 'harness': harness}
        created_at, session = metadata.get('created_at'), metadata.get('session')
        if created_at is not None:
            # Projections derive daily/v3/<created_at[:10]>.md from this value.
            if not isinstance(created_at, str) or datetime.fromisoformat(created_at).date().isoformat() != created_at[:10]:
                raise ValueError('invalid receipt created_at')
            event['created_at'] = created_at
        if session is not None:
            if not isinstance(session, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', session):
                raise ValueError('invalid receipt session')
            event['session'] = session
        return event

    def _scan_receipts(self, db):
        """Re-index receipt sources missing from local state, e.g. after a state reset or on a new device."""
        try:
            receipts_dir = self._path('receipts')
            if not receipts_dir.is_dir():
                return []
            # Every new source, local or synced from another device, is a new directory entry and
            # changes this signature; a fresh state has none stored. Warm syncs stop at one stat.
            stat = receipts_dir.stat()
            signature = f'{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}'
            if db.execute("SELECT 1 FROM metadata WHERE key='receipt_scan_signature' AND value=?", (signature,)).fetchone():
                return []
            names = sorted(os.listdir(receipts_dir))
        except (ValueError, OSError) as exc:
            return [{'source': 'receipts', 'reason': str(exc)}]
        warnings = []
        known = {_hash(row[0]) + '.md' for row in db.execute('SELECT id FROM receipts')}
        for name in names:
            # Indexed receipts stay authoritative; other names were never written by receipt().
            if name in known or not re.fullmatch(r'[0-9a-f]{64}\.md', name):
                continue
            rel = 'receipts/' + name
            try:
                event = self._receipt_event(rel)
                db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)', (event['event_id'], _json(event)))
            except (ValueError, OSError, UnicodeError) as exc:
                warnings.append({'source': rel, 'reason': str(exc)})
        # Rescan while a source is invalid, and while an entry added in the same timestamp tick
        # (up to 2 s on FAT, 1 s on HFS+) could still hide behind an unchanged mtime.
        if not warnings and time.time_ns() - stat.st_mtime_ns > 2_000_000_000:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('receipt_scan_signature',?)", (signature,))
        return warnings

    def sync(self):
        # Serialize recovery, source scan and projection across local processes.
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            completed, recovery_conflicts = self._recover(db)
            receipt_warnings = self._scan_receipts(db)
            records, warnings, conflicts = self._scan()
            warnings.extend(receipt_warnings)
            conflicts.extend(recovery_conflicts)
            old_owned = {row[0] for row in db.execute('SELECT id FROM markdown_sources')}
            deleted = 0
            for id in old_owned - records.keys():
                row = db.execute('SELECT payload FROM records WHERE id=?', (id,)).fetchone()
                if row:
                    old = json.loads(row[0])
                    db.execute("INSERT INTO events(event_type,record_id,revision,record) VALUES ('delete',?,?,?)", (id, old['revision'], row[0]))
                    db.execute('DELETE FROM records WHERE id=?', (id,))
                    deleted += 1
                db.execute('DELETE FROM markdown_sources WHERE id=?', (id,))
            for id, record in records.items():
                row = db.execute('SELECT payload FROM records WHERE id=?', (id,)).fetchone()
                if row and id in old_owned:
                    previous = json.loads(row[0])
                    if previous['source_sha256'] != record['source_sha256']:
                        record['revision'] = max(record['revision'], previous['revision'] + 1)
                    else:
                        record['revision'] = max(record['revision'], previous['revision'])
                payload = _json(record)
                if row and id not in old_owned and row[0] != payload:
                    conflicts.append({'id': id, 'reason': 'id already owned by another record'})
                    continue
                if not row or row[0] != payload:
                    event_type = 'update' if row else 'ingest'
                    db.execute('INSERT OR REPLACE INTO records VALUES (?,?)', (id, payload))
                    db.execute('INSERT INTO events(event_type,record_id,revision,record) VALUES (?,?,?,?)', (event_type, id, record['revision'], payload))
                db.execute('INSERT OR REPLACE INTO markdown_sources VALUES (?,?)', (id, record['source']))
            conflicts.extend(project_receipts(self, db))
            if conflicts:
                # A receipt hidden by a directory mtime that did not move surfaces as a view
                # conflict; the next sync then rescans receipts/ in full.
                db.execute("DELETE FROM metadata WHERE key='receipt_scan_signature'")
        for entry in completed:
            entry.unlink(missing_ok=True)
        return {'status': 'conflict' if conflicts else 'degraded' if warnings else 'succeeded', 'indexed': len(records), 'deleted': deleted, 'warnings': warnings, 'conflicts': conflicts}

    def _intent(self, relative, old_hash, content, kind, event=None):
        path = self._path(relative)
        entry = self.journal / (_hash(relative) + '.json')
        if entry.exists():
            raise RevisionConflict('pending source operation; sync and resolve first')
        intent = {'source': relative, 'old_hash': old_hash, 'content': content, 'kind': kind}
        if event:
            intent['event'] = event
        atomic(entry, _json(intent))
        actual = _hash(path.read_bytes()) if path.exists() else None
        if actual != old_hash:
            raise RevisionConflict('source changed before replace')
        atomic(path, content)
        return entry

    def update_task(self, id, expected_revision, changes):
        allowed = {'title', 'status', 'project', 'visibility', 'facts', 'next_action', 'owner', 'priority', 'due_at', 'updated_at', 'supersedes', 'completion_contract', 'completion_criterion', 'evidence_refs'}
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise ValueError('unsupported task metadata changes')
        changes, redacted = self._protect_metadata(changes)
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM records WHERE id=?', (id,)).fetchone()
            if not row:
                raise KeyError(id)
            record = json.loads(row[0])
            if record.get('kind') != 'task':
                raise ValueError('explicit task source required')
            if type(expected_revision) is not int or record['revision'] != expected_revision:
                raise RevisionConflict('revision conflict')
            if record.get('completion_contract') == 'strict' and changes.get('completion_contract', 'strict') != 'strict':
                raise ValueError('strict completion_contract cannot be removed by task-update')
            path = self._path(record['source'], existing=True)
            raw = path.read_bytes()
            if _hash(raw) != record['source_sha256']:
                raise RevisionConflict('source changed; sync and reread')
            metadata, body = parse(raw.decode('utf-8'))
            if metadata.get('id') != id or type(metadata.get('revision')) is not int or metadata['revision'] > expected_revision:
                raise RevisionConflict('source revision conflict')
            before = {field: metadata.get(field) for field in ('status', 'completion_contract', 'completion_criterion')}
            metadata.update(changes)
            metadata['revision'] = expected_revision + 1
            # The criterion is judged as it stood before completion, so neither it nor the
            # opt-in can arrive with the closing update. Compared exactly, as stored.
            if (metadata.get('completion_contract') == 'strict' and metadata.get('status') == 'done'
                    and before['status'] != 'done'):
                if before['completion_contract'] != 'strict':
                    raise ValueError('completion_contract cannot be set in the update that marks a strict task done; '
                                     'opt in with a separate update before closing it')
                if metadata.get('completion_criterion') != before['completion_criterion']:
                    raise ValueError('completion_criterion cannot change in the update that marks a strict task done; '
                                     'omit it or pass the stored value, or change it in a separate update first')
            # A task that never opted in keeps unrelated updates, even with a same-named legacy field.
            if metadata.get('completion_contract') is not None or COMPLETION_FIELDS & set(changes):
                issue = self._completion_issue(metadata, record['source'])
                if issue:
                    raise ValueError(issue)
                self._canonical_refs(metadata)
            self.store._validate(dict(metadata, source=record['source'], text=body))
            intended = render(metadata, body)
            self._intent(record['source'], record['source_sha256'], intended, 'task')
        result = self.sync()
        if result['conflicts']:
            raise RevisionConflict('source projection conflict')
        with self.store._connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?', (id,)).fetchone()
        if not row:
            raise RevisionConflict('task projection missing')
        result_record = json.loads(row[0])
        if result_record['revision'] != expected_revision + 1 or result_record['source_sha256'] != _hash(intended):
            raise RevisionConflict('task source changed before projection readback')
        self._record_redactions(redacted)
        return dict(result_record, redacted=redacted, secrets_redacted=redacted)

    def note_create(self, source, text, metadata=None):
        if not isinstance(text, str) or not text.strip() or (metadata is not None and not isinstance(metadata, dict)):
            raise ValueError('note text and metadata required')
        if text.lstrip().startswith('---'):
            raise ValueError('text must be body only; put frontmatter fields in metadata')
        if not source.startswith(('notes/', 'knowledge/')) or not source.endswith('.md'):
            raise ValueError('new semantic notes belong under notes/ or knowledge/')
        metadata = dict(metadata or {})
        for field in ('project', 'kind', 'status', 'updated_at'):
            if metadata.get(field) is None:
                metadata.pop(field, None)
            elif field in metadata and not isinstance(metadata[field], str):
                raise ValueError(field + ' must be a string')
        if 'id' in metadata and (not isinstance(metadata['id'], str) or not metadata['id'].strip()):
            raise ValueError('record id must be nonempty text')
        if 'facts' in metadata and not isinstance(metadata['facts'], dict):
            raise ValueError('facts must be an object')
        if 'revision' in metadata and (type(metadata['revision']) is not int or metadata['revision'] < 1):
            raise ValueError('positive revision required')
        if metadata.get('visibility', 'internal') not in ('public', 'internal', 'private'):
            raise ValueError('invalid visibility')
        supersedes = metadata.get('supersedes', [])
        if isinstance(supersedes, str):
            supersedes = [supersedes]
        if not isinstance(supersedes, list) or not all(isinstance(value, str) for value in supersedes):
            raise ValueError('supersedes must contain record ids')
        if metadata.get('id') in supersedes:
            raise ValueError('record cannot supersede itself')
        if metadata.get('kind') == 'task':
            raise ValueError('use task-create for new tasks')
        if metadata.get('generated') or metadata.get('kind') == 'receipt':
            raise ValueError('managed note kinds require their dedicated command')
        text, redacted = self._protect(text)
        metadata, meta_redacted = self._protect_metadata(metadata)
        redacted += meta_redacted
        path = self._path(source)
        intended = render(metadata, text+'\n')
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if path.exists():
                raise ValueError('source exists; preserve and edit deliberately')
            self._intent(source, None, intended, 'note')
        result = self.sync()
        record_id = metadata.get('id', 'md-' + _hash(source)[:24])
        if self._source_issues(result, source, record_id):
            raise RevisionConflict('note requires projection review')
        with self.store._connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?', (record_id,)).fetchone()
        if not row:
            raise RevisionConflict('created note missing from projection')
        record = json.loads(row[0])
        if record.get('source') != source or record.get('source_sha256') != _hash(intended) or path.read_bytes() != intended.encode('utf-8'):
            raise RevisionConflict('created note source readback mismatch')
        self._record_redactions(redacted)
        return {'status': 'succeeded', 'source': source, 'redacted': redacted,
                'secrets_redacted': redacted, 'warnings': result['warnings'],
                'conflicts': result['conflicts'],
                'source_sync': {'status': result['status'], 'warnings': result['warnings'],
                                'conflicts': result['conflicts']}}

    def task_create(self, source, text, metadata):
        if not isinstance(source, str) or not source.startswith('tasks/') or not source.endswith('.md'):
            raise ValueError('task-create source must be a tasks/ Markdown path')
        if not isinstance(text, str) or not text.strip() or not isinstance(metadata, dict):
            raise ValueError('task body and metadata required')
        if text.lstrip().startswith('---'):
            raise ValueError('text must be body only; put frontmatter fields in metadata')
        allowed = {'id', 'title', 'kind', 'revision', 'status', 'owner', 'project', 'visibility', 'facts', 'next_action', 'priority', 'due_at', 'updated_at', 'completion_contract', 'completion_criterion', 'evidence_refs'}
        if set(metadata) - allowed:
            raise ValueError('unsupported task metadata')
        metadata = dict(metadata)
        text, redacted = self._protect(text)
        if not isinstance(metadata.get('id'), str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}', metadata['id']):
            raise ValueError('stable task id required')
        if not isinstance(metadata.get('owner'), str) or not metadata['owner'].strip():
            raise ValueError('explicit task owner required')
        if metadata.get('status') not in ('inbox', 'active', 'waiting', 'blocked', 'done', 'cancelled'):
            raise ValueError('valid explicit task status required')
        if type(metadata.get('revision', 1)) is not int or metadata.get('revision', 1) != 1:
            raise ValueError('new task revision must be 1')
        if metadata.get('kind', 'task') != 'task':
            raise ValueError('task kind required')
        if not isinstance(metadata.get('facts', {}), dict) or metadata.get('visibility', 'internal') not in ('internal', 'public', 'private'):
            raise ValueError('invalid task facts or visibility')
        for field in ('title', 'project', 'next_action', 'updated_at'):
            if field in metadata and not isinstance(metadata[field], str):
                raise ValueError('task metadata text fields must be strings')
        metadata, meta_redacted = self._protect_metadata(metadata)
        redacted += meta_redacted
        metadata.update(kind='task', revision=1)
        issue = self._completion_issue(metadata, source)
        if issue:
            raise ValueError(issue)
        self._canonical_refs(metadata)
        path = self._path(source)
        intended = render(metadata, text+'\n')
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if path.exists() or db.execute('SELECT 1 FROM records WHERE id=?', (metadata['id'],)).fetchone():
                raise ValueError('task source or id exists; use task-update')
            self._intent(source, None, intended, 'task')
        result = self.sync()
        if self._source_issues(result, source, metadata['id']):
            raise RevisionConflict('task-create projection requires review')
        with self.store._connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?', (metadata['id'],)).fetchone()
        if not row:
            raise RevisionConflict('created task missing from projection')
        record = json.loads(row[0])
        if any(record.get(key) != value for key, value in metadata.items()) or record['source_sha256'] != _hash(intended) or path.read_bytes() != intended.encode('utf-8'):
            raise RevisionConflict('created task metadata or source readback mismatch')
        self._record_redactions(redacted)
        return dict(record, redacted=redacted, secrets_redacted=redacted,
                    warnings=result['warnings'], conflicts=result['conflicts'],
                    source_sync={'status': result['status'], 'warnings': result['warnings'],
                                 'conflicts': result['conflicts']})

    def receipt(self, event_id, summary, refs, harness, session=None):
        if not isinstance(event_id, str) or not event_id.strip() or not isinstance(summary, str) or not summary.strip():
            raise ValueError('event id and summary required')
        if harness not in HARNESSES + ('manual',) or not isinstance(refs, list) or not refs:
            raise ValueError('harness and refs required')
        summary, redacted = self._protect(summary)
        refs = [self.store._source(ref) for ref in refs]
        source = 'receipts/' + _hash(event_id) + '.md'
        event = {'event_id': event_id, 'summary': summary, 'refs': refs, 'harness': harness, 'created_at': datetime.now(timezone.utc).isoformat()}
        if session is not None:
            if not isinstance(session, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', session):
                raise ValueError('invalid receipt session')
            event['session'] = session
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM receipts WHERE id=?', (event_id,)).fetchone()
            if not row and self._path(source).exists():
                # Local state was reset: adopt the on-disk receipt instead of reporting a false conflict.
                try:
                    old = self._receipt_event(source)
                except (ValueError, OSError, UnicodeError) as exc:
                    raise ReceiptConflict('receipt source manually changed') from exc
                db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)', (event_id, _json(old)))
                row = (_json(old),)
            if row:
                old = json.loads(row[0])
                if old['summary'] != summary or old['refs'] != refs:
                    raise ReceiptConflict('event id collision')
                event = old
            receipt_metadata = {'kind': 'receipt', 'event_id': event_id, 'harness': event['harness'], 'refs': refs, 'visibility': 'internal'}
            if event.get('created_at'):
                receipt_metadata['created_at'] = event['created_at']
            if event.get('session'):
                receipt_metadata['session'] = event['session']
            content = render(receipt_metadata, summary + '\n')
            path = self._path(source)
            if path.exists():
                # Byte comparison: universal-newline text would call a '\r' summary a manual change.
                if path.read_bytes() != content.encode('utf-8'):
                    raise ReceiptConflict('receipt source manually changed')
            else:
                self._intent(source, None, content, 'receipt', event)
        result = self.sync()
        if result['conflicts']:
            raise ReceiptConflict('receipt projection conflict')
        if self._path(source, existing=True).read_bytes() != content.encode('utf-8'):
            raise ReceiptConflict('receipt source changed before readback')
        self.store.submit_receipt(event_id, summary, refs, event['harness'])
        self._record_redactions(redacted)
        return {'id': event_id, 'event_id': event_id, 'status': 'succeeded', 'source': source,
                'redacted': redacted, 'secrets_redacted': redacted}
