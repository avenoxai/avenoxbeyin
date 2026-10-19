import json
import unittest
from pathlib import Path
import tempfile
import sys
import io
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_projects as projects

class Issue397Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.valid_folder = self.vault / 'valid_folder'
        self.valid_folder.mkdir()
        
    def tearDown(self):
        self.tmp.cleanup()

    def test_malformed_scope_folder_does_not_crash_runtime(self):
        scopes_path = self.vault / runtime.PROJECT_SCOPES_FILE
        scopes_data = {
            "folders": {
                "../escape_vault": "evil_project",
                "/absolute/path": "bad_project",
                "valid_folder": "good_project",
                "missing_folder": "missing_project"
            },
            "shared_unscoped": False
        }
        scopes_path.write_text(json.dumps(scopes_data), encoding='utf-8')
        
        captured_err = io.StringIO()
        try:
            with patch('sys.stderr', captured_err):
                scopes = runtime.read_project_scopes(self.vault)
                
            self.assertIn("valid_folder", dict(scopes["folders"]).keys())
            # "missing_folder" is valid scope path, but doesn't exist, it should not be filtered out by _scope_path ValueError handling, 
            # instead the read_project_scopes logic already handles it (keeps it and prints "Warning: project scope folder not found in the vault: ...")
            self.assertIn("missing_folder", dict(scopes["folders"]).keys()) 
            
            # but "../escape_vault" should be skipped because _scope_path raises a ValueError
            self.assertNotIn("../escape_vault", dict(scopes["folders"]).keys())
            
            err_output = captured_err.getvalue()
            self.assertIn("Warning", err_output)
            self.assertIn("escape_vault", err_output)
        except Exception as e:
            self.fail(f"runtime.read_project_scopes crashed: {e}")

    def test_malformed_scope_folder_does_not_crash_projects(self):
        scopes_path = self.vault / projects.PROJECT_SCOPES_FILE
        scopes_data = {
            "folders": {
                "../escape_vault": "evil_project",
                "/absolute/path": "bad_project",
                "valid_folder": "good_project",
                "missing_folder": "missing_project"
            },
            "shared_unscoped": False
        }
        scopes_path.write_text(json.dumps(scopes_data), encoding='utf-8')
        
        captured_err = io.StringIO()
        try:
            with patch('sys.stderr', captured_err):
                scopes = projects.read_project_scopes(self.vault)
                
            self.assertIn("valid_folder", dict(scopes["folders"]).keys())
            self.assertIn("missing_folder", dict(scopes["folders"]).keys()) 
            self.assertNotIn("../escape_vault", dict(scopes["folders"]).keys())
            
            err_output = captured_err.getvalue()
            self.assertIn("Warning", err_output)
            self.assertIn("escape_vault", err_output)
        except Exception as e:
            self.fail(f"projects.read_project_scopes crashed: {e}")

if __name__ == '__main__':
    unittest.main()
