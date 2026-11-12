"""User-facing recovery hints around the agent hand-off.

An older Windows listener copy refuses the worker's breakaway: the card says how to recover.
Ctrl+Enter with the agent off no longer saves; the popup says which key does.
"""
import unittest

import v3_yakala_ajan_test as ajan_test
from v3_yakala_ajan_test import AgentFiles, yakala


class AgentBreakawayRefusalTest(AgentFiles):
    def enqueue_with(self, error):
        self.configure()
        self.spawned.side_effect = error
        saved, _ = self.card()
        result = yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Bu kaynağı özetle.')
        card = yakala.find_card(self.vault, saved['id'], self.state)
        job = yakala._agent_load_job(self.vault, self.state, result['job_id'])
        return result, card, job

    def test_access_denied_points_to_refreshing_the_listener(self):
        # A popup started by a pre-agent listener sits in a Job Object without BREAKAWAY_OK;
        # CreateProcess(CREATE_BREAKAWAY_FROM_JOB) then fails with ERROR_ACCESS_DENIED.
        denied = OSError(13, 'Access is denied')
        denied.winerror = 5
        result, card, job = self.enqueue_with(denied)
        self.assertEqual(result['status'], 'ajan_baslatilamadi')
        self.assertIn('yakala kur ile dinleyiciyi yenile', result['hata'])
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(card['meta']['durum'], 'hata')
        self.assertIn('yakala kur ile dinleyiciyi yenile', card['meta']['ajan_notu'])
        # The saved capture itself is untouched.
        self.assertIn('İncelenecek metin', card['body'])

    def test_other_spawn_errors_keep_their_plain_reason(self):
        for error in (OSError(2, 'No such file or directory'), PermissionError(13, 'Permission denied')):
            with self.subTest(error=error):
                result, card, _ = self.enqueue_with(error)
                self.assertEqual(result['status'], 'ajan_baslatilamadi')
                self.assertNotIn('yakala kur', result['hata'])
                self.assertNotIn('yakala kur', card['meta']['ajan_notu'])



class AgentDisabledShortcutTest(AgentFiles):
    popup = ajan_test.AgentPopupTest.popup  # the fake Tk popup, without re-running that class's tests

    def texts(self, root):
        return [item.get('text') for item in root.canvas.items.values()]

    def test_ctrl_enter_with_the_agent_off_says_how_to_save(self):
        result, root = self.popup([('type', 'Bu yalnızca yazılan metin.'),
                                   ('key', '<Control-Return>'), ('key', '<Escape>')])
        self.assertEqual(result['status'], 'vazgecildi')
        self.assertEqual(root.key_results['<Control-Return>'], 'break')
        self.assertIn('Ajan kapalı · kaydetmek için Enter', self.texts(root))
        self.assertEqual(yakala.pending(self.vault, self.state), 0)
        self.spawned.assert_not_called()

    def test_hint_does_not_replace_the_closing_state(self):
        result, root = self.popup([('type', 'İptal edilen metin.'),
                                   ('key', '<Escape>'), ('key', '<Control-Return>')])
        self.assertEqual(result['status'], 'vazgecildi')
        self.assertNotIn('Ajan kapalı · kaydetmek için Enter', self.texts(root))


if __name__ == '__main__':
    unittest.main()
