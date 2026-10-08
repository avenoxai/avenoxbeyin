#!/usr/bin/env python3
"""#201: five versions of one rule; every read route serves only the current one."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_passage_test import Vault  # noqa: E402

# Older versions repeat the query words, so they are the stronger lexical match. If retired
# notes were filtered after ranking, limit=1 would pick an old version.
OLD = 'Hesap yönlendirme kuralı: hesap yönlendirme kuralı gereği {} hesap önce kullanılır.'
NEW = 'Hesap yönlendirme kuralı: beşinci hesap önce.'
QUERY = 'hesap yönlendirme kuralı'


class SupersedeFamilyTest(Vault):
    def setUp(self):
        super().setUp()
        self.ingest('rule-v1', OLD.format('birinci'), 'knowledge/rule-v1.md', status='superseded')
        self.ingest('rule-v2', OLD.format('ikinci'), 'knowledge/rule-v2.md')
        self.ingest('rule-v3', OLD.format('üçüncü'), 'knowledge/rule-v3.md', supersedes=['rule-v2'])
        self.ingest('rule-v4', OLD.format('dördüncü'), 'knowledge/rule-v4.md', supersedes=['knowledge/rule-v3'])
        self.ingest('rule-v5', NEW, 'knowledge/rule-v5.md', supersedes=['[[rule-v4]]'])

    def test_note_route_serves_only_the_current_version(self):
        for limit in (1, 5):
            with self.subTest(limit=limit):
                ids = [r['id'] for r in self.store.retrieve(QUERY, statuses=('active',), limit=limit)['records']]
                self.assertEqual(ids, ['rule-v5'])

    def test_strict_context_serves_only_the_current_version(self):
        for limit in (1, 5):
            with self.subTest(limit=limit):
                ids = [r['id'] for r in self.strict(QUERY, statuses=('active',), limit=limit)['records']]
                self.assertEqual(ids, ['rule-v5'])


class SelfSupersedeTest(Vault):
    def test_a_note_naming_itself_by_link_is_not_retired(self):
        # _validate rejects a record naming its own id; a link or path to itself slips past it.
        for name, value in (('a', '[[a]]'), ('b', 'knowledge/b')):
            with self.subTest(value=value):
                self.ingest('md-' + name, f'Zirkon {name} kovası tek karar.', f'knowledge/{name}.md', supersedes=[value])
                ids = [r['id'] for r in self.store.retrieve(f'zirkon {name} kovası karar')['records']]
                self.assertIn('md-' + name, ids)


if __name__ == '__main__':
    unittest.main(verbosity=2)
