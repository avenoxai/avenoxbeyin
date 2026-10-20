"""Explicit parameters stay explicit: a misspelled keyword is a TypeError, not a silent default (#426)."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_bridge as bridge  # noqa: E402
import beyin_v3_compact as compact  # noqa: E402
import beyin_v3_jev as jev  # noqa: E402

SPEC = importlib.util.spec_from_file_location('beyin_signature_installer', ROOT / 'scripts/install_v3.py')
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class ExplicitSignatureTest(unittest.TestCase):
    def test_unknown_keyword_is_rejected_before_the_body_runs(self):
        # Under *args/**kwargs these calls ran with budget=1200, source_relative=None,
        # kept=() and transport=None, and nobody noticed the typo.
        calls = {
            'project_context': lambda: bridge.project_context(Path('v'), Path('s'), 'id', 'ad', budjet=150),
            '_record_decisions': lambda: compact._record_decisions(Path('t'), Path('v'), [], 'a.md', '\n',
                                                                   source_relativ='x'),
            'semantic_unchanged': lambda: INSTALLER.semantic_unchanged('AGENTS.md', b'', b'', [], kep=()),
            '_packaged': lambda: jev._packaged(None, 'q', [], None, None, None, {}, None, transprt=None),
        }
        for name, call in calls.items():
            with self.subTest(name=name), self.assertRaises(TypeError):
                call()


if __name__ == '__main__':
    unittest.main()
