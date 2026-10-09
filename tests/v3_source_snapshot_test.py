"""Companion selection equivalence and decode counts; synthetic local stores only."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'snapshot_subject', ROOT / 'template/.claude/scripts/beyin_v3.py')
subject = importlib.util.module_from_spec(SPEC)
_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    SPEC.loader.exec_module(subject)
finally:
    sys.dont_write_bytecode = _bytecode
_json = subject._json
_rejected_inference = subject._rejected_inference


# Frozen pre-optimization implementation: deliberately scans and decodes every row.
def original_snapshot(self, source_names, audience="internal", budget_chars=3000, source_directory=None, text_transform=None):
    """Return a bounded, source-verified continuity set in requested order."""
    if (not isinstance(source_names, (list, tuple)) or
            not all(isinstance(name, str) and name and "/" not in name and "\\" not in name
                    for name in source_names) or
            type(budget_chars) is not int or budget_chars < 0):
        raise ValueError("invalid source snapshot request")
    if audience not in ("public", "internal", "private"):
        raise ValueError("invalid audience")
    allowed = {"public"} if audience == "public" else {"public", "internal"} if audience == "internal" else {"public", "internal", "private"}
    with self._connect() as db:
        records = [json.loads(row[0]) for row in db.execute("SELECT payload FROM records ORDER BY id")]
    candidates = {name: [] for name in source_names}
    stale_count = 0
    for record in records:
        if (record.get("visibility") not in allowed or record.get("trust") == "untrusted" or
                record.get("trusted") is False or record.get("status") == "untrusted" or
                record.get("kind") == "untrusted" or _rejected_inference(record)):
            continue
        source = record.get("source", "")
        if source_directory is not None and Path(source).parent.as_posix() != source_directory:
            continue
        name = Path(source).name
        if name not in candidates:
            continue
        try:
            self._source(source)
            actual = hashlib.sha256((self.vault_root / source).read_bytes()).hexdigest()
            if actual != record.get("source_sha256"):
                stale_count += 1
                continue
        except (ValueError, OSError):
            stale_count += 1
            continue
        preferred = 0 if any(part.casefold().endswith(("companion", "echo")) for part in Path(source).parts[:-1]) else 1
        candidates[name].append((preferred, len(source), source, record))
    chosen = []
    missing = []
    for name in source_names:
        if not candidates[name]:
            missing.append(name)
            continue
        record = min(candidates[name], key=lambda item: item[:3])[3]
        chosen.append({key: record[key] for key in ("id", "source", "title", "kind", "status", "updated_at") if key in record})
        text = record.get("text", "")
        chosen[-1]["text"] = text_transform(name, text) if text_transform else text
    result = {"records": chosen, "citations": [{"id": record["id"], "source": record["source"]} for record in chosen],
              "requested_sources": list(source_names), "missing_sources": missing,
              "stale_excluded": stale_count, "truncated": False}
    empty = json.loads(json.dumps(result))
    for record in empty["records"]:
        record["text"] = ""
    available = budget_chars - len(_json(empty))
    if available < 0:
        return {"records": [], "citations": [], "requested_sources": list(source_names),
                "missing_sources": list(source_names), "stale_excluded": stale_count,
                "truncated": bool(chosen)}
    share = available // max(1, len(chosen))
    tail_names = {"Journal.md", "Kurallar.md"}
    marker = "[truncated]"
    for record in chosen:
        text = record["text"]
        if len(text) > share:
            keep = max(0, share - len(marker) - 1)
            if not keep:
                record["text"] = marker
            elif Path(record["source"]).name in tail_names:
                record["text"] = marker + "\n" + text[-keep:]
            else:
                record["text"] = text[:keep] + "\n" + marker
            result["truncated"] = True
    while len(_json(result)) > budget_chars and any(record["text"] for record in chosen):
        record = max(chosen, key=lambda item: len(item["text"]))
        text = record["text"]
        prefix = marker + "\n"
        suffix = "\n" + marker
        if text.startswith(prefix) and len(text) > len(prefix):
            record["text"] = prefix + text[len(prefix) + 1:]
        elif text.endswith(suffix) and len(text) > len(suffix):
            record["text"] = text[:-len(suffix) - 1] + suffix
        else:
            record["text"] = text[:-1]
        result["truncated"] = True
    return result

class SourceSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-snapshot-test-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.vault = self.vault.resolve()
        self.store = subject.MemoryStore(Path(self.tmp.name) / 'state', self.vault)
        self.addCleanup(self.store.close)
        self.virtual = {}
        # Synced vaults carry the ownership table; ingest-only stores do not (see below).
        with self.store._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS markdown_sources(id TEXT PRIMARY KEY, source TEXT NOT NULL)')

    def own(self, id, source):
        with self.store._connect() as db:
            db.execute('INSERT OR REPLACE INTO markdown_sources VALUES (?,?)', (id, source))

    def add(self, id, source, virtual=False, owned=True, **changes):
        data = ('Synthetic source ' + id).encode('utf-8')
        record = dict(id=id, source=source, text=id + ' 🧠 ' + 'body ' * 200,
                      visibility='internal', source_sha256=hashlib.sha256(data).hexdigest())
        record.update(changes)
        if virtual:
            # Quotes/backslashes cannot be filenames on Windows. Keep them as
            # synthetic DB sources with the same source/hash proof in both paths.
            self.virtual[self.vault / source] = data
            with self.store._connect() as db:
                db.execute('INSERT INTO records VALUES (?,?)', (id, _json(record)))
        else:
            path = self.vault / source
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(data)
            self.store.ingest(record)
        if owned:
            self.own(id, source)

    def assert_equivalent(self, names, **kwargs):
        old = original_snapshot(self.store, names, **kwargs)
        new = self.store.source_snapshot(names, **kwargs)
        self.assertEqual(_json(new).encode('utf-8'), _json(old).encode('utf-8'))
        return new

    def test_equivalence_all_gates_names_order_and_budgets(self):
        self.add('ordinary', 'notes/Other.md')
        self.add('false-positive', 'notes/Unrelated.md', title='Core.md Güncel.md')
        self.add('long', 'other-companion/long/Core.md')
        self.add('plain', 'Core.md', owned=False)
        # Equal candidate keys must retain ORDER BY id, not insertion order.
        self.add('z-tie', 'echo/Core.md')
        self.add('a-tie', 'echo/Core.md')
        self.add('private', 'private/Threads.md', visibility='private')
        self.add('public', 'public/Threads.md', visibility='public', owned=False)
        for key, value in [('trust', 'untrusted'), ('trusted', False),
                           ('status', 'untrusted'), ('kind', 'untrusted')]:
            self.add('hidden-' + key, key + '/Core.md', **{key: value})
        self.add('rejected', 'rejected/Core.md', kind='inference', validity='rejected',
                 rejected_reason='Synthetic correction', rejected_at='2026-10-09')
        self.add('legacy-rejected', 'legacy/Core.md', kind='preference', status='rejected')
        self.add('stale', 'stale/Core.md')
        (self.vault / 'stale/Core.md').write_text('Changed source', encoding='utf-8')
        self.add('deleted', 'deleted/Threads.md')
        (self.vault / 'deleted/Threads.md').unlink()
        for name in ('Güncel.md', 'Arşiv', 'Arşiv.md', 'Journal.md', 'Kurallar.md'):
            self.add(name, 'Arşiv/' + name)
        self.add('quote', 'Arşiv/"Güncel".md', virtual=True, owned=False)
        self.add('backslash-directory', 'Arşiv\\folder/Last-Session.md', virtual=True)
        self.add('knowledge', 'knowledge/index.md')
        self.add('wrong-index', 'index.md')
        names = ['Core.md', 'Threads.md', 'Güncel.md', 'Arşiv', 'Arşiv.md', '"Güncel".md',
                 'Last-Session.md', 'Journal.md', 'Kurallar.md', 'Missing.md', 'Core.md']
        read_bytes = Path.read_bytes
        source_check = self.store._source

        def read(path):
            return self.virtual[path] if path in self.virtual else read_bytes(path)

        def verify(source):
            return source if self.vault / source in self.virtual else source_check(source)

        with patch.object(Path, 'read_bytes', read), patch.object(self.store, '_source', verify):
            for audience in ('public', 'internal', 'private'):
                for directory in (None, 'echo', 'Arşiv', 'knowledge', 'missing'):
                    for budget in (0, 100, 900, 4000, 20000):
                        with self.subTest(audience=audience, directory=directory, budget=budget):
                            self.assert_equivalent(names, audience=audience, source_directory=directory,
                                                   budget_chars=budget,
                                                   text_transform=lambda name, text: name + '\n' + text)
            result = self.assert_equivalent(names, budget_chars=20000)
            self.assertEqual(result['records'][0]['id'], 'a-tie')
            self.assertEqual(result['stale_excluded'], 2)
            self.assertEqual(result['missing_sources'], ['Missing.md'])
            self.assert_equivalent(['index.md'], source_directory='knowledge', budget_chars=4000)
            self.assert_equivalent([])
            # Oversized requests preserve the original scan without exceeding SQL limits.
            self.assert_equivalent(names + [f'absent-{i}.md' for i in range(1100)], budget_chars=100000)

    def test_json_decodes_do_not_scale_with_unrelated_records(self):
        self.add('companion', 'echo/Core.md')
        loads = subject.json.loads

        def count(function):
            with patch.object(subject.json, 'loads', wraps=loads) as decode:
                function(self.store, ['Core.md'], budget_chars=3000)
                return decode.call_count

        before = count(subject.MemoryStore.source_snapshot)
        # Batch synthetic nonmatching ingest payloads; no file checks can be needed
        # for these sources under the unchanged Python basename filter either.
        with self.store._connect() as db:
            db.executemany('INSERT INTO records VALUES (?,?)',
                           ((f'note-{i}', _json(dict(id=f'note-{i}', source=f'notes/{i}.md',
                                                    text='Synthetic unrelated note', visibility='internal')))
                            for i in range(2000)))
            db.executemany('INSERT INTO markdown_sources VALUES (?,?)', ((f'note-{i}', f'notes/{i}.md') for i in range(2000)))
        after = count(subject.MemoryStore.source_snapshot)
        oracle = count(original_snapshot)
        self.assertEqual(before, 2)  # One candidate plus the existing budget copy.
        self.assertEqual(after, before)
        self.assertEqual(oracle, after + 2000)
        self.assert_equivalent(['Core.md'], budget_chars=3000)

    def test_store_without_ownership_table_keeps_full_scan(self):
        self.add('companion', 'echo/Core.md')
        self.add('ingested', 'notes/Threads.md', owned=False)
        with self.store._connect() as db:
            db.execute('DROP TABLE markdown_sources')
        for names in (['Core.md'], ['Threads.md', 'Core.md'], []):
            self.assert_equivalent(names, budget_chars=3000)

    def test_invalid_requests_still_rejected(self):
        for names in ('Core.md', [''], ['dir/Core.md'], ['quote\\name.md'], [None]):
            with self.subTest(names=names):
                for function in (original_snapshot, subject.MemoryStore.source_snapshot):
                    with self.assertRaises(ValueError):
                        function(self.store, names)
        for kwargs in ({'audience': 'unknown'}, {'budget_chars': -1}, {'budget_chars': True}):
            for function in (original_snapshot, subject.MemoryStore.source_snapshot):
                with self.assertRaises(ValueError):
                    function(self.store, ['Core.md'], **kwargs)


if __name__ == '__main__':
    unittest.main()
