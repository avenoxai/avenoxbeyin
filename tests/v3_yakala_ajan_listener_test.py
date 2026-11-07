"""An older Windows listener copy refuses the worker's breakaway; the card says how to recover."""
import unittest

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


if __name__ == '__main__':
    unittest.main()
