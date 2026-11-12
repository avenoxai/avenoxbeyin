"""Companion regression scenarios: installed package, rich context, and user data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from v3_package_helpers import inherited_env, without_hook_clock

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
COMPANION = '🔮 850-Companion'


class CompanionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='companion-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'Örnek Beyin'; self.vault.mkdir()
        self.state = self.base / 'state'
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')

    def run_cli(self, script, *args, payload=None):
        result = subprocess.run([sys.executable, str(script), *map(str, args)],
                                input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, encoding='utf-8', env=self.env,
                                cwd=self.vault, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def install(self):
        return self.run_cli(ROOT / 'scripts/install_v3.py', '--vault', self.vault, '--state', self.state)

    def sync(self):
        return self.run_cli(ROOT / 'scripts/beyin_v3.py', '--vault', self.vault, '--state', self.state, 'sync')

    def hook(self, event='SessionStart', harness='codex', **kwargs):
        payload = dict(hook_event_name=event, session_id='synthetic-continuity', event_id=event,
                       conversationId='synthetic-continuity', invocationNum=0, **kwargs)
        output = self.run_cli(ROOT / 'template/.claude/scripts/beyin_v3_hook.py',
                              '--vault', self.vault, '--state', self.state, '--harness', harness, payload=payload)
        return output.get('hookSpecificOutput', {}).get('additionalContext', '') if harness != 'antigravity' else output.get('injectSteps', [{}])[0].get('ephemeralMessage', '')

    def seed(self, folder=COMPANION):
        directory = self.vault / folder; directory.mkdir(exist_ok=True)
        bodies = {
            'Core.md': '# Kimlik\nBen Ada, kullanıcının düşünme ortağıyım. IDENTITY_CANARY\n' + 'Eski bilgi. ' * 180,
            'Soul.md': '# Üslup\nSTYLE_CANARY: sıcak, açık sözlü, kısa cümlelerle konuş.\n',
            'Kurallar.md': '# Kurallar\n' + 'eski kural\n' * 200 + 'CORRECTION_CANARY: gereksiz övgü kullanma.\n',
            'Last-Session.md': '# Son oturum\nHANDOFF_CANARY: prototipi denedik, video kaydı bekliyor.\n## Previous\nOLD_SESSION_NOISE\n',
            'Threads.md': '# Konular\n## Active Threads\n### Thread: Eğitim videosu\n**Status:** active\nTHREAD_BODY_CANARY: Derya ses denemesini yapacak.\n## Closed Threads\nCLOSED_THREAD_NOISE',
            'Journal.md': '# Journal\n## 2026-09-17\nLATEST_JOURNAL_CANARY: örnek üzerinden ilerlemek yararlı oldu.\n## 2026-08-01\nOLD_JOURNAL_NOISE\n',
        }
        for name, body in bodies.items():
            (directory / name).write_text(body, encoding='utf-8')
        (self.vault / 'future-daily.md').write_text('---\n{"updated_at":"2099-01-01T00:00:00Z"}\n---\n' + 'Distractor ' * 2000)
        archive = self.vault / 'archive'; archive.mkdir(exist_ok=True)
        (archive / 'Core.md').write_text('WRONG_IDENTITY_CANARY')
        self.sync()
        return directory

    def test_fresh_package_bootstraps_user_owned_identity(self):
        package = self.base / 'release.zip'
        self.run_cli(ROOT / 'scripts/build_v3_release.py', '--output', package, '--version', '3.0.3')
        extracted = self.base / 'package'
        with zipfile.ZipFile(package) as archive:
            archive.extractall(extracted)
        self.run_cli(extracted / 'scripts/install_v3.py', '--vault', self.vault, '--state', self.state)
        core = self.vault / COMPANION / 'Core.md'
        self.assertTrue(core.is_file(), 'Fresh V3 must create identity sources, not only a technical router')
        self.assertIn('düşünme ortağı', (self.vault / 'AGENTS.md').read_text(encoding='utf-8'))
        for name in ('Core.md', 'Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md'):
            self.assertTrue((core.parent / name).exists())
        manifest = json.loads((self.state / 'v3-install.json').read_text())
        self.assertNotIn(f'{COMPANION}/Core.md', manifest['files'])
        core.write_text('USER_OWNED_IDENTITY', encoding='utf-8')
        self.run_cli(extracted / 'scripts/install_v3.py', '--vault', self.vault, '--state', self.state)
        self.run_cli(self.vault / 'beyin.py', 'rollback')
        self.assertEqual(core.read_text(), 'USER_OWNED_IDENTITY')

    def test_rich_identity_survives_all_three_client_contexts(self):
        self.seed()
        contexts = []
        for harness in ('codex', 'claude', 'antigravity'):
            text = self.hook('PreInvocation' if harness == 'antigravity' else 'SessionStart', harness)
            for canary in ('IDENTITY_CANARY', 'STYLE_CANARY', 'CORRECTION_CANARY', 'HANDOFF_CANARY', 'THREAD_BODY_CANARY', 'LATEST_JOURNAL_CANARY'):
                self.assertIn(canary, text)
            for noise in ('WRONG_IDENTITY_CANARY', 'CLOSED_THREAD_NOISE', 'OLD_JOURNAL_NOISE', 'OLD_SESSION_NOISE'):
                self.assertNotIn(noise, text)
            self.assertLessEqual(len(text), 5000)
            contexts.append(without_hook_clock(text))
        self.assertEqual(contexts[0], contexts[1])
        self.assertEqual(contexts[0], contexts[2])

    def test_economical_budget_keeps_identity_rules_and_handoff(self):
        self.seed()
        (self.vault / '.beyin-preferences.json').write_text(json.dumps({'context_chars': 2000, 'context_mode': 'session'}))
        text = self.hook()
        for canary in ('IDENTITY_CANARY', 'STYLE_CANARY', 'CORRECTION_CANARY', 'HANDOFF_CANARY', 'THREAD_BODY_CANARY', 'LATEST_JOURNAL_CANARY'):
            self.assertIn(canary, text)
        self.assertIn('truncated: read source', text)
        self.assertLessEqual(len(text), 2000)

    def test_relational_question_restores_context_without_lexical_overlap(self):
        self.seed()
        for prompt in ('Beni tanıyor musun? Geçen sefer nerede kalmıştık?', 'What did we do last time?'):
            text = self.hook('UserPromptSubmit', prompt=prompt)
            self.assertIn('IDENTITY_CANARY', text)
            self.assertIn('HANDOFF_CANARY', text)

    def test_no_memory_does_not_inject_or_enqueue(self):
        self.seed()
        self.assertEqual(self.hook(no_memory=True), '')
        self.assertEqual(list((self.state / 'hook-queue').glob('*.json')), [])

    def test_existing_custom_identity_is_preserved_without_duplicate_companion(self):
        target = self.vault / 'My Partner'; target.mkdir()
        (target / 'Soul.md').write_text('CUSTOM_IDENTITY_CANARY')
        self.install()
        self.assertFalse((self.vault / COMPANION).exists())
        self.assertFalse((target / 'Core.md').exists())
        self.assertEqual((target / 'Soul.md').read_text(), 'CUSTOM_IDENTITY_CANARY')
        self.assertTrue((target / 'Threads.md').is_file())
        self.sync()
        self.assertIn('CUSTOM_IDENTITY_CANARY', self.hook())

    def test_private_untrusted_and_changed_identity_never_leaks(self):
        target = self.seed()
        (target / 'Core.md').write_text('---\n{"visibility":"private"}\n---\nPRIVATE_IDENTITY_CANARY')
        (target / 'Soul.md').write_text('---\n{"trust":"untrusted"}\n---\nUNTRUSTED_IDENTITY_CANARY')
        self.sync()
        (target / 'Last-Session.md').write_text('CHANGED_HANDOFF_CANARY')
        text = self.hook()
        for canary in ('PRIVATE_IDENTITY_CANARY', 'UNTRUSTED_IDENTITY_CANARY', 'CHANGED_HANDOFF_CANARY', 'HANDOFF_CANARY'):
            self.assertNotIn(canary, text)
        self.assertIn('excluded', text)

    def test_rejected_inference_is_excluded_from_companion_and_related_context(self):
        target = self.seed()
        (target / 'Core.md').write_text(
            '---\nkind: inference\nvalidity: rejected\nrejected_at: 2026-09-24\n'
            'rejected_reason: User corrected the inference.\n---\n'
            'SYNTHETIC_REJECTED_IDENTITY_CANARY: user prefers amber diagrams.\n',
            encoding='utf-8')
        self.sync()
        for text in (self.hook(), self.hook('UserPromptSubmit', prompt='Beni tanıyor musun? Amber diagrams?')):
            self.assertNotIn('SYNTHETIC_REJECTED_IDENTITY_CANARY', text)
            self.assertIn('STYLE_CANARY', text)

    def test_ambiguous_identity_is_not_silently_selected(self):
        self.seed()
        other = self.vault / 'Other Companion'; other.mkdir()
        (other / 'Core.md').write_text('OTHER_IDENTITY_CANARY')
        self.sync()
        text = self.hook()
        self.assertIn('Multiple companion directories', text)
        self.assertNotIn('IDENTITY_CANARY', text)

    def test_large_newest_first_journal_is_selected_before_budget_clipping(self):
        target = self.seed()
        (target / 'Journal.md').write_text('# Journal\n## 2026-09-17\nNEWEST_FIRST_CANARY\n## 2020-01-01\n' + 'older ' * 45000)
        self.sync()
        self.assertIn('NEWEST_FIRST_CANARY', self.hook())

    def test_existing_update_and_rollback_keep_learning_and_preferences(self):
        self.install()
        core = self.vault / COMPANION / 'Core.md'
        core.write_text('LEARNED_IDENTITY_CANARY', encoding='utf-8')
        settings = self.vault / '.beyin-preferences.json'
        settings.write_text('{"context_chars":2000,"context_mode":"session"}')
        package = self.base / 'upgrade.zip'
        self.run_cli(ROOT / 'scripts/build_v3_release.py', '--output', package, '--version', '3.0.3')
        self.run_cli(self.vault / 'beyin.py', 'update', '--package', package)
        self.assertEqual(core.read_text(), 'LEARNED_IDENTITY_CANARY')
        self.assertEqual(json.loads(settings.read_text())['context_chars'], 2000)
        self.run_cli(self.vault / 'beyin.py', 'rollback')
        self.assertEqual(core.read_text(), 'LEARNED_IDENTITY_CANARY')

    def test_bootstrap_after_managed_update_is_idempotent_and_keeps_deleted_notes_deleted(self):
        self.install()
        target = self.vault / COMPANION
        (self.state / 'companion-bootstrap.json').unlink()
        (target / 'Core.md').write_text('UPGRADE_IDENTITY_CANARY')
        (target / 'Journal.md').unlink()
        hook = ROOT / 'template/.claude/scripts/beyin_v3_hook.py'
        self.run_cli(hook, '--vault', self.vault, '--state', self.state, '--harness', 'codex', '--drain-queue')
        self.assertTrue((target / 'Journal.md').exists())
        self.assertEqual((target / 'Core.md').read_text(), 'UPGRADE_IDENTITY_CANARY')
        (target / 'Journal.md').unlink()
        self.run_cli(hook, '--vault', self.vault, '--state', self.state, '--harness', 'codex', '--drain-queue')
        self.assertFalse((target / 'Journal.md').exists())

    def test_knowledge_map_is_available_with_companion_sources(self):
        self.seed()
        knowledge = self.vault / 'knowledge'; knowledge.mkdir()
        (knowledge / 'index.md').write_text('# Bilgi haritası\nCONCEPT_LINK_CANARY [[concepts/deney]]', encoding='utf-8')
        self.sync()
        self.assertIn('CONCEPT_LINK_CANARY', self.hook())

    def test_minimum_budget_remains_bounded_and_symlinks_are_not_followed(self):
        self.seed()
        (self.vault / '.beyin-preferences.json').write_text('{"context_chars":1000}')
        text = self.hook()
        self.assertLessEqual(len(text), 1000)
        self.assertIn('Core.md', text)
        external = self.base / 'external'; external.mkdir()
        (external / 'Core.md').write_text('EXTERNAL_IDENTITY_CANARY')
        target = self.vault / 'Other Companion'
        try:
            target.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        self.sync()
        self.assertNotIn('EXTERNAL_IDENTITY_CANARY', self.hook())

    def test_relevant_continuity_query_patterns_and_exclusions(self):
        scripts_dir = str(ROOT / 'template/.claude/scripts')
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from beyin_v3_companion import relevant
        positives = [
            "Nerede kaldık?",
            "Nerede kalmıştık?",
            "nerede kaldığımızı hatırlıyor musun?",
            "Nerede kalmıştım?",
            "Nerede kalmışız?",
            "Kod incelemesinde nerede kaldım?",
            "nerede kaldigimizi hatırlat",
            "Nerede kaldik acaba?",
            "nerede kalmistik",
            "nerde kaldık",
            "nerde kalmıştık",
            "nerede kaldıydık",
            "nerede kaldıydım",
            "nerde kaldigimizi soyle",
            "en son nerede kaldığım yeri göster",
            "nerede kalmışım",
            "nerede kaldıysak oradan devam edelim",
            "NERDE KALMIŞTIK",
            "NEREDE KALDIĞIMIZI SÖYLE",
            "nerde kaldıgımızı hatırlat",
            "ne yaptık",
            "son oturum",
            "beni hatırla",
            "sen kimsin",
            "where did we leave off",
            "who are you",
        ]
        for query in positives:
            self.assertTrue(relevant(query), f"Expected True for continuity query: {query}")

        negatives = [
            "Kargo nerede kaldı acaba, kontrol eder misin?",
            "Bu ürün nerede kaldı, hâlâ gelmedi mi?",
            "Siparişim nerede kalıyor?",
            "Kargo nerede kalmış?",
            "Paket nerede kalacak?",
            "Bugün toplantı nerede?",
            "Python'da liste nasıl sıralanır?",
            "Kargonun nerede kaldığını kontrol eder misin?",
            "Paketin nerede kaldığı belli değil",
            "nerde kaldı bu kargo",
            "Kargoların nerede kaldıklarını kontrol et",
            "Paketlerin nerede kaldiklarini bul",
            "Misafirler nerede kalacaklar?",
        ]
        for query in negatives:
            self.assertFalse(relevant(query), f"Expected False for unrelated query: {query}")


if __name__ == '__main__':
    unittest.main()
