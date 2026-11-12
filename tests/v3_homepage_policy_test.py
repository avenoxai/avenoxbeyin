"""A derived page retains its source policy after the source becomes restricted."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

import v3_homepage_boundaries_test as fixtures

PROBE = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from beyin_v3_sync import SyncEngine
from beyin_v3_jev import remote_allowed, advise_context, auto_context
from unittest.mock import patch
engine = SyncEngine(Path(sys.argv[2]), Path(sys.argv[3]))
store = engine.store
records = store.candidates('Record Value', project=sys.argv[4], limit=30)
sent, automatic = [], []
def capture(store, query, cards, *args, source_guard=None):
    sent.extend(cards)
    return dict(mode='shadow', degraded=False)
def capture_auto(state, query, cards, **kwargs):
    automatic.extend(cards)
    return dict(mode='shadow', degraded=True, diagnostics=[], scores={})
with patch('beyin_v3_jev.client.inspect_config', return_value=dict(valid=True, mode='shadow', features=['context', 'auto_context'])):
    with patch('beyin_v3_jev._packaged', capture):
        advise_context(store, 'Record Value', project=sys.argv[4])
    with patch('beyin_v3_jev.client.evaluate', capture_auto):
        context = store.context_for('codex', 'Record Value details', project=sys.argv[4], strict=True)
        auto_context(store, 'codex', 'Record Value details', context, project=sys.argv[4], budget_chars=8000)
snapshot = store.source_snapshot(['Page.md'], budget_chars=10000)
with store._connect() as db:
    indexed = [json.loads(row[0]) for row in db.execute('SELECT payload FROM records')]
print(json.dumps(dict(local=[r['source'] for r in records],
    remote=[r['source'] for r in records if remote_allowed(r)], sent=sent, automatic=automatic,
    snapshot=[r['source'] for r in snapshot['records']],
    indexed={r['source']:dict(remote_allowed=remote_allowed(r), trusted=r.get('trusted')) for r in indexed})))
'''

DISPATCH = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from beyin_v3_sync import SyncEngine, parse, render
from beyin_v3_jev import advise_context, auto_context, review_candidate, verify_answer
import beyin_v3_jev_client as client
from unittest.mock import patch
engine = SyncEngine(Path(sys.argv[2]), Path(sys.argv[3]))
store = engine.store
os.environ['TYPESAFE_API_KEY'] = 'synthetic-test-key'
client.set_mode(store.state_dir, 'shadow', enable=['context', 'auto_context', 'review', 'answer'])
kind, trigger = sys.argv[4:6]
page = next(r for r in store._records() if r['source'] == 'notes/home/Page.md')
evidence = [dict(record_id=page['id'], source_sha256=page['source_sha256'], quote='ZirconConfidentialCanary')]
sent, narrowed = [], []
builder = '_context_body' if kind == 'auto' else '_answer_body' if kind == 'answer' else '_score_body'
original = getattr(client, builder)
def narrow():
    if narrowed: return
    narrowed.append(True)
    path = store.vault_root / 'notes/source/Record.md'
    metadata, body = parse(path.read_text(encoding='utf-8'))
    metadata['remote_allowed'] = False
    path.write_text(render(metadata, body), encoding='utf-8')
def build(*args, **kwargs):
    body = original(*args, **kwargs)
    if trigger == 'serialize': narrow()
    return body
bounded = client._bounded_transport
def queued(sender, *args):
    if trigger == 'dispatch': narrow()
    return bounded(sender, *args)
def transport(url, body, key, timeout):
    sent.append(body)
    return dict(answers={})
with patch.object(client, builder, build), patch.object(client, '_bounded_transport', queued):
    if kind == 'manual': advise_context(store, 'Record Value details', project='chosen', transport=transport)
    elif kind == 'auto':
        context = store.context_for('codex', 'Record Value details', project='chosen', strict=True)
        auto_context(store, 'codex', 'Record Value details', context, project='chosen', budget_chars=8000, transport=transport)
    elif kind == 'review':
        review_candidate(store, dict(status='proposed', project='chosen', claim='Record has a value.', evidence=evidence), project='chosen', transport=transport)
    else:
        verify_answer(store, [dict(text='Record has a value.', citations=evidence)], project='chosen', transport=transport)
assert narrowed, 'the requested serialization/dispatch boundary was not reached'
assert not sent, 'restricted source content reached provider transport'
print(json.dumps(dict(status='passed', kind=kind, trigger=trigger, outbound_calls=len(sent))))
'''


class HomepagePolicyTest(unittest.TestCase):
    installed = False

    def fixture(self, project='chosen'):
        fixture = fixtures.HomepageBoundariesTest()
        fixture.installed = self.installed
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.reset(project=project)
        fixture.change(fixture.source, facts={'value': 'ZirconConfidentialCanary'})
        fixture.create()
        home = fixture.vault / fixture.page
        home.write_bytes(home.read_bytes() + b'\nMy human navigation and latest personal note.\n')
        fixture.cli('sync')
        return fixture

    def test_removed_selected_fact_withholds_old_copy_until_restored(self):
        fixture = self.fixture()
        before = (fixture.vault / fixture.page).read_bytes()
        fixture.change(fixture.source, facts={})
        fixture.cli('sync')
        self.assert_restricted(fixture, local=False, indexed=True)
        self.assertEqual(before, (fixture.vault / fixture.page).read_bytes())
        fixture.change(fixture.source, facts={'value': 'ZirconConfidentialCanary'})
        fixture.cli('sync')
        self.assertIn(fixture.page, self.probe(fixture)['local'])

    def test_retired_source_withholds_copy_and_prevents_back_write(self):
        fixture = self.fixture()
        fixture.change(fixture.source, status='retired')
        home = fixture.vault / fixture.page
        home.write_text(home.read_text(encoding='utf-8').replace(
            '-->ZirconConfidentialCanary<!--', '-->EditedCanary<!--'), encoding='utf-8')
        before = fixture.snapshot()
        fixture.cli('sync')
        self.assert_restricted(fixture, local=False, indexed=True)
        self.assertEqual(before, fixture.snapshot())

    def probe(self, fixture, project='chosen'):
        scripts = fixture.vault / '.claude/scripts' if self.installed else fixtures.ROOT / 'template/.claude/scripts'
        run = subprocess.run([sys.executable, '-c', PROBE, str(scripts), str(fixture.vault),
                              str(fixture.state), project], env=fixture.env,
                             capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def assert_restricted(self, fixture, *, local, project='chosen', indexed=False):
        result = self.probe(fixture, project)
        self.assertEqual(fixture.page in result['local'], local)
        self.assertNotIn(fixture.page, result['remote'])
        self.assertNotIn('ZirconConfidentialCanary', json.dumps(result['sent']))
        self.assertNotIn('ZirconConfidentialCanary', json.dumps(result['automatic']))
        self.assertEqual(fixture.page in result['snapshot'], local)
        if indexed:
            self.assertFalse(result['indexed'][fixture.page]['remote_allowed'])

    def test_source_policy_narrowing_restricts_existing_copy_before_and_after_sync(self):
        for policy in ({'remote_allowed': False}, {'visibility': 'private'},
                       *fixtures.UNTRUSTED, {'project': 'other'}):
            with self.subTest(policy=policy):
                fixture = self.fixture()
                self.assertIn(fixture.page, self.probe(fixture)['remote'])
                before = (fixture.vault / fixture.page).read_bytes()
                fixture.change(fixture.source, **policy)
                # Before indexing, the changed dependency is stale: withhold the page.
                self.assert_restricted(fixture, local=False)
                fixture.cli('sync')
                self.assert_restricted(fixture, local=policy == {'remote_allowed': False}, indexed=True)
                self.assertEqual(before, (fixture.vault / fixture.page).read_bytes())
                fixture.write(fixture.source, {'id': 'record', 'kind': 'note', 'project': 'chosen',
                              'facts': {'value': 'ZirconConfidentialCanary'}}, '# Record\n')
                fixture.cli('sync')
                self.assertIn(fixture.page, self.probe(fixture)['remote'])
                self.assertEqual(before, (fixture.vault / fixture.page).read_bytes())

    def test_folder_scope_change_and_block_withhold_existing_copy(self):
        for folders in ({'notes/home': 'alpha', 'notes/source': 'beta'},
                        {'notes': 'alpha', 'notes/source': 'alpha', 'notes/source/': 'beta'}):
            with self.subTest(folders=folders):
                fixture = self.fixture(project=None)
                fixture.scopes({'notes/home': 'alpha', 'notes/source': 'alpha'})
                fixture.cli('sync')
                before = (fixture.vault / fixture.page).read_bytes()
                self.assertIn(fixture.page, self.probe(fixture, 'alpha')['remote'])
                fixture.scopes(folders)
                self.assert_restricted(fixture, local=False, project='alpha')
                fixture.cli('sync')
                self.assert_restricted(fixture, local=False, project='alpha', indexed=True)
                self.assertEqual(before, (fixture.vault / fixture.page).read_bytes())

    def test_policy_change_during_serialization_or_dispatch_blocks_every_remote_path(self):
        for kind in ('manual', 'auto', 'review', 'answer'):
            for trigger in ('serialize', 'dispatch'):
                with self.subTest(kind=kind, trigger=trigger):
                    fixture = self.fixture()
                    scripts = fixture.vault / '.claude/scripts' if self.installed else fixtures.ROOT / 'template/.claude/scripts'
                    run = subprocess.run([sys.executable, '-c', DISPATCH, str(scripts), str(fixture.vault),
                                          str(fixture.state), kind, trigger], env=fixture.env,
                                         capture_output=True, text=True, encoding='utf-8', timeout=30)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    self.assertEqual(json.loads(run.stdout)['outbound_calls'], 0)

    def test_local_only_page_and_derived_source_cannot_promote_information(self):
        fixture = self.fixture()
        fixture.change(fixture.page, remote_allowed=False)
        fixture.cli('sync')
        self.assert_restricted(fixture, local=True, indexed=True)
        fixture.change(fixture.page, remote_allowed=True, facts={'value': 'AnotherCanary'})
        fixture.request.update(source='notes/home/Second.md', expected_sha256=None)
        fixture.request['selections'][0]['source'] = fixture.page
        before = fixture.snapshot()
        fixture.create(ok=False)
        self.assertEqual(before, fixture.snapshot())


class InstalledHomepagePolicyTest(HomepagePolicyTest):
    installed = True


if __name__ == '__main__':
    unittest.main()
