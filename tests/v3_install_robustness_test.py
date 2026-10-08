import errno
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from v3_package_helpers import ROOT, isolated_env

class InstallRobustnessTest(unittest.TestCase):
    def test_permission_error_on_replace_install(self):
        with tempfile.TemporaryDirectory() as home:
            env = isolated_env(home)
            vault = Path(home) / "vault"
            vault.mkdir()
            
            wrapper_script = Path(home) / "wrapper.py"
            wrapper_script.write_text(f"""
import sys
import os
import stat
import tempfile
from unittest.mock import patch
from pathlib import Path

original_replace = os.replace
original_unlink = os.unlink

def mock_replace(src, dst):
    if os.path.exists(dst) and not (os.stat(dst).st_mode & stat.S_IWRITE):
        raise PermissionError(13, 'Permission denied', str(dst))
    return original_replace(src, dst)

def mock_unlink(path):
    if os.path.exists(path) and not (os.stat(path).st_mode & stat.S_IWRITE):
        raise PermissionError(13, 'Permission denied', str(path))
    return original_unlink(path)

sys.path.insert(0, r'{ROOT / "scripts"}')
import install_v3

target = Path(r'{vault / "dummy.txt"}')
target.write_text('old')
target.chmod(stat.S_IREAD)

with patch('os.replace', side_effect=mock_replace):
    try:
        install_v3.atomic(target, b'new')
        print("SUCCESS_REPLACE")
    except Exception as e:
        print("EXCEPTION_REPLACE:", type(e).__name__, str(e))
            """)
            
            result = subprocess.run([sys.executable, str(wrapper_script)], env=env, capture_output=True, text=True)
            self.assertIn("SUCCESS_REPLACE", result.stdout)
            self.assertNotIn("EXCEPTION_REPLACE", result.stdout)

    def test_path_too_long(self):
        with tempfile.TemporaryDirectory() as home:
            env = isolated_env(home)
            vault = Path(home) / "vault"
            vault.mkdir()
            
            wrapper_script = Path(home) / "wrapper.py"
            wrapper_script.write_text(f"""
import sys
import os
import errno
from unittest.mock import patch
from pathlib import Path

original_mkdir = Path.mkdir

def mock_mkdir(self, *args, **kwargs):
    if len(str(self)) > 260:
        raise OSError(errno.ENAMETOOLONG, "File name too long")
    return original_mkdir(self, *args, **kwargs)

sys.path.insert(0, r'{ROOT / "scripts"}')
import install_v3

target = Path(r'{vault}') / ("A" * 300) / "dummy.txt"

with patch.object(Path, 'mkdir', new=mock_mkdir):
    try:
        install_v3.atomic(target, b'new')
    except Exception as e:
        print("EXCEPTION_LONG_PATH:", type(e).__name__, str(e))
            """)
            
            result = subprocess.run([sys.executable, str(wrapper_script)], env=env, capture_output=True, text=True)
            self.assertIn("Path limit exceeded:", result.stdout)

if __name__ == '__main__':
    unittest.main()
