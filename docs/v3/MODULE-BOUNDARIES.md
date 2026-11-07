# V3 kaynak ve dağıtım sınırları

Bu envanter [#501](https://github.com/avenoxai/avenoxbeyin/issues/501) için
hazırlık kaydıdır. İncelenen taban `0be8f9961e3bef7a312735cd7461c66ecdca760a`
(V3.12.0). Aşağıdaki yollar ve semboller bu kaynaktaki mevcut davranışı gösterir;
önerilen ayrıştırma sınırları kabul edilmiş bir yeni mimari değildir.
Katkı akışı ve test kuralları için tek kaynak [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Girişler ve sorumluluklar

| Kaynak | Mevcut sorumluluk / çağrı yüzeyi | Ayrıştırmadan önce korunacak sınır |
| --- | --- | --- |
| [`scripts/beyin_entry.py`](../../scripts/beyin_entry.py) | Kurulu `beyin.py`; `main`, `load_cli`, `human_result` | CLI JSON/stdout, insan çıktısı ve eski Windows kod sayfası davranışı; kurulu CLI dinamik yüklenir. |
| [`scripts/beyin_v3.py`](../../scripts/beyin_v3.py) | CLI dispatch, state çözümleme, `hook_reconcile`; kurulumda `beyin_v3_cli.py` olur | Runtime çekirdeğiyle aynı isimli kaynak dosyası değildir. Komutlar, argümanlar, çıkış kodları ve hook uzlaştırması korunmalı. |
| [`scripts/install_v3.py`](../../scripts/install_v3.py) | `install` / `_install`: yönetilen dosyalar, istemci hook/skill kurulumu, migration ve update journal | ZIP içinden bağımsız çalışır; kullanıcı bölgeleri, exclusions, eski kurulumların sahipliği ve rollback korunmalı. |
| [`beyin_v3.py`](../../template/.claude/scripts/beyin_v3.py) | `MemoryStore`, state ve retrieval/context işlemleri | SQLite/Markdown sınırı, kamusal semboller, bağlam bütçesi ve privacy/trust filtreleri. |
| [`beyin_v3_sync.py`](../../template/.claude/scripts/beyin_v3_sync.py) | `SyncEngine`, `parse`, `render`, `atomic`; tarama/imza, receipt, görev ve not işlemleri | Core, index, hygiene, projections, preferences, secrets ve notes modüllerini kullanır. Parser, tarama ve receipt ayrımları adaydır; transaction sahipliği tek kalmalı. |
| [`beyin_v3_yakala.py`](../../template/.claude/scripts/beyin_v3_yakala.py) | URL/taşıma güvenliği, kart/ek saklama, çıkarıcılar, platform yakalama, popup, listener ve `install` | Vault dışındaki tek dosya listener çalışabilmeli; aşağıdaki dağıtım kararı alınmadan modül taşıma yapılamaz. |
| [`beyin_v3_projections.py`](../../template/.claude/scripts/beyin_v3_projections.py) | Receipt görünümleri, coverage/gaps, quarantine ve knowledge freshness | Türetilmiş görünüm sahipliği, kaynağın gerçekliği ve receipt izolasyonu. |
| [`beyin_v3_hygiene.py`](../../template/.claude/scripts/beyin_v3_hygiene.py) | Boundary, dosya boyutu, inbox/knowledge denetimleri ve görev raporları | Salt okunur denetim yolları; mevcut preferences/exclusions modülleriyle ikinci bir ayar otoritesi oluşturulmamalı. |
| [`beyin_v3_hook.py`](../../template/.claude/scripts/beyin_v3_hook.py) | Hook protokolü, bağlam/receipt hatırlatması, kuyruk | Gecikme bütçesi, fail-open ve no-memory davranışı; `beyin_v3_hook_done.py` ile mevcut ayrım. |

`beyin_v3_notes.py`, `beyin_v3_passage.py`, `beyin_v3_index.py`,
`beyin_v3_references.py`, `beyin_v3_preferences.py`, `beyin_v3_exclusions.py`
ve istemci adaptörleri zaten ayrı sorumluluk taşır. Yeni yardımcılar bunları
kopyalamadan kullanmalı. Benzer `default_state` veya atomik yazma kodu,
bağımsız çalıştırma ve güven sınırları incelenmeden tekleştirilmemeli.

## Dağıtım zinciri

1. [`build_v3_release.py:build`](../../scripts/build_v3_release.py) üç giriş
   betiğini, düz `template/.claude/scripts/beyin_v3*.py` dosyalarını ve üç temel
   skill'i ZIP'e alır. Schema-1 manifest her dosyanın SHA-256 değerini taşır.
2. [`beyin_v3_update.py:allowed`](../../template/.claude/scripts/beyin_v3_update.py)
   runtime adlarını `beyin_v3(?:_[a-z]+)*.py` ile sınırlar. Build glob'una uymak
   tek başına updater uyumluluğu değildir; alt paket veya keyfî yardımcı dosya
   eski updater tarafından reddedilebilir.
3. Installer runtime'ı `.claude/scripts/` içine, geliştirme CLI'sini
   `beyin_v3_cli.py` olarak, giriş betiğini vault köküne `beyin.py` olarak kurar.
   Kullanıcının Markdown kaynakları bu yönetilen runtime ile karıştırılmaz.
4. Yakala `install()` macOS, Linux ve Windows yollarında yalnız kendi betiğini
   `state/yakala/` altına kopyalar (`shutil.copy2(script, runner)`). Vault'taki
   kardeş bir modülü import etmek, bu kopyanın yanında o modülün bulunacağını
   garanti etmez. `listen_mac` / `_hand_over` güncel vault kopyasına geçebilse de
   başlangıç yolu tek başına açılabilmelidir.

Yakala için **üretilen tek dosyalık bootstrap** ile **atomik runtime paketi**
arasında karar açık. Karar; eski kısayol, vault'un bulunamaması, çoklu vault,
offline kullanım, yarıda kesilen kurulum, upgrade ve rollback'i kapsamalı.
Bu belge bu seçeneklerden birini seçmez. `src/` kaynak paketine geçiş de eski
updater → ZIP → installer zinciri kanıtlanmadan yapılacak bir dosya taşıması değildir.

## Taşıma öncesi kanıt ve geçici çıktılar

- `tests/v3_signature_contract_test.py` ve `tests/v3_adjacent_import_test.py`
  kamusal imza ve yan modül yükleme sözleşmelerini korur. Yalnız yeniden export
  etmek, eski `patch.object(original_module, ...)` çağrılarının helper içindeki
  global çözümlemesini aynı tutmayabilir; ilgili patch noktası ayrıca sınanmalı.
- [`tests/v3_yakala_standalone_cli_test.py`](../../tests/v3_yakala_standalone_cli_test.py)
  yalnız kopyalanmış tek dosyayı izole Python süreçlerinde çalıştırır: çevrimdışı
  metin yakalama → listeleme → işleme → bitirme ve vault yokken salt okunur
  yardım/boş listeleme. GUI, listener, yerel platform kabulü veya dağıtım
  mimarisi kararı için kanıt değildir.
- Mevcut keşif `python -m unittest discover -s tests -p "v3_*test.py"`;
  test alt dizinine taşıma keşfi değiştirebilir. V3, release ve Jev workflow
  path filtreleri de dosya taşımasıyla birlikte değerlendirilmelidir.
- Kaynak bazlı testlerin yanında gerçek ZIP kurulumu, eski sürümden upgrade,
  rollback ve recovery gerekir; Windows/macOS/Linux sonucu ayrı belirtilir.
- Geçici yerel ajan çıktıları kökteki `.agent-work/` altında tutulabilir ve Git
  tarafından yok sayılır. Bu ignore kuralı mevcut takipli dosyaları silmez.
- `tests/fixtures/v3/`, frozen değerlendirme girdileri, karar kalitesi kanıtları
  ve tarihî raporlar bu PR'da taşınmaz/silinmez. Ayrı bir dosya bazlı envanter,
  referans ve yeniden üretilebilirlik kontrolü yapılmadan toplu temizlik yapılmamalı.

Bu hazırlık kaydı runtime refactor'unun tamamlandığı, yeni dağıtım tasarımının
onaylandığı veya platform testlerinin çalıştırıldığı anlamına gelmez.
