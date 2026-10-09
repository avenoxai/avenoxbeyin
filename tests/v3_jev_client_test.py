# Adapted from Forn hafiza-os; see docs/v3/THIRD-PARTY-JEV.txt.
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
sys.path.insert(0, str(ROOT / 'extensions/laya'))
import beyin_v3_jev_client as j

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.vault=Path(self.temp.name); None
        self.cards=[dict(id='one',title='A',statement='B',scope='user',domains=['all'],secret_extra='never send')]
        self.calls=[]
        self.env=patch.dict(os.environ,{'TYPESAFE_API_KEY':'test-key'},clear=True);self.env.start();self.addCleanup(self.env.stop)
    def config(self,**kw):
        (self.vault/'jev.json').write_text(json.dumps(dict(mode='shadow',**kw)))
    def transport(self,url,body,key,timeout):
        self.calls.append(body)
        self.assertNotIn('secret_extra',json.dumps(body))
        return dict(answers={q:dict(type='score',score=1.8,confidence='ignored') for q in body['questions']},usage=dict(input_tokens=3,output_tokens=2))
    def run_client(self,**kw):
        return j.evaluate(self.vault,'question',self.cards,transport=self.transport,**kw)
    def test_default_off(self):
        self.assertEqual(self.run_client()['mode'],'off');self.assertFalse(self.calls)
    def test_cache_source_and_scope_binding(self):
        self.config()
        a=self.run_client(source_versions={'a':'1'});self.assertEqual(a['scores'],{'one':1.8})
        self.assertTrue(self.run_client(source_versions={'a':'1'})['cache_hit'])
        self.assertFalse(self.run_client(source_versions={'a':'2'})['cache_hit'])
        self.assertFalse(self.run_client(source_versions={'a':'2'},scope='project:other')['cache_hit'])
        self.assertEqual(len(self.calls),3)
        for path in (self.vault/'.cache/jev').glob('*.json'):
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertNotIn('test-key',path.read_text());self.assertNotIn('question',path.read_text())
    def test_facets(self):
        self.config()
        r=self.run_client(facets=['A','B']);self.assertEqual(len(r['facet_scores']),2)
        self.assertEqual(set(self.calls[0]['questions']),{'f0_c0','f1_c0'})
    def test_bad_answers_do_not_cache(self):
        self.config()
        for value in [float('nan'),True,3,-1]:
            r=j.evaluate(self.vault,'q',self.cards,transport=lambda *a:dict(answers={'f0_c0':dict(type='score',score=value)}))
            self.assertTrue(r['degraded']);self.assertEqual(r['scores'],{})
        self.assertFalse(list((self.vault/'.cache/jev').glob('*.json')))
    def test_missing_extra_answers(self):
        self.config()
        for answers in [{},{'other':dict(type='score',score=2)}]:
            self.assertTrue(j.evaluate(self.vault,'q',self.cards,transport=lambda *a:dict(answers=answers))['degraded'])
    def test_budget_and_bad_config(self):
        self.config(max_questions=1)
        self.assertTrue(self.run_client(facets=['A','B'])['degraded']);self.assertFalse(self.calls)
        (self.vault/'jev.json').write_text('{bad')
        self.assertEqual(j.inspect_config(self.vault)['valid'],False)
        self.assertIn('config_invalid',self.run_client()['diagnostics'])
    def test_endpoint_and_error_redaction(self):
        self.config(base_url='http://example.com')
        self.assertTrue(self.run_client()['degraded']);self.assertFalse(self.calls)
        self.config()
        def bad(*args): raise RuntimeError('test-key PRIVATE')
        r=j.evaluate(self.vault,'q',self.cards,transport=bad)
        self.assertNotIn('PRIVATE',json.dumps(r));self.assertNotIn('test-key',json.dumps(r))
    def test_env_file_literal_not_executed(self):
        keyfile=self.vault/'env';keyfile.write_text('TYPESAFE_API_KEY="$(touch nope)"\n')
        self.config(env_file=str(keyfile))
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(j._environment(j.load_config(self.vault))['TYPESAFE_API_KEY'],'$(touch nope)')
        self.assertFalse((self.vault/'nope').exists())
    def test_deadline(self):
        # The transport blocks far longer than the deadline, so a loaded CI runner
        # cannot blur "returned at the deadline" into "waited for the transport".
        self.config(timeout=0.05)
        release=threading.Event();self.addCleanup(release.set)
        def slow(*args): release.wait(10); return {}
        start=time.monotonic()
        result=j.evaluate(self.vault,'q',self.cards,transport=slow)
        self.assertLess(time.monotonic()-start,5)
        self.assertIn('deadline_exceeded',result['diagnostics'])
    def test_bad_distribution(self):
        self.config()
        result=j.evaluate(self.vault,'q',self.cards,transport=lambda *a:dict(answers={
            'f0_c0':dict(type='score',score=1,probabilities=[0.1,0.1,0.1])}))
        self.assertTrue(result['degraded'])
    def test_categorized_errors(self):
        import urllib.error
        self.config()
        for status,expected in [(401,'http_unauthorized'),(429,'http_rate_limited'),(503,'http_server_error')]:
            def fail(*args): raise urllib.error.HTTPError('https://example.com',status,'PRIVATE',{},None)
            result=j.evaluate(self.vault,'q',self.cards,transport=fail)
            self.assertIn(expected,result['diagnostics'])
            self.assertNotIn('PRIVATE',json.dumps(result))
    def test_metadata_and_config(self):
        self.config()
        result=j.evaluate(self.vault,'q',self.cards,transport=lambda *a:dict(model='jev-1.13.0',answers={
            'f0_c0':dict(type='score',score=1,confidence=0.8)}))
        self.assertEqual(result['reported_model'],'jev-1.13.0')
        self.assertEqual(result['confidence_provenance']['present'],1)
        self.assertFalse(result['confidence_provenance']['used_for_selection'])
        for config in [dict(env_file=123),dict(timeout=float('nan')),dict(timeout=float('inf'))]:
            self.config(**config)
            self.assertFalse(j.inspect_config(self.vault)['valid'])
    def test_cache_expiry(self):
        self.config(cache_ttl=1);self.run_client()
        path=next((self.vault/'.cache/jev').glob('*.json'));v=json.loads(path.read_text());v['created_at']=0;path.write_text(json.dumps(v))
        self.assertFalse(self.run_client()['cache_hit'])

    def test_invalid_answer_diagnostics_preserve_usage_without_raw_content(self):
        self.config()
        variants = [
            ({}, 'answer_keys_mismatch'),
            ({'f0_c0':dict(type='choice',score=1)}, 'answer_type'),
            ({'f0_c0':dict(type='score',score=True)}, 'score_range_or_type'),
            ({'f0_c0':dict(type='score',score=1,probabilities={'bad':1})}, 'probability_keys'),
            ({'f0_c0':dict(type='score',score=1,probabilities=[.5,.5])}, 'probability_shape'),
            ({'f0_c0':dict(type='score',score=1,probabilities=[-.1,.1,1])}, 'probability_range_or_type'),
            ({'f0_c0':dict(type='score',score=1,probabilities=[.33,.33,.33])}, 'probability_sum')]
        for answers,issue in variants:
            result=j.evaluate(self.vault,'q',self.cards,transport=lambda *a:dict(answers=answers,private='DO NOT LOG',usage={'input_tokens':5,'output_tokens':2}))
            self.assertTrue(result['degraded']); self.assertEqual(result['answer_issue'],issue)
            self.assertEqual(result['usage'],{'input_tokens':5,'output_tokens':2})
            self.assertNotIn('DO NOT LOG',json.dumps(result))
            self.assertEqual(result['scores'],{})
        self.assertFalse(list((self.vault/'.cache/jev').glob('*.json')))

    def test_purpose_profiles_and_cache_separation(self):
        self.config()
        for purpose in ('retrieval','memory_review','evidence_review'):
            result=self.run_client(purpose=purpose,facets=['Requested relationship'])
            self.assertFalse(result['degraded']);self.assertFalse(result['cache_hit'])
            self.assertEqual(result['purpose'],purpose)
            self.assertTrue(self.run_client(purpose=purpose,facets=['Requested relationship'])['cache_hit'])
        self.assertEqual(len(self.calls),3)
        self.assertEqual(self.calls[0]['questions']['f0_c0']['criteria'],j.CRITERIA)
        self.assertEqual(self.calls[1]['questions']['f0_c0']['criteria'],j.REVIEW_CRITERIA)
        self.assertIn('anchor record',self.calls[1]['questions']['f0_c0']['instructions'])
        self.assertIn('unapproved proposal',self.calls[1]['questions']['f0_c0']['instructions'])
        self.assertIn('exact evidence quotes',self.calls[2]['questions']['f0_c0']['instructions'])
        self.assertNotEqual(self.calls[1]['questions'],self.calls[2]['questions'])

    def answer_items(self):
        return [dict(id='k0',claim='Notes are short.',quotes=['prefers short notes'],context=['Quartz prefers short notes. Agreed in March.']),
                dict(id='k1',claim='Notes are long.',quotes=['prefers short notes'],context=[])]
    def choice(self,choice='supports',confidence=.93,**changes):
        rest=round((1-confidence)/2,4)
        return dict(dict(type='choice',choice=choice,confidence=confidence,
                         probabilities={k:confidence if k==choice else rest for k in j.RELATIONS}),**changes)
    def answer_client(self,answer=None,items=None,**kw):
        def transport(url,body,key,timeout):
            self.calls.append(body)
            return dict(answers={q:answer or self.choice() for q in body['questions']})
        return j.evaluate(self.vault,'',self.answer_items() if items is None else items,purpose='answer_check',transport=transport,**kw)
    def test_answer_check_keys_items_and_asks_one_backticked_choice_each(self):
        self.config()
        result=self.answer_client()
        self.assertFalse(result['degraded'])
        self.assertEqual(result['relations']['k1'],dict(choice='supports',confidence=.93,probabilities=self.choice()['probabilities']))
        body=self.calls[0]
        self.assertEqual(body['state'],dict(items=dict(
            k0=dict(claim='Notes are short.',evidence=dict(quotes=['prefers short notes'],source_context=['Quartz prefers short notes. Agreed in March.'])),
            k1=dict(claim='Notes are long.',evidence=dict(quotes=['prefers short notes'])))))
        self.assertEqual(set(body['questions']),{'k0','k1'})
        self.assertEqual(body['questions']['k1']['criteria'],j.RELATIONS)
        self.assertIn('`items.k1.evidence`',body['questions']['k1']['instructions'])
        self.assertIn('`items.k1.claim`',body['questions']['k1']['instructions'])
        self.assertTrue(self.answer_client()['cache_hit'])
        self.assertEqual(self.answer_client()['relations'],result['relations'])
        self.assertEqual(len(self.calls),1)
    def test_answer_check_rejects_bad_payloads_before_network(self):
        self.config()
        item=self.answer_items()[0]
        for items in ([dict(item,id='K0')],[dict(item,id='a.b')],[item,item],[dict(item,quotes=[])],[dict(item,claim=' ')],
                      [dict(item,context=[''])],[dict(item,extra='x')],[{k:v for k,v in item.items() if k!='context'}]):
            result=self.answer_client(items=items)
            self.assertTrue(result['degraded']);self.assertIn('payload_invalid',result['diagnostics'])
        self.assertIn('payload_invalid',self.answer_client(facets=['Caller question'])['diagnostics'])
        self.assertFalse(self.calls)
    def test_answer_check_bad_choices_do_not_cache(self):
        self.config()
        good=self.choice()['probabilities']
        for answer in (self.choice(type='score'),dict(self.choice(),choice='maybe'),dict(self.choice(),choice='contradicts'),
                       self.choice(confidence=1.2),self.choice(confidence=True),self.choice(probabilities=dict(good,supports=.5)),
                       self.choice(probabilities={k:v for k,v in good.items() if k!='says_nothing'}),self.choice(probabilities=[.9,.05,.05])):
            result=self.answer_client(answer=answer)
            self.assertTrue(result['degraded']);self.assertEqual(result['relations'],{})
        self.assertFalse(list((self.vault/'.cache/jev').glob('*.json')))

    def test_unknown_purpose_rejected_before_network(self):
        self.config()
        for purpose in ('raw_prompt',None,[]):
            result=self.run_client(purpose=purpose)
            self.assertTrue(result['degraded']);self.assertIn('purpose_invalid',result['diagnostics'])
        self.assertFalse(self.calls)

    def test_http_status_only_no_response_body_or_retry(self):
        import urllib.error
        self.config();calls=[]
        def fail(*args):
            calls.append(1)
            raise urllib.error.HTTPError('https://example.com/private',502,'PRIVATE error details',{},None)
        result=j.evaluate(self.vault,'q',self.cards,transport=fail)
        self.assertEqual(result['http_status'],502);self.assertEqual(calls,[1])
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertTrue(result['degraded'])

    def test_captured_vercel_quantization_provider_only(self):
        cases=[(.63,{'0':.4,'1':.56,'2':.03}),(.08,{'0':.93,'1':.05,'2':.01})]
        for score,probabilities in cases:
            raw={'answers':{'x':dict(type='score',score=score,probabilities=probabilities)}}
            with self.assertRaises(ValueError):j._scores(raw,['x'])
            count=[]
            self.assertEqual(j._scores(raw,['x'],allow_quantized=True,quantized_counter=count),{'x':score})
            self.assertEqual(count,['x'])
        self.config(provider='vercel')
        def response(*args):return dict(answers={'f0_c0':dict(type='score',score=.63,probabilities={'2':.03,'0':.4,'1':.56})})
        result=j.evaluate(self.vault,'q',self.cards,transport=response)
        self.assertFalse(result['degraded']);self.assertEqual(result['quantized_probability_count'],1)
        self.assertIn('quantized_probability',result['diagnostics']);self.assertEqual(result['scores'],{'one':.63})
        cached=j.evaluate(self.vault,'q',self.cards,transport=response)
        self.assertTrue(cached['cache_hit']);self.assertEqual(cached['quantized_probability_count'],1)
        self.config(provider='typesafe')
        self.assertTrue(j.evaluate(self.vault,'q',self.cards,transport=response)['degraded'])

    def test_quantization_rejects_infeasible_distribution_and_score(self):
        for score,probabilities in [(1,[0,0,0]),(.63,[.4,.56,.0]),(1.2,[.4,.56,.03]),
                                    (.63,[.401,.56,.03]),(.08,[.93,.05,.04]),
                                    (1,[-.01,.5,.5]),(1,[True,.0,.0]),(1,['.4',.56,.03])]:
            raw={'answers':{'x':dict(type='score',score=score,probabilities=probabilities)}}
            with self.assertRaises(ValueError):j._scores(raw,['x'],allow_quantized=True)
        # A sum above one can also be valid independent rounding, not normalization.
        raw={'answers':{'x':dict(type='score',score=1,probabilities=[.34,.34,.33])}}
        self.assertEqual(j._scores(raw,['x'],allow_quantized=True),{'x':1.0})

    def test_endpoint_malformed_ports_raise_endpoint_invalid(self):
        malformed_urls = [
            'https://api.typesafe.ai:99999',
            'https://api.typesafe.ai:notaport',
            'https://api.typesafe.ai:-1',
        ]
        for url in malformed_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as cm:
                    j._endpoint(url)
                self.assertEqual(str(cm.exception), 'endpoint_invalid')

    def test_evaluate_with_malformed_port_returns_endpoint_invalid_diagnostic(self):
        self.config(base_url='https://api.typesafe.ai:99999')
        result = j.evaluate(self.vault, 'test query', self.cards)
        self.assertTrue(result['degraded'])
        self.assertIn('endpoint_invalid', result['diagnostics'])
        self.assertNotIn('request_failed', result['diagnostics'])

    def test_unparseable_endpoint_is_endpoint_invalid_not_request_failed(self):
        for url in ('https://[::1', 'https://[notip]/v1'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as cm:
                    j._endpoint(url)
                self.assertEqual(str(cm.exception), 'endpoint_invalid')
        self.config(base_url='https://[::1')
        result = j.evaluate(self.vault, 'test query', self.cards)
        self.assertIn('endpoint_invalid', result['diagnostics'])
        self.assertNotIn('request_failed', result['diagnostics'])

    def test_cache_hit_race_configuration_changed_not_swallowed(self):
        self.config()
        transport_calls = []

        def transport(url, body, key, timeout):
            transport_calls.append(body)
            return dict(answers={q: dict(type='score', score=1.8) for q in body['questions']})

        first = j.evaluate(self.vault, 'query', self.cards, transport=transport)
        self.assertFalse(first['degraded'])
        self.assertFalse(first['cache_hit'])
        self.assertEqual(len(transport_calls), 1)

        calls = [0]
        original_inspect = j.inspect_config

        def changing_inspect(vault):
            calls[0] += 1
            res = original_inspect(vault)
            if calls[0] >= 3:
                res = dict(res, policy_revision='changed_policy_revision')
            return res

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(j, 'inspect_config', side_effect=changing_inspect):
                second = j.evaluate(self.vault, 'query', self.cards, transport=transport)
                self.assertTrue(second['degraded'])
                self.assertEqual(second['diagnostics'], ['configuration_changed'])
                self.assertNotIn('cache_unavailable', second['diagnostics'])
                self.assertNotIn('credentials_missing', second['diagnostics'])
                self.assertEqual(len(transport_calls), 1)

    def test_cache_hit_race_at_final_check_aborts_without_stale_scores(self):
        self.config()
        transport_calls = []

        def transport(url, body, key, timeout):
            transport_calls.append(body)
            return dict(answers={q: dict(type='score', score=1.8) for q in body['questions']})

        first = j.evaluate(self.vault, 'query', self.cards, transport=transport)
        self.assertFalse(first['degraded'])

        calls = [0]
        original_inspect = j.inspect_config

        def changing_inspect(vault):
            calls[0] += 1
            res = original_inspect(vault)
            if calls[0] >= 4:
                res = dict(res, policy_revision='changed_policy_revision')
            return res

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(j, 'inspect_config', side_effect=changing_inspect):
                second = j.evaluate(self.vault, 'query', self.cards, transport=transport)
                self.assertTrue(second['degraded'])
                self.assertEqual(second['scores'], {})
                self.assertEqual(second['diagnostics'], ['configuration_changed'])
                self.assertNotIn('cache_unavailable', second['diagnostics'])

if __name__=='__main__': unittest.main()
