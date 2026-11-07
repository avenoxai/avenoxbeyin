"""Explicit bulk capture jobs: real durable files/locks, no model or external network.

The inherited fixture stands in only for notifications and detached worker launch. Tests
exercise the real worker loop, card ownership, crash recovery and stdlib OS file locks.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import uuid

import v3_yakala_ajan_test as base_tests

yakala = base_tests.yakala


class BatchFiles(base_tests.AgentFiles):
    def bulk(self, **options):
        options.setdefault('request', 'Bu kaynagi incele ve sonucunu kaynaklariyla not et.')
        return yakala.enqueue_agent_batch(self.vault, self.state, **options)

    def submit(self, **options):
        return self.bulk(send=True, **options)

    def members(self, result):
        batch = yakala._agent_batch_load(self.vault, self.state, result['batch_id'])
        return [yakala._agent_batch_member(self.vault, self.state, batch, item) for item in batch['items']]

    def mutate_card(self, card, **values):
        current = yakala.find_card(self.vault, card['id'], self.state)
        current['meta'].update(values)
        yakala.write_card(current['path'], current['meta'], current['body'])

    def no_submissions(self):
        root = yakala._agent_root(self.state)
        self.assertFalse((root / 'jobs').exists())
        self.assertFalse((root / 'batches').exists())
        self.spawned.assert_not_called()

    def cli(self, arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(yakala.main(arguments, vault=self.vault, state=self.state), 0)
        return json.loads(output.getvalue())


class BatchPreviewTest(BatchFiles):
    def test_off_preview_is_read_only_and_reports_that_send_is_disabled(self):
        _, card = self.card()
        before = card['path'].read_bytes()
        self.assertFalse(self.state.exists())
        result = self.bulk()
        self.assertEqual(result['status'], 'toplu_onizleme')
        self.assertEqual(result['ajan'], 'kapali')
        self.assertFalse(result['gonderilebilir'])
        self.assertFalse(self.state.exists())
        self.assertEqual(before, card['path'].read_bytes())
        self.spawned.assert_not_called()

    def test_plain_capture_and_enabled_preview_never_submit(self):
        self.configure()
        _, card = self.card()
        before = card['path'].read_bytes()
        result = self.bulk()
        self.assertTrue(result['gonderilebilir'])
        self.assertEqual(before, card['path'].read_bytes())
        self.no_submissions()

    def test_request_source_and_bounds_are_explicit(self):
        self.card()
        invalid = [dict(), dict(request='x', use_why=True), dict(request=''), dict(request='x\0y'),
                   dict(request='x' * 16001), dict(request='x', limit=0), dict(request='x', limit=33),
                   dict(request='x', limit=True), dict(request='x', send=1), dict(request='x', ids='../bad'),
                   dict(request='x', ids=['../bad']), dict(request='x', kinds='youtube'),
                   dict(request='x', kinds=['--execute']), dict(request='x', domains=['example.com/path']),
                   dict(request='x', domains=['localhost']), dict(request='x', expected_plan='not-a-plan')]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                yakala.enqueue_agent_batch(self.vault, self.state, **options)
        self.no_submissions()

    def test_oldest_capture_uses_absolute_time_and_stable_missing_timestamp_order(self):
        _, first = self.card(text='Z first capture')
        _, second = self.card(text='A second capture')
        _, unknown = self.card(text='Unknown timestamp')
        self.mutate_card(first, yakalandi='2026-10-19T01:00:00+03:00')
        self.mutate_card(second, yakalandi='2026-10-18T22:01:00+00:00')
        self.mutate_card(unknown, yakalandi='not-a-date')
        result = self.bulk(limit=2)
        self.assertEqual([row['id'] for row in result['kartlar']], [first['id'], second['id']])
        self.assertEqual(result['uygun'], 3)
        self.assertEqual(result['secilen'], 2)

    def test_ids_aliases_kind_and_domain_filters_intersect_without_substring_matching(self):
        _, reel = self.card(url='https://www.instagram.com/reel/one')
        self.card(url='https://instagram.com.attacker.example/reel/two')
        self.card(url='https://www.youtube.com/watch?v=three')
        result = self.bulk(ids=[reel['id'], reel['id'] + '.md'], kinds=['makale'], domains=['instagram.com'])
        self.assertEqual([row['id'] for row in result['kartlar']], [reel['id']])
        self.assertEqual(self.bulk(kinds=['youtube'], domains=['instagram.com'])['secilen'], 0)
        self.no_submissions()

    def test_only_initial_neden_section_is_used_and_never_selection_or_recapture_text(self):
        saved, original = self.card(why='Ilk kullanici istegi.', url='https://example.com/repeated', text='Kaynak talimati.')
        yakala.capture(self.vault, url='https://example.com/repeated', why='Sonraki yakalama notu.', state=self.state)
        result = self.bulk(request=None, use_why=True)
        self.assertEqual(result['kartlar'][0]['istek'], 'Ilk kullanici istegi.')
        self.assertEqual(result['istek_kaynagi'], 'neden')
        self.assertEqual(result['kartlar'][0]['id'], saved['id'])
        self.assertEqual(self.bulk(request='Ortak yeni istek.')['kartlar'][0]['istek'], 'Ortak yeni istek.')
        self.assertEqual(yakala.find_card(self.vault, original['id'], self.state)['meta']['durum'], 'bekliyor')

    def test_missing_neden_is_visible_in_preview_and_common_request_can_replace_it(self):
        self.card(why='')
        result = self.bulk(request=None, use_why=True)
        self.assertFalse(result['gonderilebilir'])
        self.assertTrue(result['kartlar'][0]['hata'])
        self.assertFalse(self.bulk(request='Bu kaynagi ozetle.')['gonderilebilir'])  # still off
        self.configure()
        self.assertTrue(self.bulk(request='Bu kaynagi ozetle.')['gonderilebilir'])

    def test_changed_card_or_settings_rejects_an_expected_plan_without_partial_writes(self):
        self.configure()
        _, card = self.card()
        original = self.bulk()
        self.mutate_card(card, baslik='Sonradan duzenlenen baslik')
        with self.assertRaisesRegex(ValueError, 'plani degisti'):
            self.submit(expected_plan=original['plan'])
        current = self.bulk()
        yakala.configure_agent(self.vault, self.state, 'claude', network='acik')
        with self.assertRaisesRegex(ValueError, 'plani degisti'):
            self.submit(expected_plan=current['plan'])
        self.no_submissions()

    def test_damaged_job_is_a_read_only_capacity_warning_and_blocks_new_batch(self):
        self.configure()
        _, job, _ = self.queued()
        path = yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json'
        path.write_text('{broken', encoding='utf-8')
        self.card(text='A different pending card')
        root = yakala._agent_root(self.state)
        before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        result = self.bulk()
        after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(result['gonderilebilir'])
        self.assertEqual(len(result['kayit_sorunlari']), 1)
        with self.assertRaisesRegex(ValueError, 'kapasitesi dogrulanamadi'):
            self.submit()

    def test_linked_card_is_refused_before_reading_its_target(self):
        _, card = self.card()
        outside = Path(self.tmp.name) / 'outside.md'
        outside.write_text('PRIVATE OUTSIDE', encoding='utf-8')
        card['path'].unlink()
        try:
            card['path'].symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('symlink capability is unavailable for this account')
        with self.assertRaisesRegex(ValueError, 'sembolik bag'):
            self.bulk()
        self.assertEqual(outside.read_text(encoding='utf-8'), 'PRIVATE OUTSIDE')
        self.assertFalse(self.state.exists())


class BatchSelectedSourceTest(BatchFiles):
    primary = 'https://example.org/primary'
    selected = 'https://example.net/selected'
    unselected = 'https://example.com/body-only'

    def selected_card(self, values=None):
        self.configure()
        _, card = self.card(url=self.primary)
        meta = dict(card['meta'], ek_urller=[self.selected] if values is None else values)
        yakala.write_card(card['path'], meta, card['body'] + '\n\n' + self.unselected)
        return card

    def running_member(self):
        job = self.members(self.submit())[0]
        job['state'] = 'running'
        yakala._agent_prepare_runtime(self.vault, self.state, job)
        return job

    def test_selected_secondary_is_frozen_in_each_job_and_restricted_fetch_can_use_it(self):
        self.selected_card([self.selected, self.primary, self.selected])
        job = self.running_member()
        fetched = {'url': self.selected, 'links': [], 'text': 'Synthetic public source'}
        with patch.object(yakala, '_agent_fetch_source', return_value=fetched) as fetch:
            result = yakala._agent_tool_call(self.vault, self.state, job['id'],
                                            'fetch_source', {'url': self.selected})
        self.assertEqual(result['url'], self.selected)
        fetch.assert_called_once_with(self.selected)
        self.assertEqual(job['extra_urls'], [self.selected])
        self.assertIn(self.selected, yakala._agent_prompt(self.vault, job))

    def test_body_query_and_later_card_edit_cannot_expand_a_batch_members_sources(self):
        card = self.selected_card()
        job = self.running_member()
        later = 'https://example.org/later-selection'
        self.mutate_card(card, ek_urller=[self.selected, later])
        with patch.object(yakala, '_agent_fetch_source') as fetch:
            for value in (self.unselected, later, self.selected + '?private=CANARY'):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'not authorized'):
                    yakala._agent_tool_call(self.vault, self.state, job['id'], 'fetch_source', {'url': value})
        fetch.assert_not_called()

    def test_invalid_secondary_metadata_is_a_read_only_preview_error(self):
        card = self.selected_card()
        original_meta, original_body = yakala.read_card(card['path'])
        oversized = ['https://example.org/' + str(index) + '/' + 'x' * 8000 for index in range(9)]
        for values in (self.selected, [None], ['file:///private'], [self.selected] * 64,
                       [self.selected + '/' + 'x' * 8192], oversized):
            with self.subTest(values_type=type(values).__name__, size=len(values)):
                yakala.write_card(card['path'], dict(original_meta, ek_urller=values), original_body)
                before = card['path'].read_bytes()
                result = self.bulk()
                self.assertFalse(result['gonderilebilir'])
                self.assertTrue(result['kartlar'][0]['hata'])
                self.assertEqual(card['path'].read_bytes(), before)
                self.no_submissions()

    def test_invalid_later_member_blocks_all_jobs_cards_and_manifest_before_send(self):
        self.configure()
        _, first = self.card(text='First valid source')
        _, invalid = self.card(text='Later invalid source')
        self.mutate_card(first, yakalandi='2026-01-01T00:00:00+00:00')
        self.mutate_card(invalid, yakalandi='2026-01-02T00:00:00+00:00', ek_urller=['file:///private'])
        before = [card['path'].read_bytes() for card in (first, invalid)]
        with self.assertRaisesRegex(ValueError, 'hicbir is gonderilmedi'):
            self.submit()
        self.assertEqual(before, [card['path'].read_bytes() for card in (first, invalid)])
        self.no_submissions()

    def oversized_selection(self):
        values = ['https://example.org/' + str(index) + '/' + 'x' * 8128 for index in range(8)]
        selected = self.selected_card(values)
        _, first = self.card(text='First valid source')
        self.mutate_card(first, yakalandi='2026-01-01T00:00:00+00:00')
        self.mutate_card(selected, yakalandi='2026-01-02T00:00:00+00:00')
        self.assertEqual(yakala._agent_extra_source_urls(values, self.primary), values)
        return first, selected

    def test_combined_utf8_size_is_a_read_only_preview_error_for_only_oversized_member(self):
        cards = self.oversized_selection()
        before = [card['path'].read_bytes() for card in cards]
        result = self.bulk(request='\U0001f642' * yakala.AGENT_MAX_REQUEST)
        self.assertFalse(result['gonderilebilir'])
        self.assertEqual(result['kartlar'][0]['hata'], '')
        self.assertRegex(result['kartlar'][1]['hata'], 'is kaydi.*buyuk')
        self.assertEqual(before, [card['path'].read_bytes() for card in cards])
        self.no_submissions()

    def test_combined_utf8_size_blocks_the_whole_send_before_manifest_job_or_card_write(self):
        cards = self.oversized_selection()
        before = [card['path'].read_bytes() for card in cards]
        with self.assertRaisesRegex(ValueError, 'hicbir is gonderilmedi'):
            self.submit(request='\U0001f642' * yakala.AGENT_MAX_REQUEST)
        self.assertEqual(before, [card['path'].read_bytes() for card in cards])
        self.no_submissions()


class BatchQueueTest(BatchFiles):
    def test_manifest_every_job_and_card_are_durable_before_one_wakeup(self):
        self.configure()
        self.card(text='First source')
        self.card(text='Second source')

        def inspect(vault, state):
            manifest_paths = list((yakala._agent_root(state) / 'batches').glob('*.json'))
            self.assertEqual(len(manifest_paths), 1)
            batch = yakala._agent_batch_load(vault, state, manifest_paths[0].stem)
            self.assertEqual(batch['state'], 'submitted')
            for item in batch['items']:
                job = yakala._agent_batch_member(vault, state, batch, item)
                self.assertEqual(job['state'], 'queued')
                self.assertEqual(yakala.find_card(vault, item['card_id'], state)['meta']['ajan_isi'], job['id'])
                self.assertEqual(job['config']['ag'], 'kisitli')
                self.assertEqual(job['request'], item['request'])
                self.assertFalse(any('music' in key or 'consent' in key for key in job))

        self.spawned.side_effect = inspect
        result = self.submit(request='Bu videodaki muzigi bul ve indir.')
        self.spawned.assert_called_once_with(self.vault, self.state)
        self.assertEqual(result['sayilar']['queued'], 2)
        self.assertFalse(result['bitti'])
        self.assertFalse(result['hepsi_basarili'])

    def test_capacity_failure_validates_whole_batch_before_any_card_or_job_write(self):
        self.configure(max_queue=1)
        originals = [self.card(text='Source ' + str(i))[1] for i in range(2)]
        before = [card['path'].read_bytes() for card in originals]
        with self.assertRaisesRegex(ValueError, 'kuyruga sigmiyor'):
            self.submit()
        self.assertEqual(before, [card['path'].read_bytes() for card in originals])
        self.no_submissions()

    def test_one_missing_neden_blocks_all_selected_cards_before_any_write(self):
        self.configure()
        originals = [self.card(text='Has instruction', why='Isle.')[1], self.card(text='Missing instruction', why='')[1]]
        before = [card['path'].read_bytes() for card in originals]
        with self.assertRaisesRegex(ValueError, 'hicbir is gonderilmedi'):
            self.submit(request=None, use_why=True)
        self.assertEqual(before, [card['path'].read_bytes() for card in originals])
        self.no_submissions()

    def test_missing_explicit_card_blocks_all_before_any_write(self):
        self.configure()
        _, card = self.card()
        with self.assertRaisesRegex(ValueError, 'Kart bulunamadi'):
            self.submit(ids=[card['id'], 'does-not-exist'])
        self.no_submissions()

    def test_only_pending_cards_are_eligible_and_repeated_send_does_not_duplicate(self):
        self.configure()
        _, pending = self.card(text='Pending source')
        for status in ('cikarildi', 'islendi', 'hata', 'onay-bekliyor', 'ajanda'):
            _, card = self.card(text='Other status ' + status)
            self.mutate_card(card, durum=status)
        result = self.submit()
        self.assertEqual([row['id'] for row in result['kartlar']], [pending['id']])
        self.assertEqual(self.submit()['status'], 'toplu_bos')
        self.assertEqual(len(yakala._agent_jobs(self.vault, self.state)), 1)
        self.spawned.assert_called_once()

    def test_active_ownership_survives_manual_pending_reset_and_id_aliases(self):
        self.configure()
        _, card = self.card()
        first = yakala.enqueue_agent(self.vault, self.state, card['id'] + '.md', 'Original request.')
        self.mutate_card(card, durum='bekliyor')  # an unrelated editor does not release a live job
        with self.assertRaisesRegex(ValueError, 'zaten sirada'):
            yakala.enqueue_agent(self.vault, self.state, card['id'] + '.md', 'Duplicate request.')
        result = self.submit()
        self.assertEqual(result['status'], 'toplu_bos')
        self.assertEqual(result['atlanan'][0]['id'], card['id'])
        self.assertEqual([job['id'] for job in yakala._agent_jobs(self.vault, self.state)], [first['job_id']])

    def test_fifo_clock_rollback_keeps_existing_then_selected_then_later_single_order(self):
        self.configure()
        _, earlier, _ = self.queued(text='Old queue entry')
        _, a = self.card(text='Selected first')
        _, b = self.card(text='Selected second')
        self.mutate_card(a, yakalandi='2026-01-01T00:00:00+00:00')
        self.mutate_card(b, yakalandi='2026-01-02T00:00:00+00:00')
        with patch('time.time_ns', return_value=1):
            bulk = self.submit()
            later, _ = self.card(text='Later single source')
            single = yakala.enqueue_agent(self.vault, self.state, later['id'], 'Later request.')
        expected = [earlier['id'], *[row['job_id'] for row in bulk['kartlar']], single['job_id']]
        jobs = yakala._agent_jobs(self.vault, self.state)
        self.assertEqual([job['id'] for job in jobs], expected)
        self.assertTrue(all(left['created_ns'] < right['created_ns'] for left, right in zip(jobs, jobs[1:])))

    def test_single_sender_rereads_off_switch_after_acquiring_queue_lock(self):
        self.configure()
        saved, _ = self.card()
        on = yakala.agent_settings(self.vault, self.state)
        off = dict(on, client='kapali')
        with patch.object(yakala, 'agent_settings', side_effect=[on, off]):
            with self.assertRaisesRegex(ValueError, 'Ajan kapali'):
                yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Do not start after off.')
        self.no_submissions()

    def test_concurrent_single_and_bulk_senders_share_capacity_lock(self):
        self.configure(max_queue=1)
        _, first = self.card(text='Single source')
        _, second = self.card(text='Batch source')
        barrier = threading.Barrier(2)
        results, errors = [], []

        def sender(batch):
            try:
                barrier.wait(timeout=5)
                result = (self.submit(ids=[second['id']]) if batch else
                          yakala.enqueue_agent(self.vault, self.state, first['id'], 'Single request.'))
                results.append(result)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=sender, args=(batch,)) for batch in (False, True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive(), 'queue or card lock deadlocked')
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(len(yakala._agent_jobs(self.vault, self.state)), 1)
        self.assertEqual(yakala.pending(self.vault, self.state), 1)

    def test_spawn_failure_preserves_all_queued_jobs_for_explicit_wakeup(self):
        self.configure()
        self.card(text='Preserved first')
        self.card(text='Preserved second')
        self.spawned.side_effect = OSError('launcher unavailable')
        result = self.submit()
        self.assertIn('ajan-devam', result['worker_error'])
        self.assertEqual(result['sayilar']['queued'], 2)
        self.assertEqual(result['sayilar']['failed'], 0)
        self.assertEqual(yakala.pending(self.vault, self.state), 0)

    def test_same_card_single_alias_and_bulk_race_cannot_duplicate_with_free_capacity(self):
        self.configure(max_queue=32)
        _, card = self.card()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def sender(batch):
            try:
                barrier.wait(timeout=5)
                result = (self.submit(ids=[card['id']]) if batch else
                          yakala.enqueue_agent(self.vault, self.state, card['id'] + '.md', 'Single aliased request.'))
                results.append(result)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=sender, args=(batch,)) for batch in (False, True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive(), 'shared owner check deadlocked')
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(len(yakala._agent_jobs(self.vault, self.state)), 1)
        self.spawned.assert_called_once()

    def test_largest_valid_batch_with_json_escaping_remains_readable_and_bound_to_requests(self):
        self.configure(max_queue=32)
        for index in range(32):
            self.card(text='Escaped request source ' + str(index))
        request = '\x01' * yakala.AGENT_MAX_REQUEST  # each character expands to six JSON bytes
        preview = self.bulk(request=request, limit=32)
        result = self.submit(request=request, limit=32, expected_plan=preview['plan'])
        path = yakala._agent_batch_path(self.state, result['batch_id'])
        self.assertLess(path.stat().st_size, yakala.AGENT_BATCH_BYTES)
        self.assertEqual(result['sayilar']['queued'], 32)
        self.assertTrue(all(job['request'] == request for job in self.members(result)))
        self.spawned.assert_called_once()

    def test_partial_card_write_retains_manifest_and_never_resubmits_the_selection(self):
        self.configure()
        originals = [self.card(text='Partial source ' + str(i))[1] for i in range(3)]
        write = yakala.write_card
        count = 0

        def interrupted(path, meta, body):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('simulated full disk at second card')
            return write(path, meta, body)

        with patch.object(yakala, 'write_card', side_effect=interrupted):
            result = self.submit()
        self.assertEqual(result['status'], 'toplu_kismi')
        self.assertEqual(result['gonderim_durumu'], 'interrupted')
        self.assertEqual(result['sayilar']['queued'], 1)
        self.assertEqual(result['sayilar']['preparing'], 1)
        self.assertEqual(result['sayilar']['kayit_yok'], 1)
        self.assertFalse(result['bitti'])
        self.spawned.assert_not_called()
        ran = []

        def finish(vault, state, job):
            ran.append(job['id'])
            yakala._agent_end(vault, state, job, 'approval_required', 'Test returned control safely.')

        with patch.object(yakala, '_agent_run_job', side_effect=finish):
            self.assertEqual(yakala._agent_worker(self.vault, self.state), 0)
        self.assertEqual(ran, [result['kartlar'][0]['job_id']])
        self.assertEqual(yakala.find_card(self.vault, originals[1]['id'], self.state)['meta']['durum'], 'bekliyor')
        self.assertEqual(yakala.find_card(self.vault, originals[2]['id'], self.state)['meta']['durum'], 'bekliyor')
        self.assertFalse(yakala.agent_batch_status(self.vault, self.state, result['batch_id'])['bitti'])

    def test_later_card_is_rechecked_after_intent_and_previous_member_publication(self):
        self.configure()
        _, first = self.card(text='First source before concurrent edit')
        _, second = self.card(text='Second source before concurrent edit')
        self.mutate_card(first, yakalandi='2026-01-01T00:00:00+00:00')
        self.mutate_card(second, yakalandi='2026-01-02T00:00:00+00:00')
        publish = yakala._agent_json
        edited = None

        def concurrent_edit(path, payload):
            nonlocal edited
            publish(path, payload)
            if path.name == 'job.json' and payload['card_id'] == first['id'] and payload['state'] == 'queued':
                manifests = list((yakala._agent_root(self.state) / 'batches').glob('*.json'))
                self.assertEqual(len(manifests), 1)
                intent = yakala._agent_batch_load(self.vault, self.state, manifests[0].stem)
                self.assertEqual(intent['state'], 'preparing')
                self.assertEqual([item['card_id'] for item in intent['items']], [first['id'], second['id']])
                self.mutate_card(second, ek_urller=['https://example.org/late-edit'])
                edited = second['path'].read_bytes()

        with patch.object(yakala, '_agent_json', side_effect=concurrent_edit):
            result = self.submit()
        self.assertEqual(result['status'], 'toplu_kismi')
        self.assertIn('Kart gonderim sirasinda degisti', result['submission_error'])
        self.assertEqual(result['sayilar']['queued'], 1)
        self.assertEqual(result['sayilar']['kayit_yok'], 1)
        self.assertEqual(len(yakala._agent_jobs(self.vault, self.state)), 1)
        self.assertEqual(second['path'].read_bytes(), edited)
        self.spawned.assert_not_called()

    def test_hard_crash_keeps_initial_manifest_with_all_intended_identities(self):
        self.configure()
        self.card(text='Crash first')
        self.card(text='Crash second')
        write = yakala.write_card
        count = 0

        def interrupted(path, meta, body):
            nonlocal count
            count += 1
            if count == 2:
                raise KeyboardInterrupt('simulated sender death')
            return write(path, meta, body)

        with patch.object(yakala, 'write_card', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.submit()
        manifests = list((yakala._agent_root(self.state) / 'batches').glob('*.json'))
        self.assertEqual(len(manifests), 1)
        result = yakala.agent_batch_status(self.vault, self.state, manifests[0].stem)
        self.assertEqual(result['gonderim_durumu'], 'preparing')
        self.assertEqual(result['secilen'], 2)
        self.assertEqual(result['sayilar']['queued'], 1)
        self.assertEqual(result['sayilar']['preparing'], 1)
        self.spawned.assert_not_called()


class BatchCancelTest(BatchFiles):
    def prepared(self, count=2):
        self.configure()
        originals = [self.card(text='Cancel source ' + str(i), why='Keep my captured reason.')[1] for i in range(count)]
        return self.submit(), originals

    def test_cancel_queued_members_restores_pending_and_keeps_user_edits(self):
        result, originals = self.prepared()
        self.mutate_card(originals[0], custom_field='New user field')
        returned = yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
        self.assertEqual(returned['sayilar']['cancelled'], 2)
        self.assertTrue(returned['bitti'])
        self.assertFalse(returned['hepsi_basarili'])
        self.assertEqual(yakala.pending(self.vault, self.state), 2)
        for original in originals:
            card = yakala.find_card(self.vault, original['id'], self.state)
            self.assertEqual(card['body'], original['body'])
            self.assertEqual(card['meta']['ajan_asama'], 'iptal')
        self.assertEqual(yakala.find_card(self.vault, originals[0]['id'], self.state)['meta']['custom_field'], 'New user field')
        snapshots = [original['path'].read_bytes() for original in originals]
        again = yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
        self.assertEqual(again['iptal_edilen'], [])
        self.assertEqual(snapshots, [original['path'].read_bytes() for original in originals])
        with patch.object(yakala, '_agent_run_job') as run:
            self.assertEqual(yakala._agent_worker(self.vault, self.state), 0)
        run.assert_not_called()

    def test_cancel_intent_is_durable_before_card_restore(self):
        result, _ = self.prepared(1)
        job = self.members(result)[0]
        write = yakala.write_card

        def inspect(path, meta, body):
            recorded = yakala._agent_load_job(self.vault, self.state, job['id'])
            self.assertEqual(recorded['state'], 'cancelled')
            self.assertTrue(recorded['card_reconcile'])
            return write(path, meta, body)

        with patch.object(yakala, 'write_card', side_effect=inspect):
            returned = yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
        self.assertEqual(returned['kart_uzlastirma_bekleyen'], 0)

    def test_failed_restore_is_reconciled_by_worker_without_starting_a_model(self):
        result, originals = self.prepared(1)
        with patch.object(yakala, '_agent_update_card', side_effect=OSError('temporary card write failure')):
            returned = yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
        self.assertEqual(returned['sayilar']['cancelled'], 1)
        self.assertEqual(returned['kart_uzlastirma_bekleyen'], 1)
        self.assertFalse(returned['bitti'])
        self.assertEqual(yakala.find_card(self.vault, originals[0]['id'], self.state)['meta']['durum'], 'ajanda')
        with patch.object(yakala, '_agent_run_job') as run:
            self.assertEqual(yakala._agent_worker(self.vault, self.state), 0)
        run.assert_not_called()
        final = yakala.agent_batch_status(self.vault, self.state, result['batch_id'])
        self.assertTrue(final['bitti'])
        self.assertEqual(final['kart_uzlastirma_bekleyen'], 0)
        self.assertEqual(yakala.pending(self.vault, self.state), 1)

    def test_running_completed_failed_and_approval_members_are_preserved(self):
        result, originals = self.prepared(5)
        jobs = self.members(result)
        jobs[0]['state'] = 'running'
        yakala._agent_json(yakala._agent_job_dir(self.vault, self.state, jobs[0]['id']) / 'job.json', jobs[0])
        for job, status in zip(jobs[1:4], ('completed', 'failed', 'approval_required')):
            yakala._agent_end(self.vault, self.state, job, status, 'Existing result must stay.')
        output = originals[1]['path'].parent / 'An-existing-result.md'
        output.write_text('Completed user result', encoding='utf-8')
        snapshots = [card['path'].read_bytes() for card in originals[:4]]
        returned = yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
        self.assertEqual(returned['sayilar']['cancelled'], 1)
        self.assertEqual(len(returned['korunan']), 4)
        self.assertEqual(snapshots, [card['path'].read_bytes() for card in originals[:4]])
        self.assertEqual(output.read_text(encoding='utf-8'), 'Completed user result')
        self.assertFalse(returned['bitti'])

    def test_worker_claim_wins_race_and_only_other_queued_members_are_cancelled(self):
        result, _ = self.prepared(2)
        started, finish = threading.Event(), threading.Event()
        ran, worker_result = [], []

        def run(vault, state, job):
            ran.append(job['id'])
            started.set()
            if not finish.wait(10):
                raise RuntimeError('test did not release the worker')
            yakala._agent_end(vault, state, job, 'approval_required', 'Test worker returned safely.')

        def worker():
            worker_result.append(yakala._agent_worker(self.vault, self.state))

        with patch.object(yakala, '_agent_run_job', side_effect=run):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(started.wait(10))
                returned = yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
                self.assertEqual(returned['sayilar']['running'], 1)
                self.assertEqual(returned['sayilar']['cancelled'], 1)
            finally:
                finish.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(worker_result, [0])
        self.assertEqual(ran, [result['kartlar'][0]['job_id']])

    def test_unowned_card_or_execution_evidence_is_never_cancelled(self):
        result, originals = self.prepared(2)
        jobs = self.members(result)
        self.mutate_card(originals[0], ajan_isi=str(uuid.uuid4()), durum='islendi')
        evidence = yakala._agent_job_dir(self.vault, self.state, jobs[1]['id']) / 'result.json'
        evidence.write_text('Evidence is retained', encoding='utf-8')
        before = [card['path'].read_bytes() for card in originals]
        returned = yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
        self.assertEqual(returned['sayilar']['cancelled'], 0)
        self.assertEqual(len(returned['korunan']), 2)
        self.assertEqual(before, [card['path'].read_bytes() for card in originals])
        self.assertEqual(evidence.read_text(encoding='utf-8'), 'Evidence is retained')

    def test_mismatched_manifest_member_cannot_cancel_an_unrelated_job(self):
        result, originals = self.prepared(1)
        _, other, _ = self.queued(text='Unrelated single job')
        path = yakala._agent_batch_path(self.state, result['batch_id'])
        data = json.loads(path.read_text(encoding='utf-8'))
        data['items'][0] = {'card_id': other['card_id'], 'card_path': other['card_path'],
                            'request': other['request'], 'job_id': other['id']}
        yakala._agent_json(path, data)
        returned = yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
        self.assertEqual(returned['sayilar']['kayit_gecersiz'], 1)
        self.assertEqual(returned['iptal_edilen'], [])
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, other['id'])['state'], 'queued')
        self.assertEqual(yakala.find_card(self.vault, originals[0]['id'], self.state)['meta']['durum'], 'ajanda')

    def test_malformed_or_duplicate_manifest_is_rejected_before_any_member_changes(self):
        result, originals = self.prepared(2)
        path = yakala._agent_batch_path(self.state, result['batch_id'])
        data = json.loads(path.read_text(encoding='utf-8'))
        before = [card['path'].read_bytes() for card in originals]
        damaged = [[], dict(data, vault=str(self.vault / 'wrong')), dict(data, items=[data['items'][0]] * 2),
                   dict(data, items=[dict(data['items'][0], card_path='../outside.md')])]
        for value in damaged:
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
                self.assertEqual(before, [card['path'].read_bytes() for card in originals])

    def test_manifest_symlink_and_path_traversal_are_refused(self):
        result, originals = self.prepared(1)
        for identifier in ('../../elsewhere', '', '/tmp/outside', '..\\outside'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                yakala.cancel_agent_batch(self.vault, self.state, identifier)
        path = yakala._agent_batch_path(self.state, result['batch_id'])
        outside = Path(self.tmp.name) / 'external-manifest.json'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('symlink capability is unavailable for this account')
        with self.assertRaisesRegex(ValueError, 'sembolik bag'):
            yakala.cancel_agent_batch(self.vault, self.state, result['batch_id'])
        self.assertEqual(yakala.find_card(self.vault, originals[0]['id'], self.state)['meta']['durum'], 'ajanda')


class BatchStatusAndCliTest(BatchFiles):
    def test_cli_preview_send_status_and_cancel_roundtrip_without_popup(self):
        self.configure()
        self.card(why='Bu metni incele.')
        preview = self.cli(['ajan-toplu', '--neden', '--limit', '1', '--json'])
        self.assertEqual(preview['status'], 'toplu_onizleme')
        self.spawned.assert_not_called()
        submitted = self.cli(['--json', 'ajan-toplu', '--neden', '--limit', '1', '--gonder', '--plan', preview['plan']])
        self.assertEqual(submitted['status'], 'toplu_gonderildi')
        shown = self.cli(['ajan-toplu-durum', submitted['batch_id'], '--json'])
        self.assertEqual(shown['sayilar']['queued'], 1)
        self.assertFalse(shown['bitti'])
        cancelled = self.cli(['ajan-toplu-iptal', submitted['batch_id'], '--json'])
        self.assertEqual(cancelled['sayilar']['cancelled'], 1)
        self.assertTrue(cancelled['bitti'])
        self.spawned.assert_called_once()

    def test_cli_requires_exactly_one_request_source(self):
        for argv in (['ajan-toplu'], ['ajan-toplu', '--neden', '--istek', 'Conflicting sources']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                yakala.main(argv, vault=self.vault, state=self.state)
            self.assertEqual(caught.exception.code, 2)
        self.no_submissions()

    def test_worker_receipt_boundary_and_real_finalize_produce_dynamic_summary(self):
        last = self.vault / 'Last-Session.md'
        last.write_text('# Last session\n\nExisting user context.\n', encoding='utf-8')
        self.configure(last_session_path='Last-Session.md')
        self.card(text='Worker first')
        self.card(text='Worker second')
        result = self.submit()
        self.assertEqual(result['sayilar']['completed'], 0)
        self.assertFalse(result['bitti'])

        def receipt(vault, state, job, summary, refs):
            path = vault / ('receipt-' + job['id'] + '.md')
            path.write_text(summary + '\n' + '\n'.join(refs), encoding='utf-8')
            return path.relative_to(vault).as_posix()

        def run(vault, state, job):
            note = yakala._agent_write_note(vault, state, job, 'Kaynakli ozet', 'Yerel testin dogrulanabilir sonucu.')
            yakala._agent_complete(vault, state, job, 'completed', 'Kaynak incelendi.', notes=[note['path']])
            terminal, reason = yakala._agent_finalize(vault, state, job)
            yakala._agent_end(vault, state, job, terminal, reason)

        with patch.object(yakala, '_agent_run_job', side_effect=run), patch.object(yakala, '_agent_receipt', side_effect=receipt):
            self.assertEqual(yakala._agent_worker(self.vault, self.state), 0)
        final = yakala.agent_batch_status(self.vault, self.state, result['batch_id'])
        self.assertTrue(final['bitti'])
        self.assertTrue(final['hepsi_basarili'])
        self.assertEqual(final['sayilar']['completed'], 2)
        for row in final['kartlar']:
            self.assertEqual(row['ozet'], 'Kaynak incelendi.')
            self.assertTrue((self.vault / row['sonuc']).is_file())
            self.assertTrue((self.vault / row['receipt']).is_file())
            job = yakala._agent_load_job(self.vault, self.state, row['job_id'])
            # The finalizer writes a short journal pointer; Last-Session stays untouched.
            self.assertIn('<!-- yakala-ajan:' + row['job_id'] + ' -->',
                          (self.vault / job['journal']).read_text(encoding='utf-8'))
            self.assertNotIn(row['job_id'], last.read_text(encoding='utf-8'))
        self.assertEqual(last.read_text(encoding='utf-8'), '# Last session\n\nExisting user context.\n')

    def test_model_complete_submission_is_not_worker_verified_completion(self):
        self.configure()
        self.card()
        result = self.submit()
        job = self.members(result)[0]
        job['state'] = 'running'
        yakala._agent_json(yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json', job)
        yakala._agent_complete(self.vault, self.state, job, 'completed', 'Model submitted an unverified claim.')
        shown = yakala.agent_batch_status(self.vault, self.state, result['batch_id'])
        self.assertFalse(shown['bitti'])
        self.assertFalse(shown['hepsi_basarili'])
        self.assertEqual(shown['sayilar']['completed'], 0)
        self.assertEqual(shown['sayilar']['running'], 1)

    def test_missing_job_or_card_is_unknown_instead_of_success(self):
        self.configure()
        _, card = self.card()
        result = self.submit()
        card['path'].unlink()
        shown = yakala.agent_batch_status(self.vault, self.state, result['batch_id'])
        self.assertEqual(shown['sayilar']['kayit_yok'], 1)
        self.assertFalse(shown['bitti'])
        self.assertFalse(shown['hepsi_basarili'])

    def test_human_output_shows_explicit_preview_and_queue_not_completion(self):
        self.configure()
        self.card(why='Do not print controls: \x1b[31m')
        preview = self.bulk(request=None, use_why=True)
        text = yakala.human(preview, 'ajan-toplu')
        self.assertIn('--gonder', text)
        self.assertNotIn('\x1b', text)
        result = self.submit()
        text = yakala.human(result, 'ajan-toplu')
        self.assertIn('tum isler bitti: hayir', text)
        self.assertIn('[sirada]', text)


if __name__ == '__main__':
    unittest.main()
