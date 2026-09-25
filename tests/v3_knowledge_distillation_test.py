"""Tests for knowledge distillation recency and V2 instruction conflicts (Issue #109)."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
import importlib.util
import io
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

import beyin_entry
from beyin_v3_projections import knowledge_freshness, check_instruction_conflicts
from beyin_v3_sync import SyncEngine


class KnowledgeDistillationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        self.state.mkdir()

    def test_knowledge_freshness_clean_vault_without_knowledge(self):
        """When no knowledge notes exist, last_distilled_at is None and total receipts are reported."""
        engine = SyncEngine(self.vault, self.state)
        with engine.store._connect() as db:
            freshness = knowledge_freshness(self.vault, db)
        self.assertIsNone(freshness['last_distilled_at'])
        self.assertIsNone(freshness['days_ago'])
        self.assertEqual(freshness['receipts_since'], 0)
        self.assertEqual(freshness['total_receipts'], 0)

        # Human rendering check
        text = beyin_entry.human_result({'knowledge_freshness': freshness}, 'doctor')
        self.assertIn('Son bilgi damitmasi: henuz kavram notu damitilmadi (toplam 0 makbuz)', text)

    def test_knowledge_freshness_counts_receipts_since_last_knowledge_update(self):
        """Knowledge freshness calculates days ago and counts only receipts created since."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time()
        five_days_ago = now - 5 * 86400

        # Create knowledge note with mtime 5 days ago
        kn_dir = self.vault / 'knowledge/concepts'
        kn_dir.mkdir(parents=True)
        note = kn_dir / 'sqlite-wal.md'
        note.write_text('# SQLite WAL\nDetails here.', encoding='utf-8')
        os.utime(note, (five_days_ago, five_days_ago))

        # Receipt 1: created 6 days ago (before knowledge note)
        t_6d = datetime.fromtimestamp(now - 6 * 86400, timezone.utc).isoformat()
        # Receipt 2: created 3 days ago (after knowledge note)
        t_3d = datetime.fromtimestamp(now - 3 * 86400, timezone.utc).isoformat()
        # Receipt 3: created 1 day ago (after knowledge note)
        t_1d = datetime.fromtimestamp(now - 1 * 86400, timezone.utc).isoformat()

        with engine.store._connect() as db:
            db.execute("INSERT INTO receipts VALUES (?,?)", ('r1', json.dumps({'event_id': 'r1', 'created_at': t_6d})))
            db.execute("INSERT INTO receipts VALUES (?,?)", ('r2', json.dumps({'event_id': 'r2', 'created_at': t_3d})))
            db.execute("INSERT INTO receipts VALUES (?,?)", ('r3', json.dumps({'event_id': 'r3', 'created_at': t_1d})))

            freshness = knowledge_freshness(self.vault, db, now=now)

        self.assertEqual(freshness['days_ago'], 5)
        self.assertEqual(freshness['receipts_since'], 2)
        self.assertEqual(freshness['total_receipts'], 3)
        self.assertEqual(freshness['latest_source'], 'knowledge/concepts/sqlite-wal.md')

        text = beyin_entry.human_result({'knowledge_freshness': freshness}, 'doctor')
        self.assertIn('Son bilgi damitmasi: 5 gun once (o tarihten beri 2 makbuz)', text)

    def test_knowledge_freshness_ignores_generated_projections_and_gitkeep(self):
        """knowledge/v3/outcomes.md (generated receipt view) and .gitkeep must not count as knowledge distillation."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time()

        # Add .gitkeep and generated outcomes
        v3_dir = self.vault / 'knowledge/v3'
        v3_dir.mkdir(parents=True)
        (self.vault / 'knowledge/.gitkeep').touch()
        outcomes = v3_dir / 'outcomes.md'
        outcomes.write_text('---\n{"generated": true}\n---\n# Outcomes', encoding='utf-8')

        with engine.store._connect() as db:
            freshness = knowledge_freshness(self.vault, db, now=now)

        self.assertIsNone(freshness['last_distilled_at'])
        self.assertIsNone(freshness['latest_source'])

    def test_knowledge_freshness_ignores_template_seed_files(self):
        """A fresh install ships knowledge/index.md and log.md; they are not a distillation."""
        engine = SyncEngine(self.vault, self.state)
        (self.vault / 'knowledge/concepts').mkdir(parents=True)
        (self.vault / 'knowledge/concepts/.gitkeep').touch()
        (self.vault / 'knowledge/index.md').write_text('# Bilgi Tabanı İndeksi\n', encoding='utf-8')
        (self.vault / 'knowledge/log.md').write_text('# Derleme Günlüğü\n', encoding='utf-8')
        with engine.store._connect() as db:
            freshness = knowledge_freshness(self.vault, db)
        self.assertIsNone(freshness['last_distilled_at'])
        text = beyin_entry.human_result({'knowledge_freshness': freshness}, 'doctor')
        self.assertIn('henuz kavram notu damitilmadi', text)
        self.assertNotIn('bugun', text)

    def test_knowledge_freshness_prefers_frontmatter_date_over_checkout_mtime(self):
        """git checkout and iCloud restore reset mtime; the recorded updated/modified date wins."""
        engine = SyncEngine(self.vault, self.state)
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc).timestamp()
        concepts = self.vault / 'knowledge/concepts'
        concepts.mkdir(parents=True)
        (concepts / 'yaml.md').write_text('---\ntitle: WAL\nupdated: 2026-06-01\n---\nx\n', encoding='utf-8')
        (concepts / 'json.md').write_text('---\n{"id": "j", "modified": "2026-06-10T08:00:00"}\n---\nx\n', encoding='utf-8')
        for note in concepts.iterdir():
            os.utime(note, (now, now))  # what a fresh checkout looks like
        with engine.store._connect() as db:
            freshness = knowledge_freshness(self.vault, db, now=now)
        self.assertEqual(freshness['latest_source'], 'knowledge/concepts/json.md')
        self.assertGreaterEqual(freshness['days_ago'], 106)

    def test_doctor_renders_unavailable_freshness_as_unmeasured(self):
        text = beyin_entry.human_result(
            {'knowledge_freshness': {'status': 'unavailable', 'error': 'OperationalError'}}, 'doctor')
        self.assertIn('Son bilgi damitmasi: olculemedi (OperationalError)', text)
        self.assertNotIn('henuz kavram notu damitilmadi', text)

    def test_doctor_keeps_conflicts_when_freshness_fails(self):
        (self.vault / 'AGENTS.md').write_text('- `knowledge/` (elle düzenleme, derleyici yönetir)\n', encoding='utf-8')
        spec = importlib.util.spec_from_file_location('v3_distillation_cli', ROOT / 'scripts/beyin_v3.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        out = io.StringIO()
        with patch('beyin_v3_projections.knowledge_freshness', side_effect=OSError('evicted')), redirect_stdout(out):
            self.assertEqual(cli.main(['--vault', str(self.vault), '--state', str(self.state), 'doctor']), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc['knowledge_freshness'], {'status': 'unavailable', 'error': 'OSError'})
        self.assertEqual(len(doc['instruction_conflicts']), 1)
        self.assertEqual(doc['status'], 'needs_attention')

    def test_instruction_conflicts_detected_outside_managed_block(self):
        """Legacy V2 compiler instructions outside the managed block are detected and flagged."""
        agents_md = self.vault / 'AGENTS.md'
        agents_md.write_text(
            "# Kurallar\n\n"
            "| Derlenmiş bilgi (makine) | knowledge/ (elle düzenleme, derleyici yönetir) |\n\n"
            "<!-- beyin-v3:start -->\n"
            "## V3 companion\n"
            "The active agent now performs knowledge synthesis.\n"
            "<!-- beyin-v3:end -->\n",
            encoding='utf-8'
        )

        conflicts = check_instruction_conflicts(self.vault)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['file'], 'AGENTS.md')
        self.assertIn('derleyici yönetir', conflicts[0]['snippet'])

        # Doctor output test
        text = beyin_entry.human_result({'instruction_conflicts': conflicts}, 'doctor')
        self.assertIn('Talimat celiskisi: AGENTS.md icinde V2 derleyici ifadesi var', text)

    def test_instruction_conflicts_clean_managed_vault(self):
        """Standard clean V3 vaults without legacy compiler warnings produce no conflicts."""
        agents_md = self.vault / 'AGENTS.md'
        agents_md.write_text(
            "# Proje\n\n"
            "<!-- beyin-v3:start -->\n"
            "The active agent now performs knowledge synthesis.\n"
            "<!-- beyin-v3:end -->\n",
            encoding='utf-8'
        )

        conflicts = check_instruction_conflicts(self.vault)
        self.assertEqual(conflicts, [])

    def test_instruction_conflicts_ignore_unrelated_rules(self):
        """Only V2 wording that hands knowledge/ to the compiler counts; ordinary rules do not."""
        clean = [
            'knowledge/ notlarına dokunmadan önce knowledge/index.md oku.',
            'Old compiler is retired; agents write knowledge/concepts directly.',
            'Operator knowledge base: derleyici çıktısı değil, elle yazılır.',
            'Gece derleyicisi artık yok; knowledge/ notlarını ajan damıtır.',
        ]
        for line in clean:
            with self.subTest(line=line):
                (self.vault / 'AGENTS.md').write_text('# Kurallar\n' + line + '\n', encoding='utf-8')
                self.assertEqual(check_instruction_conflicts(self.vault), [])
        for line in ('- `knowledge/` (elle düzenleme, derleyici yönetir)',
                     '- KNOWLEDGE/ (ELLE DÜZENLEME, DERLEYİCİ YÖNETİR)',
                     '- knowledge/ is compiler-managed; do not edit.'):
            with self.subTest(line=line):
                (self.vault / 'AGENTS.md').write_text(line + '\n', encoding='utf-8')
                self.assertEqual(len(check_instruction_conflicts(self.vault)), 1)

    def test_shipped_template_instructions_have_no_conflict(self):
        template = ROOT / 'template'
        self.assertEqual(check_instruction_conflicts(template), [])

    def test_doctor_cli_includes_freshness_and_conflicts(self):
        """Full CLI 'doctor' command includes knowledge_freshness and instruction_conflicts in JSON output."""
        engine = SyncEngine(self.vault, self.state)
        engine.note_create('knowledge/concepts/sample.md', 'Sample concept note.', {'id': 's-1'})

        agents_md = self.vault / 'AGENTS.md'
        agents_md.write_text(
            "knowledge/ klasörünü elle düzenleme, derleyici yönetir\n"
            "<!-- beyin-v3:start -->\n"
            "<!-- beyin-v3:end -->\n",
            encoding='utf-8'
        )

        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
               '--state', str(self.state), 'doctor']
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        doc = json.loads(proc.stdout)

        self.assertIn('knowledge_freshness', doc)
        self.assertIsNotNone(doc['knowledge_freshness']['last_distilled_at'])
        self.assertIn('instruction_conflicts', doc)
        self.assertEqual(len(doc['instruction_conflicts']), 1)
        self.assertEqual(doc['status'], 'needs_attention')


if __name__ == '__main__':
    unittest.main()
