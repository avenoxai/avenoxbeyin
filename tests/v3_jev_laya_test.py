"""Optional local Laya provider: config, isolation, request split, merge and fail-closed paths.

Offline: every server here is a fake laya-serve bound to 127.0.0.1:0 in a thread. It returns
Laya-shaped answers (4 dp probabilities, entropy `confidence`, `answer_confidence`, `routing`),
so these tests say nothing about model quality.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from v3_package_helpers import windows_runtime_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / 'extensions/laya'))
import beyin_v3 as runtime
import beyin_v3_jev as advisor
import beyin_v3_jev_client as client
import beyin_v3_jev_contracts as contracts
import beyin_v3_laya as laya
from beyin_v3_memory_assessment import assess_memory

TYPESAFE_KEY = 'MARKERTYPESAFEKEY'
LAYA_KEY = 'MARKERLAYAKEY'
SECRET = 'sk-' + 'a' * 40
PROXY_NAMES = ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy')


def closed_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class FakeLaya:
    """A loopback stand-in for laya-serve. Records every request; behaviour is set per test."""

    def __init__(self, test):
        self.hits, self.gets, self.status = [], [], {}
        self.routing, self.tokens, self.delay, self.wait = None, 50, 0, None
        self.noul = lambda state: 0.9 if 'RELEVANT' in json.dumps(state, ensure_ascii=False) else 0.1
        self.active = self.peak = 0
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.gets.append(dict(path=self.path, headers=dict(self.headers)))
                self.reply(200, dict(status='ok', loaded=['english', 'multilingual', 'Bad Name'], device='mps'))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                with outer.lock:
                    index = len(outer.hits)
                    outer.hits.append(dict(path=self.path, headers=dict(self.headers), body=body))
                    outer.active += 1
                    outer.peak = max(outer.peak, outer.active)
                try:
                    if outer.wait is not None:
                        outer.wait.wait(10)
                    time.sleep(outer.delay)
                    code = outer.status.get(index, 200)
                    self.reply(code, outer.answer(body) if code == 200 else dict(detail='PRIVATE server detail'))
                finally:
                    with outer.lock:
                        outer.active -= 1

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def answer(self, body):
        answers = {}
        for ident, question in body['questions'].items():
            action = dict(act_probability=1.0)
            if question['type'] == 'noul':
                value = round(self.noul(body['state']), 4)
                answers[ident] = dict(type='noul', noul=value, confidence=max(value, 1 - value),
                                      answer_confidence=max(value, 1 - value), action=action)
            elif question['type'] == 'score':
                answers[ident] = dict(type='score', score=1.6, probabilities={'0': 0.1, '1': 0.2, '2': 0.7},
                                      legend={str(i): c for i, c in enumerate(question['criteria'])},
                                      confidence=0.2345, answer_confidence=0.7, action=action)
            else:
                keys = list(question['criteria'])
                rest = round(0.2 / (len(keys) - 1), 4)
                # Laya's `confidence` on a choice is normalized entropy, deliberately not max(p).
                answers[ident] = dict(type='choice', choice=keys[0], confidence=0.1234, answer_confidence=0.8,
                                      probabilities={k: 0.8 if k == keys[0] else rest for k in keys}, action=action)
        return dict(model='laya-rl-agent', answers=answers, usage=dict(input_tokens=self.tokens, output_tokens=0),
                    routing=dict(model=self.routing or body.get('model'), repo='fake', reason='explicit'))

    def bodies(self):
        return [hit['body'] for hit in self.hits]


class LayaCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / 'state'
        self.laya = FakeLaya(self)
        self.environ({'TYPESAFE_API_KEY': TYPESAFE_KEY, 'TYPESAFE_BASE_URL': 'https://marker.invalid'})

    def environ(self, values):
        patcher = patch.dict(os.environ, {**windows_runtime_env(), **values}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, mode='shadow', url=None, **kw):
        return client.set_mode(self.state, mode, provider='laya', laya=dict(base_url=url or self.laya.url), **kw)

    def hand_edit(self, **changes):
        """What a user typing into jev.json gets: set_mode refuses `on` for Laya, the file cannot."""
        path = self.state / 'jev.json'
        path.write_text(json.dumps(dict(json.loads(path.read_text(encoding='utf-8')), **changes)), encoding='utf-8')

    def cards(self, count=3):
        return [dict(id='c%d' % i, title='Kart %d' % i, statement=('RELEVANT ' if i == 0 else '') + 'MARKERCARD%d not metni.' % i,
                     scope='project:demo', domains=['note']) for i in range(count)]

    def items(self, count=2):
        return [dict(id='k%d' % i, claim='Iddia %d.' % i, quotes=['alinti %d' % i], context=['Kaynak MARKERITEM%d metni.' % i])
                for i in range(count)]

    def notes(self, count=3):
        return [dict(id='k%d' % i, title='Not %d' % i, excerpt=('RELEVANT ' if i == 1 else '') + 'MARKERNOTE%d kisa metin.' % i,
                     project='demo', status='active', updated_at='2026-09-20') for i in range(count)]

    def memory_item(self, priors=2):
        return [dict(claim='Kisa notlar tercih ediliyor.', project='demo',
                     evidence=[dict(quote='kisa notlar', context='Demo icin kisa notlar kullanalim.', updated_at='2026-09-20')],
                     prior=[dict(statement='Eski karar %d.' % i, updated_at='2026-09-01') for i in range(priors)])]

    def cache_files(self):
        return list((self.state / '.cache/jev').glob('*.json')) if (self.state / '.cache/jev').exists() else []


class ConfigTest(LayaCase):
    def test_laya_block_is_written_with_the_pinned_defaults(self):
        status = client.set_mode(self.state, 'shadow', provider='laya')
        written = json.loads((self.state / 'jev.json').read_text(encoding='utf-8'))
        self.assertEqual(written['provider'], 'laya')
        self.assertEqual(written['laya'], dict(base_url='http://127.0.0.1:8765', model='multilingual'))
        self.assertEqual(status['laya'], dict(base_url='http://127.0.0.1:8765', model='multilingual', timeout=8.0))
        self.assertEqual((status['provider'], status['key_required'], status['endpoint_local']), ('laya', False, True))
        self.assertEqual(status['calibration'], 'jev_thresholds_unverified_for_laya')
        self.assertIs(status['auto_context_applied'], False)

    def test_block_validation_rejects_every_non_loopback_or_unknown_value(self):
        bad = [dict(base_url='http://example.com'), dict(base_url='https://api.typesafe.ai'),
               dict(base_url='http://localhost:8765'), dict(base_url='http://user:pw@127.0.0.1:8765'),
               dict(base_url='http://127.0.0.1:8765?x=1'), dict(base_url='http://127.0.0.1:99999'),
               dict(base_url='http://127.0.0.1:8765/other'), dict(base_url=8765), dict(model='jev-1.13.0'),
               dict(model='typed-decisions'), dict(timeout=11), dict(timeout=0), dict(timeout=float('nan')),
               dict(local_only_notes=True), dict(api_key='x')]
        self.state.mkdir()
        for block in bad:
            with self.subTest(block=block):
                (self.state / 'jev.json').write_text(json.dumps(dict(mode='on', provider='laya', laya=block)), encoding='utf-8')
                self.assertFalse(client.inspect_config(self.state)['valid'])
        for block in ([], 'x'):
            (self.state / 'jev.json').write_text(json.dumps(dict(mode='on', laya=block)), encoding='utf-8')
            self.assertFalse(client.inspect_config(self.state)['valid'])
        for block in (dict(base_url='http://[::1]:9000/'), dict(base_url='https://127.0.0.1:9000/v1', model='english', timeout=2)):
            (self.state / 'jev.json').write_text(json.dumps(dict(mode='on', provider='laya', laya=block)), encoding='utf-8')
            self.assertTrue(client.inspect_config(self.state)['valid'], block)

    def test_cli_options_are_checked_and_need_the_laya_provider(self):
        with self.assertRaisesRegex(ValueError, 'laya_option_invalid'):
            client.set_mode(self.state, 'shadow', provider='laya', laya=dict(base_url='http://example.com'))
        self.assertFalse((self.state / 'jev.json').exists())
        with self.assertRaisesRegex(ValueError, 'laya_option_requires_laya_provider'):
            client.set_mode(self.state, 'on', laya=dict(model='english'))
        with self.assertRaisesRegex(ValueError, 'provider_unknown'):
            client.set_mode(self.state, 'on', provider='other')

    def test_switching_back_to_typesafe_keeps_the_block_and_restores_jev(self):
        self.config()
        client.set_mode(self.state, 'shadow', laya=dict(model='english'))
        switched = client.set_mode(self.state, 'on', provider='typesafe')
        written = json.loads((self.state / 'jev.json').read_text(encoding='utf-8'))
        self.assertEqual(written['laya'], dict(base_url=self.laya.url, model='english'))
        config = client.load_config(self.state)
        self.assertEqual((config['base_url'], config['model'], config['timeout']), ('https://api.typesafe.ai', 'jev-1.13.0', 3.0))
        self.assertEqual(switched['provider'], 'typesafe')
        self.assertNotIn('laya', switched)
        self.assertEqual(client.set_mode(self.state, 'shadow', provider='laya')['laya']['model'], 'english')

    def test_threshold_table_has_a_row_for_every_checkpoint(self):
        for model in laya.CHECKPOINTS:
            row = client.thresholds('laya', model)
            self.assertIs(row['verified'], False)
            self.assertEqual(set(row), {'gate', 'keep', 'rescue', 'confidence', 'verified'})
        self.assertIs(client.thresholds('typesafe', 'jev-1.13.0')['verified'], True)
        self.assertEqual(client.thresholds('vercel', 'x'), client.THRESHOLDS[('typesafe', '*')])
        self.assertEqual((advisor.AUTO_GATE, advisor.AUTO_KEEP, advisor.AUTO_RESCUE, advisor.CONFIDENCE_GATE), (0.25, 0.4, 0.6, 0.8))


class ShadowOnlyTest(LayaCase):
    def test_saving_on_with_laya_is_refused_on_every_path(self):
        with self.assertRaisesRegex(ValueError, 'laya_shadow_only'):
            client.set_mode(self.state, 'on', provider='laya')
        with self.assertRaisesRegex(ValueError, 'laya_shadow_only'):
            client.set_mode(self.state, 'on', provider='laya', laya=dict(model='english'), enable=['auto_context'])
        self.assertFalse((self.state / 'jev.json').exists())
        self.config()
        before = (self.state / 'jev.json').read_bytes()
        for arguments in (dict(mode='on'), dict(mode='on', enable=['auto_context']), dict(mode='on', laya=dict(model='english'))):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, 'laya_shadow_only'):
                client.set_mode(self.state, **arguments)
        self.assertEqual((self.state / 'jev.json').read_bytes(), before)
        # A saved typesafe `on` cannot be carried over to laya either.
        client.set_mode(self.state, 'on', provider='typesafe')
        before = (self.state / 'jev.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'laya_shadow_only'):
            client.set_mode(self.state, provider='laya')
        self.assertEqual((self.state / 'jev.json').read_bytes(), before)
        self.assertEqual(client.set_mode(self.state, 'shadow', provider='laya')['mode'], 'shadow')
        self.assertEqual(client.set_mode(self.state, 'on', provider='typesafe')['mode'], 'on')
        self.assertEqual(client.set_mode(self.state, 'off', provider='laya')['mode'], 'off')

    def test_a_hand_edited_on_runs_as_shadow_and_status_says_so(self):
        self.config(enable=['auto_context'])
        self.hand_edit(mode='on')
        self.assertEqual(client.load_config(self.state)['mode'], 'shadow')
        self.assertEqual(client.inspect_config(self.state)['mode'], 'shadow')
        status = client.status(self.state)
        self.assertEqual((status['mode'], status['saved_mode'], status['config_valid']), ('shadow', 'on', True))
        self.assertIs(status['shadow_only'], True)
        self.assertEqual(status['mode_refused'], 'laya_shadow_only')
        self.assertIs(status['auto_context_applied'], False)
        advice = client.evaluate(self.state, '', self.items(1), purpose='answer_check')
        self.assertFalse(advice['degraded'], advice['diagnostics'])
        self.assertEqual(advice['mode'], 'shadow')
        self.assertIs(advice['confidence_provenance']['used_for_selection'], False)
        # Shadow-only is independent of the threshold table: a measured row changes nothing.
        with patch.dict(client.THRESHOLDS, {('laya', 'multilingual'): dict(client.THRESHOLDS[('laya', 'multilingual')], verified=True)}):
            self.assertEqual(client.inspect_config(self.state)['mode'], 'shadow')
            self.assertIs(client.status(self.state)['auto_context_applied'], False)
        (self.state / 'jev.disabled').write_text('', encoding='utf-8')
        self.assertEqual(client.status(self.state)['mode'], 'off')
        (self.state / 'jev.disabled').unlink()
        self.hand_edit(mode='shadow')
        self.assertNotIn('mode_refused', client.status(self.state))
        self.hand_edit(mode='on', provider='typesafe')
        typesafe = client.status(self.state)
        self.assertEqual(typesafe['mode'], 'on')
        self.assertNotIn('shadow_only', typesafe)


class IsolationTest(LayaCase):
    def test_typesafe_key_and_url_never_reach_laya(self):
        self.config()
        advice = client.evaluate(self.state, 'MARKERQUERY', self.cards(1))
        self.assertFalse(advice['degraded'], advice['diagnostics'])
        self.assertEqual(len(self.laya.hits), 1)
        hit = self.laya.hits[0]
        self.assertEqual(hit['path'], '/v1/systemone')
        self.assertNotIn('Authorization', hit['headers'])
        raw = json.dumps(self.laya.hits)
        self.assertNotIn(TYPESAFE_KEY, raw)
        self.assertNotIn('marker.invalid', raw)

    def test_laya_api_key_is_sent_as_bearer_and_never_shown(self):
        self.environ({'TYPESAFE_API_KEY': TYPESAFE_KEY, 'LAYA_API_KEY': LAYA_KEY})
        self.config()
        client.evaluate(self.state, 'MARKERQUERY', self.cards(2))
        self.assertEqual([hit['headers'].get('Authorization') for hit in self.laya.hits], ['Bearer ' + LAYA_KEY] * 2)
        status = client.status(self.state)
        self.assertIs(status['key_present'], True)
        for key in (LAYA_KEY, TYPESAFE_KEY):
            self.assertNotIn(key, json.dumps(status))
            self.assertNotIn(key, (self.state / 'jev-calls.jsonl').read_text(encoding='utf-8'))

    def test_environment_proxies_are_never_used(self):
        proxy = FakeLaya(self)
        self.environ({name: proxy.url for name in PROXY_NAMES})
        self.config()
        advice = client.evaluate(self.state, 'MARKERQUERY', self.cards(2))
        self.assertFalse(advice['degraded'], advice['diagnostics'])
        self.assertTrue(client.probe(self.state)['reachable'])
        self.assertEqual((len(self.laya.hits), len(self.laya.gets)), (2, 1))
        self.assertEqual((proxy.hits, proxy.gets), ([], []))


class SplitTest(LayaCase):
    def plan(self, purpose, body, model='multilingual'):
        return laya.plan(purpose, body, model)

    def check_questions(self, requests, template, criteria=None):
        for sub, _ in requests:
            self.assertEqual(sub['model'], 'multilingual')
            self.assertEqual(list(sub['questions']), ['q'])
            question = sub['questions']['q']
            self.assertEqual((question['type'], question['instructions']), laya.QUESTIONS[template])
            self.assertLessEqual(len(question['instructions']), 160)
            self.assertEqual(question.get('criteria'), criteria)

    def test_auto_context_is_one_topical_request_plus_one_per_note(self):
        body = client._context_body(dict(model='multilingual'), 'MARKERPROMPT deploy nasil yapilir', self.notes(3))
        requests = self.plan('auto_context', body)
        self.assertEqual([outer for _, outer in requests], ['topical', 'k0', 'k1', 'k2'])
        self.assertEqual(requests[0][0]['state'], dict(request='MARKERPROMPT deploy nasil yapilir'))
        for i, (sub, _) in enumerate(requests[1:]):
            raw = json.dumps(sub['state'])
            self.assertEqual([m for m in ('MARKERNOTE0', 'MARKERNOTE1', 'MARKERNOTE2') if m in raw], ['MARKERNOTE%d' % i])
            self.assertIn('MARKERPROMPT', raw)
        self.check_questions(requests[:1], 'topical')
        self.check_questions(requests[1:], 'note')

    def test_retrieval_is_one_request_per_candidate_and_facet(self):
        cards = self.cards(3)
        body = dict(model='multilingual', state=dict(query='q', facets=['q'], candidates=cards),
                    questions={'f0_c%d' % i: client._question('retrieval', i, 0) for i in range(3)})
        requests = self.plan('retrieval', body)
        self.assertEqual([outer for _, outer in requests], ['f0_c0', 'f0_c1', 'f0_c2'])
        self.check_questions(requests, 'retrieval', client.CRITERIA)
        for i, (sub, _) in enumerate(requests):
            self.assertEqual(sub['state']['candidate']['statement'], cards[i]['statement'])
            self.assertNotIn('id', sub['state']['candidate'])
        body['state']['facets'] = ['a', 'b']
        body['questions'] = {'f%d_c%d' % (j, i): client._question('retrieval', i, j) for j in range(2) for i in range(3)}
        requests = dict((outer, sub) for sub, outer in self.plan('retrieval', body))
        self.assertEqual(sorted(requests), sorted(body['questions']))
        self.assertEqual((requests['f0_c2']['state']['part'], requests['f1_c2']['state']['part']), ('a', 'b'))
        requests = [(sub, outer) for outer, sub in requests.items()]
        self.check_questions(requests, 'retrieval_part', client.CRITERIA)

    def test_reviews_answers_and_memory_keep_jev_criteria(self):
        card = self.cards(1)
        for purpose in ('evidence_review', 'memory_review'):
            body = dict(model='x', state=dict(query='claim', facets=['check'], candidates=card),
                        questions={'f0_c0': client._question(purpose, 0, 0)})
            requests = self.plan(purpose, body)
            self.assertEqual(requests[0][0]['state']['check'], 'check')
            self.check_questions(requests, purpose, client.REVIEW_CRITERIA)
        requests = self.plan('answer_check', client._answer_body(dict(model='x'), self.items(2)))
        self.assertEqual([outer for _, outer in requests], ['k0', 'k1'])
        self.assertIn('MARKERITEM1', json.dumps(requests[1][0]['state']))
        self.assertNotIn('MARKERITEM0', json.dumps(requests[1][0]['state']))
        self.check_questions(requests, 'relation', client.RELATIONS)
        requests = self.plan('memory_assessment', contracts.memory_body(dict(model='x'), self.memory_item(2)))
        self.assertEqual([outer for _, outer in requests], ['support', 'commitment', 'kind', 'relation_p0', 'relation_p1'])
        for (sub, outer), criteria in zip(requests, [contracts.SUPPORT, contracts.COMMITMENT, contracts.KIND, contracts.RELATION, contracts.RELATION]):
            self.assertEqual(sub['questions']['q']['criteria'], criteria)
        self.assertNotIn('prior', requests[0][0]['state'])
        self.assertEqual(requests[4][0]['state']['prior'], dict(statement='Eski karar 1.', updated_at='2026-09-01'))

    def test_only_the_prompt_share_is_trimmed_and_notes_never_are(self):
        long_prompt = 'MARKERPROMPT ' + 'uzun istem ' * 400
        body = client._context_body(dict(model='x'), long_prompt[:2000], self.notes(2))
        requests = self.plan('auto_context', body)
        for sub, _ in requests:
            self.assertLessEqual(laya._units(sub['state']), laya.STATE_UNITS['multilingual'])
            self.assertTrue(long_prompt.startswith(sub['state']['request']))
        self.assertEqual(requests[1][0]['state']['note'], body['state']['notes']['k0'])
        big = self.notes(1)
        big[0]['excerpt'] = 'x' * 2000
        with self.assertRaisesRegex(ValueError, 'laya_state_too_large'):
            self.plan('auto_context', client._context_body(dict(model='x'), 'kisa istem burada', big))
        with self.assertRaisesRegex(ValueError, 'config_invalid'):
            self.plan('auto_context', body, 'typed-decisions')

    def test_more_than_max_requests_is_refused(self):
        body = dict(model='x', state=dict(query='q', facets=['a', 'b', 'c'], candidates=self.cards(17)),
                    questions={'f%d_c%d' % (j, i): client._question('retrieval', i, j) for j in range(3) for i in range(17)})
        with self.assertRaisesRegex(ValueError, 'budget_exceeded'):
            self.plan('retrieval', body)
        body['state']['candidates'] = self.cards(16)
        body['questions'] = {k: v for k, v in body['questions'].items() if not k.endswith('_c16')}
        self.assertEqual(len(self.plan('retrieval', body)), laya.MAX_REQUESTS)

    def test_size_units_count_dense_text_heavier(self):
        self.assertEqual(laya.size_units('abc ışğ'), 7)
        self.assertEqual(laya.size_units('2026'), 8)
        self.assertEqual(laya.size_units('日'), 4)
        self.assertEqual(laya.size_units('🍋'), 5)


class MergeTest(LayaCase):
    def test_answers_merge_into_the_jev_shape(self):
        self.config()
        advice = client.evaluate(self.state, '', self.items(3), purpose='answer_check')
        self.assertFalse(advice['degraded'], advice['diagnostics'])
        self.assertEqual(len(self.laya.hits), 3)
        for relation in advice['relations'].values():
            # max(p), never the entropy value 0.1234 the server put in `confidence`.
            self.assertEqual((relation['choice'], relation['confidence']), ('supports', 0.8))
        self.assertEqual(advice['reported_model'], 'laya:multilingual')
        self.assertEqual(advice['usage'], dict(input_tokens=150, output_tokens=0))
        self.assertEqual(advice['confidence_provenance']['origin'], 'laya_max_probability')
        self.assertEqual((advice['network_requests'], advice['http_requests']), (1, 3))
        retrieval = client.evaluate(self.state, 'q', self.cards(2), facets=['a', 'b'])
        self.assertEqual(retrieval['scores'], {'c0': 1.6, 'c1': 1.6})
        self.assertEqual(retrieval['facet_scores'], {0: {'c0': 1.6, 'c1': 1.6}, 1: {'c0': 1.6, 'c1': 1.6}})
        rows = [json.loads(line) for line in (self.state / 'jev-calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(r['provider'], r['network_requests'], r['http_requests']) for r in rows], [('laya', 1, 3), ('laya', 1, 4)])

    def test_memory_assessment_merges_five_requests(self):
        self.config()
        advice = client.evaluate(self.state, '', self.memory_item(2), purpose='memory_assessment')
        self.assertFalse(advice['degraded'], advice['diagnostics'])
        self.assertEqual(sorted(advice['relations']), ['commitment', 'kind', 'relation_p0', 'relation_p1', 'support'])
        self.assertEqual(advice['relations']['kind']['choice'], 'task')
        self.assertEqual(len(self.laya.hits), 5)


class FailClosedTest(LayaCase):
    def degraded(self, advice, code):
        self.assertTrue(advice['degraded'])
        self.assertIn(code, advice['diagnostics'])
        self.assertEqual((advice['scores'], advice['relations']), ({}, {}))
        self.assertNotIn('PRIVATE', json.dumps(advice))

    def test_oversized_state_is_refused_before_any_request(self):
        self.config()
        cards = self.cards(2)
        cards[1]['statement'] = 'x' * 4000
        advice = client.evaluate(self.state, 'q', cards)
        self.degraded(advice, 'laya_state_too_large')
        self.assertEqual((self.laya.hits, advice['network_requests'], advice['http_requests']), ([], 0, 0))

    def test_too_many_requests_are_refused_before_any_request(self):
        self.config()
        advice = client.evaluate(self.state, 'q', self.cards(17), facets=['a', 'b', 'c'])
        self.degraded(advice, 'budget_exceeded')
        self.assertEqual(self.laya.hits, [])

    def test_closed_port_is_provider_unreachable(self):
        self.config(url='http://127.0.0.1:%d' % closed_port())
        self.degraded(client.evaluate(self.state, 'q', self.cards(1)), 'provider_unreachable')

    def test_http_errors_map_to_existing_codes(self):
        self.config()
        for status, code in ((401, 'http_unauthorized'), (413, 'http_error'), (422, 'http_error'), (500, 'http_server_error')):
            with self.subTest(status=status):
                self.laya.status = {len(self.laya.hits): status}
                self.degraded(client.evaluate(self.state, 'q' + str(status), self.cards(1)), code)

    def test_other_checkpoint_or_cut_row_is_discarded(self):
        self.config()
        self.laya.routing = 'english'
        self.degraded(client.evaluate(self.state, 'q1', self.cards(1)), 'laya_checkpoint_mismatch')
        self.laya.routing, self.laya.tokens = None, laya.MAX_TOKENS['multilingual']
        self.degraded(client.evaluate(self.state, 'q2', self.cards(1)), 'laya_state_truncated')
        self.assertEqual(self.cache_files(), [])

    def test_a_failure_on_request_k_of_n_discards_everything(self):
        self.config()
        self.laya.status = {2: 500}
        advice = client.evaluate(self.state, 'q', self.cards(5))
        self.degraded(advice, 'http_server_error')
        self.assertEqual((len(self.laya.hits), advice['http_requests']), (3, 3))
        self.assertEqual(self.cache_files(), [])

    def test_one_deadline_covers_the_whole_fan_out(self):
        self.config()
        self.laya.wait = threading.Event()
        self.addCleanup(self.laya.wait.set)
        started = time.monotonic()
        advice = client.evaluate(self.state, 'q', self.cards(5), timeout_cap=0.2)
        self.assertLess(time.monotonic() - started, 5)
        self.degraded(advice, 'deadline_exceeded')
        self.laya.wait.set()
        time.sleep(0.5)
        self.assertEqual(len(self.laya.hits), 1)  # no request after the deadline


class CacheTest(LayaCase):
    def test_providers_never_share_cache_and_laya_hits_are_free(self):
        client.set_mode(self.state, 'on')
        seen = []
        def typesafe(url, body, key, timeout):
            seen.append(body)
            return dict(answers={q: dict(type='score', score=0.5) for q in body['questions']})
        first = client.evaluate(self.state, 'MARKERQUERY', self.cards(2), source_versions={'a': '1'}, transport=typesafe)
        self.assertEqual(first['scores'], {'c0': 0.5, 'c1': 0.5})
        self.config()
        second = client.evaluate(self.state, 'MARKERQUERY', self.cards(2), source_versions={'a': '1'})
        self.assertFalse(second['cache_hit'])
        self.assertEqual(second['scores'], {'c0': 1.6, 'c1': 1.6})
        third = client.evaluate(self.state, 'MARKERQUERY', self.cards(2), source_versions={'a': '1'})
        self.assertTrue(third['cache_hit'])
        self.assertEqual((third['scores'], third['http_requests'], len(self.laya.hits)), ({'c0': 1.6, 'c1': 1.6}, 0, 2))
        self.assertEqual(third['confidence_provenance']['origin'], 'cached_laya_max_probability')
        for path in self.cache_files():
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('MARKERQUERY', text)
            self.assertNotIn('MARKERCARD', text)


class StoreCase(LayaCase):
    def setUp(self):
        super().setUp()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(self.state, self.vault)
        self.state = self.store.state_dir

    def record(self, ident, text, **extra):
        path = self.vault / extra.pop('source', ident + '.md')
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(dict(id=ident, title=ident, text=text, source=path.relative_to(self.vault).as_posix(), **extra))


class GateTest(StoreCase):
    QUERY = 'how do we deploy the quartz site'

    def setUp(self):
        super().setUp()
        self.record('deploy', 'Quartz deploy steps: push to main, the site server pulls. RELEVANT')
        self.record('pricing', 'Quartz pricing: deploy the yearly plan at ninety dollars.')
        self.record('rollback', 'Undo a release: revert the previous commit, then deploy again.')

    def local(self, query=None):
        return self.store.context_for('claude', query or self.QUERY, strict=True)

    def auto(self, query=None, context=None):
        return advisor.auto_context(self.store, 'claude', query or self.QUERY, self.local(query) if context is None else context,
                                    budget_chars=6000)

    def test_local_only_sensitive_private_and_session_notes_never_reach_laya(self):
        self.record('localonly', 'Quartz deploy MARKERLOCALONLY host list.', remote_allowed=False)
        self.record('sensitive', 'Quartz deploy MARKERSENSITIVE token rotation.', sensitivity='sensitive')
        self.record('hidden', 'Quartz deploy MARKERPRIVATE credentials.', visibility='private')
        self.record('journal', 'Today we tried to deploy MARKERDAILY twice.', source='daily/2026-01-01.md')
        self.config('shadow', enable=['auto_context', 'context'])
        self.auto()
        raw = json.dumps(self.laya.bodies(), ensure_ascii=False)
        self.assertTrue(self.laya.hits)
        for marker in ('MARKERLOCALONLY', 'MARKERSENSITIVE', 'MARKERPRIVATE', 'MARKERDAILY'):
            self.assertNotIn(marker, raw)

    def test_secret_kill_switch_and_disabled_feature_send_nothing(self):
        self.config(enable=['auto_context'])
        leaking = self.QUERY + ' ' + SECRET
        self.assertEqual(self.auto(leaking), self.local(leaking))
        (self.state / 'jev.disabled').write_text('', encoding='utf-8')
        self.assertEqual(self.auto(), self.local())
        (self.state / 'jev.disabled').unlink()
        self.config(disable=['auto_context'])
        self.assertEqual(self.auto(), self.local())
        item = dict(id='k0', claim='Iddia.', quotes=['alinti'], context=[])
        self.config(disable=['answer'])
        self.assertIn('feature_disabled', client.evaluate(self.state, '', [item], purpose='answer_check')['diagnostics'])
        self.assertEqual(self.laya.hits, [])

    def test_auto_context_with_laya_only_logs_even_from_a_hand_edited_on(self):
        self.config(enable=['auto_context'])
        self.hand_edit(mode='on')
        local = self.local()
        self.assertEqual(sorted(r['id'] for r in local['records']), ['deploy', 'pricing'])
        self.laya.noul = lambda state: 0.1 if 'pricing' in json.dumps(state) else 0.9
        self.assertEqual(self.auto(), local)
        self.assertEqual(len(self.laya.hits), 4)  # topical + deploy, pricing, rollback
        rows = [json.loads(line) for line in (self.state / 'jev-calls.jsonl').read_text(encoding='utf-8').splitlines()]
        scored = [row for row in rows if row.get('event') == 'auto_context']
        self.assertEqual((scored[-1]['mode'], scored[-1]['outcome'], scored[-1]['dropped'], scored[-1]['rescued']),
                         ('shadow', 'scored_unverified', 1, 1))
        # Even a measured threshold row never applies Laya scores: the provider is shadow-only.
        measured = dict(client.THRESHOLDS[('laya', 'multilingual')], verified=True)
        with patch.dict(client.THRESHOLDS, {('laya', 'multilingual'): measured}):
            self.assertEqual(self.auto(), local)
        # The same scores and a measured row would change a typesafe `on` context: the gate is the provider.
        self.hand_edit(mode='on', provider='typesafe')
        def typesafe(endpoint, body, key, timeout):
            return dict(answers={name: dict(type='noul', noul=0.1 if 'pricing' in json.dumps(body['state'].get('notes', {}).get(name, {})) else 0.9)
                                 for name in body['questions']})
        applied = advisor.auto_context(self.store, 'claude', self.QUERY, local, budget_chars=6000, transport=typesafe)
        self.assertEqual(sorted(r['id'] for r in applied['records']), ['deploy', 'rollback'])

    def test_manual_context_with_laya_never_reorders_from_a_hand_edited_on(self):
        for ident in ('qdeploy', 'qsteps'):
            self.record(ident, 'Quartz site deploy steps for %s: push to main.' % ident, project='quartz')
        self.config(enable=['context'])
        self.hand_edit(mode='on')
        original = self.laya.answer
        def low(body):
            reply = original(body)
            for answer in reply['answers'].values():
                answer.update(score=0.2, probabilities={'0': 0.8, '1': 0.2, '2': 0.0})
            return reply
        self.laya.answer = low
        plain = [r['id'] for r in self.store.retrieve(self.QUERY, project='quartz', limit=5, budget_chars=8000)['records']]
        self.assertEqual(sorted(plain), ['qdeploy', 'qsteps'])
        advised = advisor.advise_context(self.store, self.QUERY, project='quartz')
        self.assertEqual((advised['jev']['mode'], advised['jev']['degraded']), ('shadow', False))
        self.assertEqual(set(advised['jev']['scores'].values()), {0.2})
        self.assertEqual([r['id'] for r in advised['records']], plain)
        # The same low scores drop every card from a typesafe `on` result.
        self.hand_edit(mode='on', provider='typesafe')
        def typesafe(endpoint, body, key, timeout):
            return dict(answers={name: dict(type='score', score=0.2) for name in body['questions']})
        self.assertEqual(advisor.advise_context(self.store, self.QUERY, project='quartz', transport=typesafe)['records'], [])

    def test_server_down_keeps_the_local_result(self):
        self.config(url='http://127.0.0.1:%d' % closed_port(), enable=['auto_context'])
        self.assertEqual(self.auto(), self.local())


class AnswerTest(StoreCase):
    def claim(self, record, text):
        return dict(text=text, citations=[dict(record_id=record['id'], source_sha256=record['source_sha256'], quote=record['text'][:20])])

    def test_one_worker_and_a_provider_sized_context_limit(self):
        short = self.record('short', 'Quartz project prefers short notes.', project='quartz')
        long = self.record('long', 'Quartz long note. ' + 'detay ' * 300, project='quartz')
        self.assertLess(len(advisor.source_context(long)), advisor.CONTEXT_LIMIT)
        self.assertGreater(len(advisor.source_context(long)), laya.CONTEXT_CHARS['multilingual'])
        self.config()
        self.hand_edit(mode='on')
        self.laya.delay = 0.02
        claims = [self.claim(short, 'Short notes %d.' % i) for i in range(10)] + [self.claim(long, 'Long note.')]
        result = advisor.verify_answer(self.store, claims, project='quartz')
        # Scored by the fake server (supports, 0.8) but never turned into a verdict: shadow-only.
        self.assertEqual([c['verdict'] for c in result['claims']], ['uncertain'] * 11)
        self.assertEqual([c['diagnostics'] for c in result['claims'][:10]], [['semantic_shadow']] * 10)
        self.assertNotIn('relation', result['claims'][0])
        self.assertEqual(result['claims'][10]['diagnostics'], ['source_context_incomplete'])
        self.assertEqual((len(self.laya.hits), self.laya.peak), (10, 1))
        self.assertEqual(result['calibration'], 'unverified_for_provider')

    def test_memory_assessment_is_shadow_only_and_marks_unverified_calibration(self):
        note = self.record('note', 'Quartz project prefers short notes.', project='quartz')
        self.config()
        self.hand_edit(mode='on')
        proposal = dict(status='proposed', project='quartz', claim='Quartz prefers short notes.', prior_record_ids=[],
                        evidence=[dict(record_id='note', source_sha256=note['source_sha256'], quote='prefers short notes')])
        result = assess_memory(self.store, proposal, project='quartz')
        self.assertEqual(result['calibration'], 'unverified_for_provider')
        self.assertEqual((result['diagnostics'], result['dimensions'], result['route']), (['shadow_not_applied'], {}, 'local_source_review'))
        # The logged shadow scores stay visible for measurement.
        self.assertEqual(result['jev']['relations']['support']['confidence'], 0.8)
        self.assertEqual(len(self.laya.hits), 3)
        self.assertFalse(result['memory_written'])


HOOK_WRAPPER = '''import contextlib, importlib.util, io, json, sys
scripts, hook, vault, state = sys.argv[1:5]
sys.path.insert(0, scripts)
spec = importlib.util.spec_from_file_location('hook_under_test', hook)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
sys.argv = ['hook', '--vault', vault, '--state', state, '--harness', 'claude']
sys.stdin = io.StringIO(json.dumps({'hook_event_name': 'UserPromptSubmit', 'prompt': 'how do we deploy the quartz site',
                                    'session_id': 'sentetik'}))
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    module.main()
sys.stdout.write('REPORT:' + json.dumps({'output': buffer.getvalue(), 'modules': sorted(sys.modules)}))
'''


class HookTest(unittest.TestCase):
    def test_hook_with_laya_down_keeps_local_context_and_never_loads_torch(self):
        from v3_package_helpers import isolated_env
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault, state = root / 'vault', root / 'state'
            (vault / 'notes').mkdir(parents=True)
            (vault / 'notes/deploy.md').write_text('---\nid: deploy\n---\nQuartz deploy steps: push to main.\n', encoding='utf-8')
            from beyin_v3_sync import SyncEngine
            SyncEngine(vault.resolve(), state.resolve()).sync()
            wrapper = root / 'wrapper.py'
            wrapper.write_text(HOOK_WRAPPER, encoding='utf-8')
            env = isolated_env(root / 'home')

            def run():
                result = subprocess.run([sys.executable, str(wrapper), str(SCRIPTS), str(SCRIPTS / 'beyin_v3_hook.py'),
                                         str(vault), str(state)], capture_output=True, timeout=60, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout.decode('utf-8').split('REPORT:', 1)[1])
            client.set_mode(state.resolve(), 'shadow', provider='laya', enable=['auto_context'],
                            laya=dict(base_url='http://127.0.0.1:%d' % closed_port()))
            config = state.resolve() / 'jev.json'
            config.write_text(json.dumps(dict(json.loads(config.read_text(encoding='utf-8')), mode='on')), encoding='utf-8')
            report = run()
            context = json.loads(report['output'])['hookSpecificOutput']['additionalContext']
            self.assertIn('Quartz deploy steps', context)
            self.assertIn('beyin_v3_laya', report['modules'])
            for name in ('laya', 'torch', 'transformers'):
                self.assertNotIn(name, report['modules'])


if __name__ == '__main__':
    unittest.main()
