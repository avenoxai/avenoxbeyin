"""Default context packing stays byte for byte as in 3.18.0 (#975 share, #974 list).

`context --share balanced` and `context --list` are opt-in. Without them, pack_context,
pack_widened and render_context must deliver exactly what the 3.18.0 engine delivered. The
digest below was computed by running grid() against the 3.18.0 engine (origin/main 18001d2a);
change it only together with a deliberate change of the default delivery.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import beyin_v3 as runtime  # noqa: E402

if str(ROOT / 'tests') not in sys.path:
    sys.path.insert(0, str(ROOT / 'tests'))
from v3_package_helpers import inherited_env, isolated_env  # noqa: E402

DEFAULT_PATH_SHA256 = 'f3f4d7ae488ba3086fee5c7fd268c0dd0eaa0a5a890f38f95696f049000d5716'
FILLER = ('İstanbul', 'KALDIK', 'ılık', '"quoted"', 'back\\slash', 'çay', 'bud', 'drain', 'gate', 'note')
TERMS = ('orchard', 'pruning', 'tree', 'şifre', 'müşteri', 'arşivi')
SIZES = (0, 120, 151, 480, 520, 1150, 2600, 5200)


def _text(seed, size):
    """Filler first; the query terms only in the later part, so clipping must find them."""
    parts, total, step = [], 0, 0
    while total < size:
        words = TERMS + FILLER if total > size * (0.3 + 0.1 * (seed % 5)) else FILLER
        word = words[(seed * 7 + step * 3) % len(words)]
        piece = '\n\n## ' + word + '\n\n' if step % 17 == 5 else word + ' '
        parts.append(piece)
        total += len(piece)
        step += 1
    return ''.join(parts)[:size]


def _records(count, offset):
    records = []
    for i in range(count):
        record = {'id': f'md-{count}-{offset}-{i}', 'source': f'notes/n{i}.md', 'kind': 'note',
                  'text': _text(i + offset, SIZES[(i + offset) % len(SIZES)]), 'facts': {}, 'revision': 1,
                  'visibility': 'internal', 'source_sha256': 'a' * 64}
        if (i + offset) % 3 == 0:
            record.update(title=f'Not {i} başlık', facts={'k': 'v' * (i * 40)}, project='p1')
        records.append(record)
    return records


def grid():
    """Every default-path delivery of a fixed grid, as one JSON-serializable list."""
    out = []
    for count in (1, 3, 5, 7):
        for offset in (0, 2):
            records = _records(count, offset)
            wider = {r['id']: dict(r, text=r['text'] + ' wider ' + r['text'][:120]) for r in records[::2]}
            extensions = {r['id']: [dict(r, text=r['text'] + ' extra' * 20)] for r in records[1::2]}
            for limit in (1, 5, 8):
                for budget in (250, 900, 1500, 2200, 4000, 8000):
                    for query in (None, 'orchard pruning', 'şifre müşteri arşivi'):
                        project = 'p1' if count == 5 and offset == 2 else None
                        packed = runtime.pack_context(records, limit, budget, offset, query=query, project=project)
                        widened = runtime.pack_widened(records, wider, limit, budget, 0, extensions=extensions,
                                                       query=query, project=project)
                        widened.wider, widened.extensions = wider, extensions
                        text, delivered = runtime.render_context(widened, budget + 200, prefix='P\n', suffix='\nS')
                        out.append([packed, [s['id'] for s in packed.sources],
                                    widened, [s['id'] for s in widened.sources],
                                    text, delivered, [s['id'] for s in delivered.sources]])
    return out


def digest():
    return hashlib.sha256(json.dumps(grid(), ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


class DefaultContextPathTest(unittest.TestCase):
    def test_default_delivery_matches_3_18_0_byte_for_byte(self):
        self.assertEqual(digest(), DEFAULT_PATH_SHA256)

    def test_explicit_top_with_scores_and_list_off_equal_the_default(self):
        for count in (1, 3, 5, 7):
            records = _records(count, 1)
            scores = [float(count - i) for i in range(count)]
            for limit in (1, 5, 8):
                for budget in (250, 2200, 8000):
                    for query in (None, 'orchard pruning'):
                        default = runtime.pack_context(records, limit, budget, 0, query=query)
                        explicit = runtime.pack_context(records, limit, budget, 0, query=query, share='top',
                                                        scores=scores, list_mode=False)
                        self.assertEqual(json.dumps(explicit, ensure_ascii=False), json.dumps(default, ensure_ascii=False))
                        self.assertEqual([s['id'] for s in explicit.sources], [s['id'] for s in default.sources])
                        rendered = runtime.render_context(explicit, budget, prefix='P')[0]
                        self.assertEqual(rendered, runtime.render_context(default, budget, prefix='P')[0])


class ListAndShareTogetherTest(unittest.TestCase):
    """A candidate list carries no note bodies, so a budget share has nothing to act on."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-context-list-share-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        (self.vault / 'notes').mkdir(parents=True)
        for i in range(6):
            body = f'# Budama {i}\n\n' + ('Dolgu satırı. ' * 120) + f'\n\n## Kış\n\nMeyve ağacı budama takvimi {i}.\n'
            (self.vault / f'notes/budama-{i}.md').write_text(body, encoding='utf-8', newline='\n')
        self.env = inherited_env(**isolated_env(self.root / 'home'), BEYIN_UPDATES_OFF='1')

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                               '--state', str(self.state), *args],
                              capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=60)

    def test_explicit_share_with_list_is_rejected(self):
        for extra in (['--share', 'balanced'], ['--share', 'top']):
            with self.subTest(extra=extra):
                result = self.run_cli('context', 'meyve ağacı budama', '--list', *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertEqual(json.loads(result.stderr)['message'], '--share cannot be combined with --list')
        request = self.root / 'request.json'
        request.write_text(json.dumps({'query': 'meyve ağacı budama', 'share': 'balanced'}), encoding='utf-8', newline='\n')
        result = self.run_cli('context', '--file', str(request), '--list')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--share cannot be combined with --list', result.stderr)

    def test_saved_share_preference_leaves_the_list_unchanged(self):
        plain = self.run_cli('context', 'meyve ağacı budama', '--list', '--limit', '6')
        self.assertEqual(plain.returncode, 0, plain.stderr)
        saved = self.run_cli('preferences', '--context-share', 'balanced')
        self.assertEqual(saved.returncode, 0, saved.stderr)
        listed = self.run_cli('context', 'meyve ağacı budama', '--list', '--limit', '6')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout, plain.stdout)
        self.assertEqual(len(json.loads(listed.stdout)['records']), 6)


if __name__ == '__main__':
    unittest.main()
