import unittest
import os
import tempfile
import sys
import shutil
import json
import sqlite3
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# IMPORTANT: Insert template/.claude/scripts BEFORE scripts
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
if str(ROOT / 'scripts') in sys.path:
    sys.path.remove(str(ROOT / 'scripts'))
sys.path.append(str(ROOT / 'scripts'))

from beyin_v3_sync import parse as parse_frontmatter
from beyin_v3 import pack_context
from beyin_v3_projections import recent_receipts, _receipt_instant, receipt_day
from beyin_v3_compact import compact

class TestV3EdgeCasesExtended(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE receipts(id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        
        # Setup mock vault for sync engine / cli
        self.vault_dir = self.temp_dir / 'vault'
        self.vault_dir.mkdir()
        (self.vault_dir / 'tasks').mkdir()
        (self.vault_dir / '.state').mkdir()
        
        # Initialize vault
        subprocess.run(
            [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault_dir), 'init'],
            capture_output=True, text=True
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_sync_frontmatter_edge_cases(self):
        with self.assertRaises(ValueError):
            parse_frontmatter("---\ntitle:\n\t- value\n---")
        with self.assertRaises(ValueError):
            parse_frontmatter("---\nmetadata:\n\tkey: value\n---")
        with self.assertRaises(ValueError):
            parse_frontmatter("---\ntitle:\n\tvalue\n---")
        with self.assertRaises(ValueError):
            parse_frontmatter("---\nnested:\n  deep:\n    level: 3\n---")
        with self.assertRaises(ValueError):
            parse_frontmatter("---\ntitle: \"unclosed\n---")
            
        parsed, _ = parse_frontmatter("---\ntitle: \"A\\tB\"\n---")
        self.assertEqual(parsed['title'], "A\tB")

    def test_pack_context_extreme_budgets(self):
        records = [
            {'id': 'a1', 'source': 'notes/a.md', 'text': 'This is a normal sized sentence for testing.'},
            {'id': 'b1', 'source': 'notes/b.md', 'text': 'Short.'},
        ]
        
        with self.assertRaises(ValueError):
            pack_context(records, limit=2, budget_chars=-1)
            
        result = pack_context(records, limit=2, budget_chars=0)
        self.assertEqual(len(result['records']), 0)
        self.assertTrue(result['truncated'])
            
        result = pack_context(records, limit=2, budget_chars=1)
        self.assertEqual(len(result['records']), 0)
        self.assertTrue(result['truncated'])
        
        result = pack_context(records, limit=2, budget_chars=50)
        self.assertTrue(isinstance(result, dict))

    def test_turkish_characters(self):
        records = [
            {'id': 'turk1', 'source': 'notes/İstanbul.md', 'text': 'İstanbul ı İ ğ ş'},
        ]
        result = pack_context(records, limit=1, budget_chars=8000)
        self.assertEqual(len(result['records']), 1)
        
        records_surrogate = [
            {'id': 'surrogate1', 'source': 'notes/a.md', 'text': 'Emoji: \U0001F600'},
        ]
        result_surrogate = pack_context(records_surrogate, limit=1, budget_chars=8000)
        self.assertEqual(len(result_surrogate['records']), 1)
        
        text_with_i = "İstanbul"
        parsed, body = parse_frontmatter(f"---\ntitle: {text_with_i}\n---")
        self.assertEqual(parsed['title'], text_with_i)

    def test_date_boundary_transitions(self):
        instant = _receipt_instant("2024-02-29T23:59:59Z")
        self.assertIsNotNone(instant)
        instant = _receipt_instant("2024-02-30T10:00:00Z")
        self.assertIsNone(instant)
        instant = _receipt_instant("2024-12-32T10:00:00Z")
        self.assertIsNone(instant)

    def test_recent_receipts_edge_cases(self):
        self.db.execute('INSERT INTO receipts VALUES (?,?)', ('r1', '{invalid}'))
        self.db.execute('INSERT INTO receipts VALUES (?,?)', ('r2', '{"event_id": "r2", "summary": "test"'))
        self.db.execute('INSERT INTO receipts VALUES (?,?)', ('r3', '{"event_id": "r3", "summary": "no date"}'))
        
        result = recent_receipts(self.db, days=7, limit=20)
        self.assertEqual(result['undated_omitted'], 3)
        self.assertEqual(len(result['items']), 0)

    def test_malformed_tasks(self):
        task_data = {
            "source": "tasks/t1.md",
            "text": "This is a task body.",
            "metadata": {
                "id": "t1",
                "status": "inbox",
                "owner": "jules",
                "project": "test"
            }
        }
        task_file = self.temp_dir / 'task.json'
        task_file.write_text(json.dumps(task_data))

        res = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault_dir), 
             'task-create', '--file', str(task_file)],
            capture_output=True, text=True
        )
        self.assertEqual(res.returncode, 0)
        
        task_path = self.vault_dir / 'tasks/t1.md'
        self.assertTrue(task_path.exists())
        
        content = task_path.read_text(encoding='utf-8')
        content += "\n- [ ] Missing date task with trailing whitespace   \n"
        content += "\n- [x] Done missing date task\n"
        task_path.write_text(content, encoding='utf-8')
        
        res = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault_dir), 
             'sync'],
            capture_output=True, text=True
        )
        self.assertEqual(res.returncode, 0)

    def test_mixed_path_separators(self):
        records = [
            {'id': 'a1', 'source': 'notes\\a.md', 'text': 'Mixed path separators.'},
            {'id': 'b1', 'source': 'notes/b\\c.md', 'text': 'Short.'},
        ]
        
        result = pack_context(records, limit=2, budget_chars=8000)
        self.assertEqual(len(result['records']), 2)

if __name__ == '__main__':
    unittest.main()
