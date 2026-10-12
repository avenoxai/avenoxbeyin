"""Unit tests for rule scoping and dynamic rule layering (Issue #288 Item 1)."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_companion as companion
import beyin_v3 as runtime


class RulesScopingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-scoping-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir()
        self.store = runtime.MemoryStore(self.state, self.vault)

    def test_rule_scopes_parsing(self):
        self.assertEqual(companion.rule_scopes('- Genel kural'), set())
        self.assertEqual(companion.rule_scopes('- [scope: global] Genel kural'), set())
        self.assertEqual(companion.rule_scopes('- [kapsam: genel] Genel kural'), set())
        self.assertEqual(companion.rule_scopes('- [scope: backend] Sadece backend kuralı'), {'backend'})
        self.assertEqual(companion.rule_scopes('- [kapsam: frontend, ui] UI kuralı'), {'frontend', 'ui'})
        self.assertEqual(companion.rule_scopes('- scope: mobile Mobil kural'), {'mobile'})

    def test_filter_scoped_rules_without_active_project_keeps_all(self):
        text = (
            "# Kurallar\n\n"
            "- Genel kural bir.\n"
            "- [scope: backend] Backend kuralı.\n"
            "- [scope: frontend] Frontend kuralı.\n"
        )
        self.assertEqual(companion.filter_scoped_rules(text, active_project=None), text)
        self.assertEqual(companion.filter_scoped_rules(text, active_project=""), text)

    def test_filter_scoped_rules_with_active_project(self):
        text = (
            "# Kurallar\n\n"
            "- Genel kural bir.\n\n"
            "- [scope: backend] Backend kuralı.\n\n"
            "- [scope: frontend] Frontend kuralı.\n\n"
            "- [kapsam: backend, infra] Altyapı kuralı.\n"
        )
        filtered_backend = companion.filter_scoped_rules(text, active_project="backend")
        self.assertIn("Genel kural bir", filtered_backend)
        self.assertIn("Backend kuralı", filtered_backend)
        self.assertIn("Altyapı kuralı", filtered_backend)
        self.assertNotIn("Frontend kuralı", filtered_backend)

        filtered_frontend = companion.filter_scoped_rules(text, active_project="frontend")
        self.assertIn("Genel kural bir", filtered_frontend)
        self.assertIn("Frontend kuralı", filtered_frontend)
        self.assertNotIn("Backend kuralı", filtered_frontend)
        self.assertNotIn("Altyapı kuralı", filtered_frontend)

    def test_project_folder_rules_are_loaded_into_companion_context(self):
        comp = self.vault / '🔮 850-Companion'
        comp.mkdir(parents=True)
        (comp / 'Kurallar.md').write_text('# Kurallar\n- Global kural\n', encoding='utf-8')
        (comp / 'Core.md').write_text('# Core\nKimlik.\n', encoding='utf-8')
        (comp / 'Last-Session.md').write_text('## 2026-10-12 00:00 · etiket · abcd1234\nDevir.\n', encoding='utf-8')
        (comp / 'Threads.md').write_text('## Active Threads\n### Thread: T1\n**Status:** active\n', encoding='utf-8')

        # Create project specific rules in projects/alpha/Kurallar.md
        proj = self.vault / 'projects' / 'alpha'
        proj.mkdir(parents=True)
        (proj / 'Kurallar.md').write_text('# Alpha Kuralları\n- Alpha özel kuralı\n', encoding='utf-8')

        # Without project, project rules are not loaded
        ctx_no_proj = companion.context(self.store, 8000, 'session-1', 'claude', project=None)
        self.assertNotIn('Alpha özel kuralı', ctx_no_proj)

        # With project='alpha', project rules are loaded into companion context
        ctx_proj = companion.context(self.store, 8000, 'session-1', 'claude', project='alpha')
        self.assertIn('[projects/alpha/Kurallar.md]', ctx_proj)
        self.assertIn('Alpha özel kuralı', ctx_proj)


if __name__ == '__main__':
    unittest.main()
