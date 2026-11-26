"""#177: rules arrive before their reasons, and the V2 "first 60 lines" text stays out of the opening."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evaluator = load('beyin_v3_rules_evaluator', 'scripts/evaluate_v3.py')
companion = load('beyin_v3_rules_companion', 'template/.claude/scripts/beyin_v3_companion.py')
COMPANION = '🔮 850-Companion'
# The V2 seed as migrated vaults still carry it (v3.6.0 template, placeholders filled).
V2_HEAD = ('# Echo Kuralları\n\nAyşe bu dosyaya koyduğu kurallar bağlayıcıdır. Oturum başında ilk 60 satırı otomatik\n'
           'olarak bağlama girer, yani buraya yazılan şey bir daha unutulmaz.\n\n## Kurallar\n\n')
V2_TAIL = ('## Nasıl büyür\n\nAyşe seni düzelttiğinde ("bunu böyle yapma", "şunu bir daha yapma", "böyle istemiyorum")\n'
           'o düzeltmeyi aynı oturumda buraya yeni bir madde olarak ekle: kural ne, neden var. Kuralı\n'
           'kullanıcının kendi cümlesine yakın tut, kendi yorumunu ekleme. Bir kural artık geçerli değilse\n'
           'sil veya üstünü güncelle, çelişen iki maddeyi yan yana bırakma. Liste uzarsa en çok işe\n'
           'yarayanları üste taşı, ilk 60 satır enjeksiyon penceresi budur.\n')
RULES = [f'- **kural:** KURAL-{i:02d} Şifre ve müşteri verisini arşive taşımadan önce sor. '
         + '**neden:** ' + 'Kullanıcı bu tarihte bu cümleyle düzeltti, şu hata olmuştu. ' * 6 + '\n\n'
         for i in range(1, 19)]
KURALLAR = V2_HEAD + ''.join(RULES) + V2_TAIL
OTHERS = {
    'Core.md': '# Core\n\n' + 'Kimlik ve çalışma biçimi. ' * 45,
    'Last-Session.md': '# Son oturum\n\n## 2026-10-01 10:00 · deneme · abcd1234\nHANDOFF_CANARY ' + 'Devir kartı. ' * 180,
    'Threads.md': '# Threads\n\n## Active Threads\n' + ''.join(
        f'### Thread: Konu {i}\n**Status:** active\n' + 'Açık iş, sıradaki adım. ' * 60 + '\n' for i in range(5)),
    'Journal.md': '# Journal\n\n## 2026-10-01\n' + 'Gözlem. ' * 300,
}


def norm(text):
    return re.sub(r'\s+', ' ', text).strip()


class CompanionRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='companion-rules-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        (self.vault / COMPANION).mkdir(parents=True)
        module = evaluator.load_runtime()
        self.store = module.MemoryStore(root / 'runtime', self.vault)
        self.addCleanup(lambda: evaluator.close_store(self.store))

    def seed(self, rules):
        for name, body in dict(OTHERS, **{'Kurallar.md': rules}).items():
            source = f'{COMPANION}/{name}'
            (self.vault / source).write_text(body, encoding='utf-8')
            self.store.ingest({'id': name, 'kind': 'note', 'status': 'active', 'text': body,
                               'source': source, 'updated_at': '2026-10-01T00:00:00Z'})

    def rules_section(self, text):
        section = text.split(f'[{COMPANION}/Kurallar.md]', 1)[1]
        return re.split(r'\n\[(?:🔮|Knowledge|Related|Latest)', section, maxsplit=1)[0]

    def test_every_rule_sentence_arrives_before_any_reason(self):
        self.seed(KURALLAR)
        source = (self.vault / COMPANION / 'Kurallar.md').read_bytes()
        for budget in (5000, 9500):
            with self.subTest(budget=budget):
                text = companion.context(self.store, budget, 'synthetic-rules', 'claude')
                self.assertLessEqual(len(text), budget)
                self.assertLessEqual(companion.client_size('claude', text), companion.CLIENT_TEXT_LIMITS['claude'][0])
                section = self.rules_section(text)
                for rule in RULES:
                    self.assertIn(norm(rule.split('**neden:**')[0]), norm(section))
                self.assertIn('[18 rule reasons (**neden:**) omitted to fit the opening; read source]', section)
                self.assertNotIn('Kullanıcı bu tarihte', section)
                self.assertNotIn('60 satır', section)
                self.assertNotIn('Nasıl büyür', section)
                self.assertIn(companion.V2_WINDOW_NOTE, section)
                self.assertIn('# Echo Kuralları', section)
                self.assertIn('HANDOFF_CANARY', text)
        # The opening is a view: the user's file is never rewritten.
        self.assertEqual((self.vault / COMPANION / 'Kurallar.md').read_bytes(), source)

    def test_rules_that_fit_keep_their_reasons_verbatim(self):
        rules = '# Kurallar\n\n' + ''.join(RULES[:2])
        self.seed(rules)
        section = self.rules_section(companion.context(self.store, 12000, 'synthetic-rules', 'codex'))
        self.assertEqual(norm(section), norm(rules))
        self.assertNotIn('omitted', section)

    def test_reason_forms(self):
        cases = [
            ('- **kural:** A. **neden:** x\n  devamı.\n- **kural:** B.\n', '- **kural:** A.\n- **kural:** B.\n'),
            ('- **kural:** A.\n  **Neden:** x\n\n1. **kural:** B. **gerekçe:** y\n## Son\n',
             '- **kural:** A.\n\n1. **kural:** B.\n## Son\n'),
            ('- **kural:** A. **neden**: x', '- **kural:** A.'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                result = companion.without_reasons(text)
                self.assertTrue(result.startswith(expected.rstrip('\n')), result)
                self.assertRegex(result, r'\n\[\d rule reasons \(\*\*neden:\*\*\) omitted')
        plain = '- **kural:** neden diye sorma, kısa yaz.\n'
        self.assertEqual(companion.without_reasons(plain), plain)

    def test_v2_window_text_is_the_only_text_dropped(self):
        own = '# Kurallar\n\n- **kural:** ilk 60 satırı okumadan özet çıkarma.\n'
        self.assertEqual(companion.excerpt('Kurallar.md', own), own)
        kept = companion.excerpt('Kurallar.md', KURALLAR)
        self.assertTrue(kept.startswith('# Echo Kuralları\n\n' + companion.V2_WINDOW_NOTE + '\n\n## Kurallar\n\n'))
        self.assertTrue(kept.endswith(RULES[-1].rstrip('\n')), kept[-200:])
        self.assertEqual(kept.count(companion.V2_WINDOW_NOTE), 1)
        # Other companion files are not filtered.
        self.assertEqual(companion.excerpt('Core.md', V2_HEAD), V2_HEAD)

    def test_project_owned_rules_keep_prose_scope_notes(self):
        central = '# Kurallar\n\n- **genel:** Her zaman kisa yaz.\n- [scope: beta] Sadece beta kurali.\n'
        self.seed(central)
        proj_rules = (
            '# Alpha Kurallari\n\n'
            '- **2026-10-11 · API kurali:** Istekleri kuyruga al. Kapsam: Faz 2.\n'
            '- **2026-10-12 · Pencere kurali:** Kisa pencere kullan. Scope: staging.\n'
        )
        proj_source = 'projects/alpha/Kurallar.md'
        (self.vault / 'projects' / 'alpha').mkdir(parents=True)
        (self.vault / proj_source).write_text(proj_rules, encoding='utf-8')
        self.store.ingest({
            'id': 'alpha-kurallar',
            'kind': 'note',
            'status': 'active',
            'project': 'alpha',
            'text': proj_rules,
            'source': proj_source,
            'updated_at': '2026-10-12T00:00:00Z',
        })
        text = companion.context(self.store, 9500, 'synthetic-proj-rules', 'claude', rules_project='alpha')
        self.assertIn('[projects/alpha/Kurallar.md]', text)
        self.assertIn('API kurali', text)
        self.assertIn('Pencere kurali', text)
        # Negative control: global Kurallar.md still filters foreign [scope: beta] tags.
        self.assertNotIn('Sadece beta kurali', text)
        self.assertIn('Her zaman kisa yaz', text)

    def test_new_installs_and_the_v2_seed_no_longer_promise_a_60_line_window(self):
        seed = (ROOT / 'template' / COMPANION / 'Kurallar.md').read_text(encoding='utf-8')
        self.assertNotRegex(seed, r'60 sat')
        self.assertNotRegex(companion.STARTERS['Kurallar.md'], r'60 sat')


if __name__ == '__main__':
    unittest.main()
