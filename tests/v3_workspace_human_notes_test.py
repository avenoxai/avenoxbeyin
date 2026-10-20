"""Project identity signals never create workspace authority."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from v3_package_helpers import inherited_env
from v3_workspace_helpers import install_fixture
from beyin_v3_projects import resolve_rules_project, resolve_project_from_query
from beyin_v3_workspace_context import bound_project_for_cwd, request_scope
from beyin_v3_sync import SyncEngine
from beyin_v3_workspaces import WorkspaceError
from beyin_v3_bridge import origin
from beyin_v3 import render_context

class WorkspaceHumanNotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve()
        self.runtime,self.spec,self.request=install_fixture(self.base,'academic')
        self.vault=self.runtime.registry.vault;self.state=self.runtime.registry.state

    def test_human_read_edit_scope_and_metadata_guard(self):
        self.runtime.activate();engine=SyncEngine(self.vault,self.state)
        for source,extra in [('notes/allowed.md',{}),('notes/private.md',{'visibility':'private'}),('notes/foreign.md',{'workspace_id':'foreign'}),('notes/untrusted.md',{'trust':'untrusted'}),('notes/unscoped.md',{'workspace_id':None})]:
            meta={'id':source,'kind':'note','visibility':'internal','workspace_id':'academic','remote_allowed':False};meta.update(extra)
            (self.vault/source).write_text('---\n'+json.dumps(meta)+'\n---\nBefore\n',encoding='utf-8')
        engine.sync()
        with self.assertRaises(WorkspaceError):engine.note_read('notes/allowed.md')
        with request_scope('academic','academic-user'):
            for name in ('private','foreign','untrusted','unscoped'):
                source='notes/'+name+'.md'
                with self.assertRaises(WorkspaceError):engine.note_read(source)
                raw=(self.vault/source).read_bytes()
                with self.assertRaises(WorkspaceError):engine.note_edit({'source':source,'expected_sha256':hashlib.sha256(raw).hexdigest(),'edits':[{'old':'Before','new':'BYPASS'}]})
                self.assertEqual((self.vault/source).read_bytes(),raw)
            with self.assertRaises(WorkspaceError):engine.note_edit({'source':'notes/new.md','expected_sha256':None,'content':'unscoped new note'})
            self.assertFalse((self.vault/'notes/new.md').exists())
            before=engine.note_read('notes/allowed.md')
            request={'source':'notes/allowed.md','expected_sha256':before['sha256'],'edits':[{'old':'Before','new':'After'}]}
            self.assertEqual(engine.note_edit(request)['status'],'written')
            before=engine.note_read('notes/allowed.md')
            with self.assertRaises(WorkspaceError):engine.note_edit({'source':'notes/allowed.md','expected_sha256':before['sha256'],'edits':[{'old':'"academic"','new':'"foreign"'}]})
            with self.assertRaises(WorkspaceError):engine.note_edit({'source':'notes/allowed.md','expected_sha256':before['sha256'],'edits':[{'old':'\"remote_allowed\": false','new':'\"remote_allowed\": true'}]})
        with request_scope('academic','revoked'):
            with self.assertRaises(WorkspaceError):engine.note_read('notes/allowed.md')
            with self.assertRaises(WorkspaceError):engine.note_edit({'source':'notes/allowed.md','expected_sha256':before['sha256'],'edits':[{'old':'After','new':'REVOKED'}]})

