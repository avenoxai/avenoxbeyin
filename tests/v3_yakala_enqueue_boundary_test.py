"""Single-card enqueue boundaries shared with the bulk-queue proposal.

Real temporary configuration/card/job files; no agent process or model is launched.
"""
import contextlib
from unittest.mock import patch
import v3_yakala_ajan_test as base

y = base.yakala


class EnqueueBoundaryTest(base.AgentFiles):
    def test_off_switch_changed_during_lock_wait_prevents_submission(self):
        self.configure()
        saved, card = self.card()
        before = card['path'].read_bytes()
        original_lock = y._AgentFileLock

        @contextlib.contextmanager
        def changed_queue_lock(path):
            with original_lock(path):
                y.configure_agent(self.vault, self.state, 'kapali')
                yield

        def changed_lock(path):
            return changed_queue_lock(path) if path.name == 'queue.lock' else original_lock(path)

        with patch.object(y, '_AgentFileLock', side_effect=changed_lock):
            with self.assertRaisesRegex(ValueError, 'Ajan kapali'):
                y.enqueue_agent(self.vault, self.state, saved['id'], 'Do not submit after off.')
        self.assertEqual(card['path'].read_bytes(), before)
        self.assertEqual(y._agent_jobs(self.vault, self.state), [])
        self.spawned.assert_not_called()

    def test_delivery_retry_accepts_the_same_filename_alias(self):
        self.configure()
        saved, _ = self.card()
        alias = saved['id'] + '.md'
        delivery = 'a' * 32
        first = y.enqueue_agent(self.vault, self.state, alias, 'Read this source.', delivery_id=delivery)
        again = y.enqueue_agent(self.vault, self.state, alias, 'Read this source.', delivery_id=delivery)
        self.assertEqual(again['job_id'], first['job_id'])
        self.assertEqual(len(y._agent_jobs(self.vault, self.state)), 1)

    def test_filename_alias_cannot_duplicate_an_active_owner(self):
        self.configure()
        saved, _ = self.card()
        alias = saved['id'] + '.md'
        first = y.enqueue_agent(self.vault, self.state, alias, 'Read this source.')
        card = y.find_card(self.vault, saved['id'], self.state)
        card['meta']['durum'] = 'bekliyor'
        y.write_card(card['path'], card['meta'], card['body'])
        with self.assertRaisesRegex(ValueError, 'zaten sirada'):
            y.enqueue_agent(self.vault, self.state, alias, 'Duplicate request.')
        self.assertEqual([job['id'] for job in y._agent_jobs(self.vault, self.state)], [first['job_id']])

    def test_music_permission_is_rechecked_after_lock_wait(self):
        self.configure(ag='acik')
        saved, card = self.card()
        original_lock = y._AgentFileLock

        @contextlib.contextmanager
        def changed_queue_lock(path):
            with original_lock(path):
                y.configure_agent(self.vault, self.state, 'claude', network='kisitli')
                yield

        def changed_lock(path):
            return changed_queue_lock(path) if path.name == 'queue.lock' else original_lock(path)

        with patch.object(y, '_AgentFileLock', side_effect=changed_lock):
            with self.assertRaisesRegex(ValueError, 'Ses isleme'):
                y.enqueue_agent(self.vault, self.state, saved['id'], 'Identify this source.',
                                music={'schema': 1, 'extract': True, 'upload': None})
        self.assertEqual(y._agent_jobs(self.vault, self.state), [])
        self.spawned.assert_not_called()
