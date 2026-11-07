#!/usr/bin/env python3
"""Turkish suffix folding in retrieval: synthetic local vaults only, no network."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
spec = importlib.util.spec_from_file_location('v3_turkish_evaluator', ROOT / 'scripts/evaluate_v3.py')
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)

# (root as a note writes it, inflected form as a user types it). Both directions must retrieve.
PAIRS = (
    ('fark', 'farkı'), ('not', 'notlar'), ('rapor', 'raporlardan'), ('proje', 'projede'),
    ('kütüphane', 'kütüphanesi'), ('toplantı', 'toplantıda'), ('dükkan', 'dükkanında'),
    ('çoban', 'çobanın'), ('yayın', 'yayında'), ('kartal', 'kartalların'),
    ('berber', 'berberlerin'), ('köşe', 'köşelerde'), ('şişe', 'şişeden'),
    ('çatal', 'çatallarda'), ('kitap', 'kitaplar'), ('karar', 'kararı'),
    ('durum', 'durumu'), ('sunucu', 'sunucularda'), ('araba', 'arabanın'),
    ('neden', 'nedenleri'), ('kapı', 'kapısında'), ('orman', 'ormanda'),
    # Consonant softening: p/b and k/ğ alternate before a vowel suffix.
    ('cevap', 'cevabı'), ('hesap', 'hesabından'), ('örnek', 'örneği'),
    ('temizlik', 'temizliği'), ('çocuk', 'çocuğu'), ('istek', 'isteğini'),
)

# Pairs that must stay apart: a stemmer that merges these turns recall into noise.
NEGATIVES = (
    ('not', 'nota'), ('kar', 'karar'), ('kara', 'karar'), ('sinem', 'sinema'),
    ('bir', 'birim'), ('kod', 'kodla'), ('gol', 'golden'), ('tab', 'table'),
    ('hand', 'handle'), ('gar', 'garden'), ('sta', 'state'), ('list', 'listen'),
    ('dur', 'durum'), ('sür', 'sürüm'), ('yay', 'yayın'), ('haf', 'hafta'),
    ('denedi', 'denetim'), ('kod', 'kot'), ('card', 'cart'), ('bulgu', 'bulk'),
    # #430 vowel loss stays out where the short form is usually another word.
    ('devir', 'devreye'), ('resim', 'resmi'), ('emir', 'emre'), ('karın', 'karne'),
    ('burun', 'burn'), ('tren', 'trendi'), ('açık', 'açıklamasını'),
    # #430 these imperative stems stay distinct: a command in a prompt is not a topic.
    ('göster', 'göstermek'), ('çalıştır', 'çalıştırıyor'), ('değiştir', 'değiştirir'),
    ('çalış', 'çalışıyor'), ('göster', 'gösterdi'), ('çalıştır', 'çalışır'),
    # #430 review: so does the infinitive, how a prompt asks for an action ("değiştirmek gerekiyor").
    ('çalıştı', 'çalışmak'), ('gönderdi', 'göndermek'), ('değiştirdi', 'değiştirmek'),
    # #430 a negation keeps its own stem (the audit pairs the two), and so does the noun.
    ('çalışıyor', 'çalışmıyor'), ('çalışıyor', 'çalışma'),
    # #430 the suffix that reaches a short vowel-loss form decides: devrim, karne and Emre stay.
    ('devri', 'devrim'), ('karnı', 'karne'), ('emri', 'Emre'),
)

# Issue #430: one verb, one stem. Each group must fold onto a single token.
VERB_GROUPS = (
    ('kurulum', 'kurulmalı', 'kurulmasın', 'kurulur', 'kurulmuş', 'kuruldu', 'kurulacak',
     'kurulacağı', 'kurulması', 'kurulmak', 'kuruluyor'),
    ('yapılmalı', 'yapılır', 'yapıldı', 'yapılmış', 'yapılacak', 'yapılması'),
    ('kullanılır', 'kullanılmalı', 'kullanıldı', 'kullanılması', 'kullanılıyor'),
    ('kuruluyor', 'kuruluyordu', 'kurulur'),
)

# Issue #430: active verbs on other consonants fold onto the past tense stem ("çalıştı").
ACTIVE_VERB_GROUPS = (
    ('çalışır', 'çalıştı', 'çalışıyor', 'çalışıyorlar', 'çalışıyordu', 'çalışmalı',
     'çalışmasın', 'çalışacak', 'çalışacağı', 'çalışmış', 'ÇALIŞIYOR'),
    ('gönderir', 'gönderdi', 'gönderiyor', 'göndermeli', 'gönderecek'),
    ('değişir', 'değişti', 'değişiyor', 'değişmeli', 'değişecek'),
    ('anlatır', 'anlattı', 'anlatıyor', 'anlatmalı'),
    ('kaydediyor', 'kaydetti', 'kaydetmeli', 'kaydedecek'),
)

# Issue #430: roots that lose their last vowel before a vowel suffix.
VOWEL_LOSS = (
    ('şehir', 'şehri'), ('fikir', 'fikri'), ('beyin', 'beyni'), ('metin', 'metninde'),
    ('isim', 'ismini'), ('akıl', 'aklına'), ('kayıt', 'kaydı'), ('kısım', 'kısmı'),
    ('vakit', 'vakti'), ('ağız', 'ağzı'), ('oğul', 'oğlu'), ('ömür', 'ömrü'),
    ('devir', 'devri'), ('devir', 'devrinde'), ('devir', 'devrine'), ('burun', 'burnu'),
    ('burun', 'burnundan'), ('karın', 'karnı'), ('karın', 'karnını'), ('emir', 'emri'),
    ('emir', 'emrini'),
)

# The verb rules must not reach these: m-final nouns take -a/-e, and loanwords ending in
# a consonant cluster take a possessive vowel. Each pair must keep sharing one stem.
GUARDS = (
    ('sistem', 'sisteme'), ('durum', 'duruma'), ('platform', 'platforma'), ('alarm', 'alarma'),
    ('prompt', 'promptu'), ('backend', 'backendi'), ('trend', 'trendi'), ('build', 'buildi'),
    ('minimal', 'minimali'), ('anahtar', 'anahtarı'), ('sınır', 'sınırı'),
    # A folded i/u cannot reveal fronting, so older vowels must not veto suffixes.
    ('tarih', 'tarihte'), ('hakim', 'hakime'), ('kalori', 'kaloriler'), ('kabul', 'kabulde'),
    ('claude', 'claudenin'), ('claude', 'claudeye'), ('claude', 'claudede'),
    # #430 active verb rules: loanwords, nouns, copulas and causatives keep their stem.
    ('analist', 'analisti'), ('checklist', 'checklisti'), ('anormal', 'anormali'),
    ('teşekkür', 'teşekkürler'), ('çalıştır', 'çalıştırın'),
)

# A possessive on a three-letter root stays on the word, because _stem keeps four letters.
# (note word, question word): the question must still find the root. Upper case questions run
# Turkish I/ı and İ/i folding through the same path.
SHORT_ROOTS = (
    ('boy', 'boyum'), ('yaş', 'yaşını'), ('göz', 'gözümün'), ('kol', 'kolumda'),
    ('baş', 'başı'), ('kız', 'KIZIM'), ('diş', 'DİŞİM'), ('ses', 'sesimi'), ('cep', 'cebimi'),
)

FILLER = 'Bu sentetik test kaydıdır.'


class VaultTestCase(unittest.TestCase):
    def setUp(self):
        self.module = evaluator.load_runtime()
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-turkish-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.store = self.module.MemoryStore(self.root / 'runtime', self.vault)
        self.addCleanup(lambda: evaluator.close_store(self.store))

    def ingest(self, id, text, source, updated_at='2026-09-18T10:00:00Z'):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.store.ingest({'id': id, 'kind': 'note', 'status': 'active', 'visibility': 'internal',
                           'text': text, 'facts': {}, 'source': source, 'updated_at': updated_at})

    def ids(self, query, **kwargs):
        return [record['id'] for record in self.store.retrieve(query, **kwargs)['records']]


class TurkishStemTest(unittest.TestCase):
    """Token level: one canonical stem per word, identical on both sides of the query."""

    def setUp(self):
        self.module = evaluator.load_runtime()

    def stem(self, word):
        tokens = self.module._tokens(word)
        self.assertEqual(len(tokens), 1, word)
        return tokens.pop()

    def test_inflected_forms_fold_onto_the_same_stem(self):
        for root, inflected in PAIRS:
            with self.subTest(root=root):
                self.assertEqual(self.stem(root), self.stem(inflected))

    def test_unrelated_words_stay_apart(self):
        for left, right in NEGATIVES:
            with self.subTest(pair=(left, right)):
                self.assertNotEqual(self.stem(left), self.stem(right))

    def test_passive_verb_forms_fold_onto_one_stem(self):
        for group in VERB_GROUPS:
            with self.subTest(verb=group[0]):
                self.assertEqual(len({self.stem(word) for word in group}), 1)

    def test_active_verb_forms_fold_onto_the_past_tense_stem(self):
        for group in ACTIVE_VERB_GROUPS:
            with self.subTest(verb=group[0]):
                self.assertEqual(len({self.stem(word) for word in group}), 1)
        self.assertEqual(self.stem('çalışıyor'), self.stem('çalıştı'))

    def test_verb_rules_leave_copulas_and_bare_stems_spelled_as_written(self):
        # A copula (-dir) and a causative imperative end like an aorist but are not one.
        for word, stem in (('kalitedir', 'kalitedir'), ('önemlidir', 'onemlidir'),
                           ('çalıştır', 'calistir'), ('değiştir', 'degistir'), ('çalış', 'calis')):
            with self.subTest(word=word):
                self.assertEqual(self.stem(word), stem)

    def test_hyphenated_name_keeps_its_parts_and_its_joined_spelling(self):
        tokens = self.module._tokens
        self.assertEqual(tokens('claude-mem'), {'claude', 'mem'} | tokens('claudemem'))
        self.assertLessEqual(tokens('eposta'), tokens('e-posta'))
        self.assertLessEqual(tokens('eposta'), tokens('E\u2011POSTA'))  # non-breaking hyphen
        self.assertLessEqual(tokens('claudemem'), tokens("Claude-Mem'i"))
        # A date or numeric range is not a name, and a dash between spaces joins nothing.
        self.assertEqual(tokens('2026-10-19'), {'2026', '10', '19'})
        self.assertEqual(tokens('kurulum - claude'), {'kurul', 'claude'})
        # Nor does a dash at a line start (a list item), or one that ends a line.
        self.assertEqual(tokens('kurulum\n-claude'), {'kurul', 'claude'})
        self.assertEqual(tokens('kurulum-\nclaude'), {'kurul', 'claude'})

    def test_hyphen_in_a_note_keeps_tokenizing_linear(self):
        # A name is read once from its first part: neither a long unhyphenated run nor a long
        # chain may be rescanned from every position (minutes for this note).
        text = ('claude-mem ' + 'a' * 60000 + ' ' + '-'.join(['ab'] * 20000) + ' ' +
                ('x' * 100 + '-') * 2000 + 'x' + ' -ab' * 20000)
        started = time.monotonic()
        tokens = self.module._tokens(text)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(tokens, {'claude', 'mem', 'claudemem', 'a' * 60000, 'ab', 'x' * 100, 'x'})

    def test_a_hyphenated_query_word_counts_no_more_terms_than_its_parts(self):
        # #430 review: in a query the joined stem stands in for a missing part. As a term of
        # its own it gave one hyphenated word two shared terms with any note that names it.
        module = self.module

        def query(text):
            return module._query_compounds(text, module._strict_terms(module._tokens(text) - module.STOPWORDS))

        note = module._tokens('E-posta bülteni ve UTF-8 ayarı')
        for text, part, joined in (('Bu e-postayı kısa yaz', 'e-postayı', 'epostayı'),
                                   ('dosyayı utf-8 olarak kaydet', 'utf', 'utf8'),
                                   ('claude-mem kurulmalı mı', 'claude', 'claudemem')):
            with self.subTest(text=text):
                terms, alternatives = query(text)
                stem = next(iter(module._tokens(joined)))
                self.assertNotIn(stem, terms)
                self.assertEqual(alternatives, {next(iter(module._tokens(part) & terms)): stem})
        self.assertEqual(len(query('Bu e-postayı kısa yaz')[0] & note), 1)
        # Parts that are all stopwords or single letters leave nothing, as before.
        self.assertEqual(query('to-do')[0], set())
        self.assertEqual(query('a-b-c'), (set(), {}))
        # Written whole elsewhere in the query, the joined word is an ordinary term.
        self.assertEqual(query('claudemem ve claude-mem'), (module._tokens('claude mem claudemem'), {}))

    def test_the_joined_spelling_fills_in_for_a_missing_part_only(self):
        module = self.module
        terms, compounds = module._query_compounds('claude-mem', module._tokens('claude-mem'))
        notes = [module._token_counts(text) for text in ('Claude-mem eklentisi', 'Claudemem eklentisi', 'Claude Code')]
        module._fold_query_roots(notes, terms, module.STOPWORDS, compounds)
        self.assertEqual([len(terms & set(counts)) for counts in notes], [2, 1, 1])

    def test_hyphenated_title_still_matches_a_query_without_the_hyphen(self):
        record = {'title': 'claude-mem kurulumu', 'source': 'notes/x.md'}
        terms = self.module._tokens('claude mem kurulumu')
        self.assertEqual(self.module._name_match_score(record, terms), 3)
        self.assertEqual(self.module._name_match_score(record, self.module._tokens('claude-mem kurulumu')), 3)

    def test_vowel_loss_forms_fold_onto_the_root(self):
        for root, inflected in VOWEL_LOSS:
            with self.subTest(root=root):
                self.assertEqual(self.stem(root), self.stem(inflected))

    def test_verb_rules_leave_nouns_and_loanwords_alone(self):
        for root, inflected in GUARDS:
            with self.subTest(root=root):
                self.assertEqual(self.stem(root), self.stem(inflected))

    def test_claude_keeps_its_literal_root_without_matching_a_truncated_name(self):
        for word in ('claude', 'claudenin', 'claudeye', 'claudede'):
            with self.subTest(word=word):
                self.assertEqual(self.stem(word), 'claude')
        self.assertNotEqual(self.stem('clau'), self.stem('claude'))

    def test_stemming_replaces_rather_than_expands(self):
        # The strict gate counts matched query words, so a word may never become two tokens.
        self.assertEqual(len(self.module._tokens('kararlarında toplantıların notları')), 3)

    def test_turkish_content_words_are_no_longer_stopwords(self):
        for word in ('not', 'karar', 'proje', 'durum', 'neden'):
            with self.subTest(word=word):
                self.assertNotIn(self.stem(word), self.module.STOPWORDS)


class TurkishRetrievalMatrixTest(VaultTestCase):
    def matrix(self, written, asked):
        for index, pair in enumerate(PAIRS):
            self.ingest('rec-%02d' % index, pair[written] + ' hakkında kısa kayıt. ' + FILLER,
                        'notes/rec-%02d.md' % index)
        for index, pair in enumerate(PAIRS):
            with self.subTest(query=pair[asked]):
                self.assertEqual(self.ids(pair[asked]), ['rec-%02d' % index])

    def test_inflected_query_finds_the_root_bearing_note(self):
        self.matrix(written=0, asked=1)

    def test_root_query_finds_the_inflected_note(self):
        self.matrix(written=1, asked=0)


class TurkishIssueCaseTest(VaultTestCase):
    """The two cases reported in issue #48, including the newer irrelevant report."""

    def setUp(self):
        super().setUp()
        self.ingest('kisa-not', 'Ahmet ile Mehmet arasında 5 yaş fark var.',
                    'notes/kisa_not.md', updated_at='2026-09-10')
        self.ingest('uzun-rapor', 'Şirket faaliyet raporu. Personel yaş ortalaması ve '
                    'departman dağılımı ayrıntılı olarak incelendi. ' + FILLER * 20,
                    'notes/uzun_rapor.md', updated_at='2026-09-18')
        self.ingest('toplanti-notu', 'Toplantı için kısa bir not aldım.', 'notes/toplanti.md')

    def test_inflected_query_outranks_the_newer_irrelevant_report(self):
        self.assertEqual(self.ids('yaş farkı')[0], 'kisa-not')

    def test_literal_query_still_works(self):
        self.assertEqual(self.ids('arasında kaç yaş fark var')[0], 'kisa-not')

    def test_notlar_returns_the_note_that_says_not(self):
        self.assertEqual(self.ids('notlar'), ['toplanti-notu'])


class TurkishStrictContextTest(VaultTestCase):
    """Strict context admits one-word queries while retaining the two-word multiword gate."""

    def setUp(self):
        super().setUp()
        self.ingest('yedekleme', 'Kütüphane sunucusunda gece yedeklemesi çalışıyor.',
                    'knowledge/yedekleme.md')
        self.ingest('mutfak', 'Mutfak alışverişi: elma, ekmek, zeytinyağı.', 'notes/mutfak.md')
        self.ingest('gunluk', 'Gün sonu kaydı. ' + FILLER * 30, 'daily/2026-09-18.md')

    def test_one_inflected_word_can_find_a_matching_note(self):
        self.assertEqual(self.ids('kütüphanesi', strict=False), ['yedekleme'])
        self.assertEqual(self.ids('kütüphanesi', strict=True), ['yedekleme'])

    def test_two_inflected_words_still_reach_strict_context(self):
        self.assertEqual(self.ids('kütüphanenin yedeklemeleri', strict=True), ['yedekleme'])


class TurkishVerbIssueTest(VaultTestCase):
    """Issue #430: the question and the note conjugate the same verb differently."""

    def setUp(self):
        super().setUp()
        self.ingest('mem0', 'Mem0 kurulmasın; yerel hafıza yeterli, bulut bağımlılığı istenmiyor.',
                    'knowledge/mem0.md')
        self.ingest('tarih', 'Kaydın tarihi frontmatter alanında saklanır, dosya adında değil.',
                    'knowledge/tarih.md')
        self.ingest('docker', 'Docker imajı her sürümde yeniden derlenir.', 'knowledge/docker.md')
        self.ingest('claude', 'Claude Code rehberi: araç çalıştırma ve bellek ayarları burada saklanır.',
                    'knowledge/claude.md')
        self.ingest('mutfak', 'Mutfak alışverişi: elma, ekmek, zeytinyağı.', 'notes/mutfak.md')

    def context(self, query):
        return [record['id'] for record in
                self.store.context_for('claude', query, strict=True, budget_chars=2000)['records']]

    def test_strict_context_matches_another_conjugation(self):
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('Mem0 kurulmalı mı?'), ['mem0'])
                self.assertEqual(self.context('kayıt tarihi nerede'), ['tarih'])
                self.assertEqual(self.context('Claude ayarları nerede'), ['claude'])
                self.assertEqual(self.context("Claude'da bellek ayarları"), ['claude'])
                self.assertEqual(self.context("Claude'un ayarları"), ['claude'])

    def test_ambiguous_last_vowels_keep_real_note_matches(self):
        pairs = (('tarih', 'tarihte'), ('hakim', 'hakime'),
                 ('kalori', 'kaloriler'), ('kabul', 'kabulde'))
        for i, (_, written) in enumerate(pairs):
            self.ingest('weak-%s' % i, written + ' hakkında sentetik kayıt.',
                        'knowledge/weak-%s.md' % i)
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            for i, (query, _) in enumerate(pairs):
                with self.subTest(query=query, passages=passages):
                    expected = ['weak-%s' % i]
                    if query == 'tarih':
                        expected.append('tarih')
                    self.assertCountEqual(self.context(query), expected)

    def test_truncated_name_does_not_find_claude(self):
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('clau'), [])
                self.assertEqual(self.context('Claude'), ['claude'])

    def test_imperative_prompt_stays_silent(self):
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('şu testleri çalıştır ve sonucu göster'), [])


class TurkishActiveVerbIssueTest(VaultTestCase):
    """Issue #430: active verbs, devir/devri and hyphenated names in strict context."""

    def setUp(self):
        super().setUp()
        self.ingest('zirkon', 'Zirkon yedekleme servisi her gece çalışır.', 'knowledge/zirkon.md')
        self.ingest('kobalt', 'Kobalt kuyruğu dün gece çalıştı, sonra kapatıldı.', 'knowledge/kobalt.md')
        self.ingest('devir', 'Nikel deposunun devri 12 Mart tarihinde tamamlandı.', 'knowledge/nikel.md')
        self.ingest('devre', 'Lehim istasyonu: devre kartı ve havya ayarları.', 'notes/lehim.md')
        self.ingest('mem', 'claude-mem eklentisi kurulmasın; yerel hafıza yeterli.', 'knowledge/hafiza-eklentisi.md')
        self.ingest('eposta', 'Eposta ayarları: giden sunucu ve imza.', 'knowledge/smtp.md')
        self.ingest('claude', 'Claude Code rehberi: araç çalıştırma ve bellek ayarları burada saklanır.',
                    'knowledge/claude.md')
        self.ingest('mutfak', 'Mutfak alışverişi: elma, ekmek, zeytinyağı.', 'notes/mutfak.md')

    def context(self, query):
        return [record['id'] for record in
                self.store.context_for('claude', query, strict=True, budget_chars=2000)['records']]

    def test_another_conjugation_of_an_active_verb_is_found(self):
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('Zirkon çalışıyor mu?'), ['zirkon'])
                self.assertEqual(self.context('zirkon çalıştı mı'), ['zirkon'])
                self.assertEqual(self.context('Kobalt çalışır mı?'), ['kobalt'])
                self.assertEqual(self.context('kobalt çalışmalı'), ['kobalt'])

    def test_vowel_loss_finds_devir_but_not_devre(self):
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('nikel devir'), ['devir'])
                self.assertEqual(self.context('lehim devir'), [])

    def test_hyphenated_name_meets_its_joined_spelling(self):
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('claudemem kurulmalı mı?'), ['mem'])
                self.assertEqual(self.context('claude-mem kurulmalı mı?')[0], 'mem')
                self.assertEqual(self.context('e-posta ayarları'), ['eposta'])

    def test_imperative_prompt_stays_silent(self):
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('şu testleri çalıştır ve sonucu göster'), [])
                self.assertEqual(self.context('çalış'), [])

    def test_infinitive_request_stays_silent(self):
        # #430 review: a request in the infinitive must not meet a past tense in a note.
        self.ingest('derleme', 'Derleme betiği kodu yeniden çalıştırdı; testler geçti.', 'knowledge/derleme.md')
        self.ingest('rapor', 'Rapor sonucu dün akşam ekibe gönderdi.', 'knowledge/rapor.md')
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('kodu çalıştırmak için ne lazım'), [])
                self.assertEqual(self.context('sonucu göndermek gerekir mi'), [])
                self.assertEqual(self.context('derleme betiği kodu çalıştırdı mı'), ['derleme'])


class TurkishHyphenGateTest(VaultTestCase):
    """#430 review: one hyphenated word in a prompt must not pass the two-term strict gate."""

    def setUp(self):
        super().setUp()
        self.ingest('bulten', 'E-posta bülteni: haftalık Mailchimp gönderimi ve abone listesi temizliği.',
                    'knowledge/bulten.md')
        self.ingest('nginx', 'Sunucuda UTF-8 karakter sorunu yaşandı; Nginx ayarı düzeltildi.', 'knowledge/nginx.md')
        self.ingest('maliyet', 'GPT-4 maliyet karşılaştırması: Claude ve Gemini fiyatları.', 'knowledge/maliyet.md')
        self.ingest('imza', 'E-mail imzası: ad soyad ve şirket logosu.', 'knowledge/imza.md')
        self.ingest('eklenti', 'Claudemem eklentisi kurulmasın; yerel hafıza yeterli.', 'knowledge/eklenti.md')
        for n, text in enumerate(('Mutfak alışverişi: elma, ekmek, zeytinyağı.', 'Kitap listesi: roman, deneme, şiir.',
                                  'Spor programı: koşu, yüzme, bisiklet.', 'Tatil planı: Ege kıyısı, ağustos.')):
            self.ingest(f'f{n}', text, f'notes/f{n}.md')

    def context(self, query):
        return [record['id'] for record in
                self.store.context_for('claude', query, strict=True, budget_chars=2000)['records']]

    def test_one_hyphenated_word_injects_nothing(self):
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            for prompt in ('Bu e-postayı daha kısa ve net yaz', 'e-posta adresimi nereye yazdım',
                           'şu e-maili İngilizceye çevir', 'dosyayı utf-8 olarak kaydet',
                           'gpt-4 ile bu fonksiyonu yeniden yaz'):
                with self.subTest(passages=passages, prompt=prompt):
                    self.assertEqual(self.context(prompt), [])

    def test_a_hyphenated_name_with_its_topic_is_found(self):
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('e-posta bülteni ne zaman gidiyor'), ['bulten'])
                self.assertEqual(self.context('utf-8 karakter sorunu'), ['nginx'])
                self.assertEqual(self.context('claude-mem kurulmalı mı'), ['eklenti'])


class TurkishShortRootTest(VaultTestCase):
    """A possessive question word finds the short root a note writes ("boyum" -> "Boy: 180")."""

    def setUp(self):
        super().setUp()
        self.ingest('saglik', 'Boy: 180 cm. Kilo: 75 kg.', 'body/saglik.md')
        self.ingest('pano', 'Pano ölçüsü 120 cm x 80 cm, parçalar 20 cm.', 'notes/pano.md')
        self.ingest('kamera', 'Kamera odak mesafesi 30 cm.', 'notes/kamera.md')

    def context(self, query):
        return [record['id'] for record in
                self.store.context_for('claude', query, strict=True, budget_chars=2000)['records']]

    def test_possessive_question_finds_the_root_note(self):
        self.assertEqual(self.ids('benim boyum kaç cm?')[0], 'saglik')
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('benim boyum kaç cm?'), ['saglik'])
                self.assertEqual(self.context('BOYUM KAÇ CM'), ['saglik'])

    def test_a_word_the_vault_uses_does_not_fall_back(self):
        # durum is its own word, not dur plus a possessive: notes that say durum keep it apart.
        self.ingest('durum-1', 'Proje durumu: test aşamasında.', 'notes/durum-1.md')
        self.ingest('durum-2', 'Sunucu durum raporu hazır.', 'notes/durum-2.md')
        self.ingest('dur', 'Kırmızı ışıkta dur.', 'notes/dur.md')
        self.assertCountEqual(self.ids('durum'), ['durum-1', 'durum-2'])
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertCountEqual(self.context('durum'), ['durum-1', 'durum-2'])

    def test_a_word_of_its_own_stays_apart_where_the_root_is_more_common(self):
        # durum and verim are words, not dur and ver plus a possessive: a vault that says dur
        # or ver in more notes must still answer them with the notes that say durum and verim.
        self.ingest('dur-1', 'Zirkon rotasında dur.', 'notes/dur-1.md')
        self.ingest('dur-2', 'Zirkon yolunda dur.', 'notes/dur-2.md')
        self.ingest('durum', 'Zirkon durum raporu.', 'notes/durum.md')
        self.ingest('ver-1', 'Zirkon kartını ajana ver.', 'notes/ver-1.md')
        self.ingest('ver-2', 'Zirkon özetini Codex\'e ver.', 'notes/ver-2.md')
        self.ingest('verim', 'Zirkon verim ölçümü.', 'notes/verim.md')
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('zirkon durum'), ['durum'])
                self.assertEqual(self.context('zirkon verim'), ['verim'])

    def test_a_pronoun_does_not_fall_back(self):
        common = lambda stem: 9 if stem in ('bun', 'sun') else 0
        self.assertEqual(self.module._query_roots({'bunu', 'bunun', 'sunu'}, common), {})

    def test_one_root_answers_one_question_word(self):
        # A single "Boy" in the note must not pass the two-word gate for boyum and boyun.
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                self.assertEqual(self.context('boyum boyun'), [])

    def test_a_project_word_does_not_return_as_a_root(self):
        text = 'Boy projesi: zirkon seçildi.'
        (self.vault / 'notes').mkdir(exist_ok=True)
        (self.vault / 'notes/zirkon.md').write_text(text, encoding='utf-8')
        self.store.ingest({'id': 'zirkon', 'kind': 'note', 'status': 'active', 'visibility': 'internal',
                           'text': text, 'facts': {}, 'source': 'notes/zirkon.md', 'project': 'boy',
                           'updated_at': '2026-09-18T10:00:00Z'})
        for passages in (True, False):
            self.store.STRICT_PASSAGES = passages
            with self.subTest(passages=passages):
                found = self.store.context_for('claude', 'boyum zirkon', project='boy', strict=True,
                                               budget_chars=2000)['records']
                self.assertEqual(found, [])


class TurkishShortRootMatrixTest(VaultTestCase):
    def test_each_possessive_reaches_its_root(self):
        for index, (written, _) in enumerate(SHORT_ROOTS):
            self.ingest('kok-%02d' % index, written + ' hakkında kısa kayıt. ' + FILLER,
                        'notes/kok-%02d.md' % index)
        for index, (_, asked) in enumerate(SHORT_ROOTS):
            with self.subTest(query=asked):
                self.assertEqual(self.ids(asked), ['kok-%02d' % index])


class LegacyStateTest(VaultTestCase):
    """Tokens are computed per query, so a vault indexed before the stemmer needs no reindex."""

    LEGACY = {'facts': {}, 'id': 'eski-kayit', 'kind': 'note', 'revision': 1,
              'source': 'notes/eski.md', 'status': 'active', 'supersedes': [],
              'text': 'Dükkanın yayın kararı defterde duruyor.',
              'updated_at': '2026-09-01', 'visibility': 'internal'}

    def test_no_token_index_is_persisted(self):
        self.ingest('taze', 'Kütüphane kaydı.', 'notes/taze.md')
        with sqlite3.connect(self.store.database) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            payload = db.execute("SELECT payload FROM records WHERE id='taze'").fetchone()[0]
        db.close()
        self.assertEqual(tables - {'sqlite_sequence'}, {'records', 'receipts', 'metadata', 'events'})
        self.assertEqual(set(json.loads(payload)) & {'tokens', 'vocabulary', 'terms'}, set())

    def test_state_written_before_the_stemmer_answers_inflected_queries(self):
        source = self.vault / self.LEGACY['source']
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(self.LEGACY['text'], encoding='utf-8')
        record = dict(self.LEGACY, source_sha256=self.module.hashlib.sha256(
            source.read_bytes()).hexdigest())
        # Byte-for-byte the row the pre-stemmer release wrote: same schema, same payload.
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with sqlite3.connect(self.store.database) as db:
            db.execute('INSERT INTO records VALUES (?,?)', (record['id'], payload))
        db.close()
        reopened = self.module.MemoryStore(self.root / 'runtime', self.vault)
        self.addCleanup(lambda: evaluator.close_store(reopened))
        found = reopened.retrieve('dükkanlardaki yayınlar')
        self.assertEqual([item['id'] for item in found['records']], ['eski-kayit'])


if __name__ == '__main__':
    unittest.main()
