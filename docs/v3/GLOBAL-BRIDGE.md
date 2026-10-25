# Vault dışındaki oturumlar için opsiyonel köprü

Normal V3 kurulumu yalnız vault içindeki hook ayarlarını yönetir. Başka proje
klasörlerindeki oturumları göremez. [Issue #41](https://github.com/avenoxai/avenoxbeyin/issues/41)
bu sınırı ve eksik proje kökenini bildirdi. Paketle gelen bu sarmalayıcı açıkça
seçilen proje klasörleri için bağlantı sağlar; varsayılan kurulum değişmez.

## Kurulum

Bu dosyayı içeren V3 paketini kurduktan sonra köprüyü doğrudan `beyin.py` üzerinden otomatik kurabilir veya konfigürasyonu inceleyebilirsiniz:

### 1. Otomatik Kurulum (`--install`)

```sh
# Antigravity için otomatik kurulum (hooks.json ve GEMINI.md işaretçisi):
py -3 beyin.py bridge --harness antigravity --project-root "C:\Projelerim" --install

# Claude Code için:
python3 beyin.py bridge --harness claude --project-root "/tam/yol/Projelerim" --install

# Codex için:
python3 beyin.py bridge --harness codex --project-root "/tam/yol/Projelerim" --install
```

Windows'ta `python3` yerine `py -3` ve gerçek Windows yollarınızı kullanın.
State, kurulu `.beyin-runtime.json` dosyasından bulunur; özel kullanımda `--state`
ile verilebilir. `--project-root` tekrarlanabilir ve alt klasörleri kapsar.
Tüm home/disk yerine gerçekten istediğiniz proje köklerini seçin.

Otomatik kurulum (`--install`):
- İlgili harness'in global ayar dosyasını otomatik tespit eder ve köprü hook'larını güvenle ekler:
  - Claude Code: `~/.claude/settings.json` (özel `CLAUDE_CONFIG_DIR` varsa o dizin).
  - Codex: `$CODEX_HOME/hooks.json` (varsayılan `~/.codex/hooks.json`).
  - Antigravity: `~/.gemini/config/hooks.json`.
- **Otomatik `.bak` yedeği**: Var olan herhangi bir konfigürasyon dosyası değiştirilmeden önce yanına `.bak` uzantılı yedeği kaydedilir (örn. `hooks.json.bak`, `settings.json.bak`).
- **Antigravity `GEMINI.md` işaretçisi**: `--harness antigravity --install` çalıştırıldığında, hook kaydına ek olarak kullanıcı ana dizinindeki `~/.gemini/config/GEMINI.md` dosyasına vault konumunu ve temel yönergeleri işaret eden idempotent bir blok (`<!-- beyin-v3-bridge:start --> ... <!-- beyin-v3-bridge:end -->`) eklenir veya güncellenir. Mevcut dosya varsa önce `GEMINI.md.bak` olarak yedeklenir.

Bozuk JSON veya beklenmeyen hook listesi biçimi korunur; kurulum hata vererek durur.
Antigravity yönergesi de yalnız seçilen proje köklerinde geçerlidir; Companion
dosyalarını otomatik okuma veya değiştirme talimatı vermez.

Global istemci ayarları vault güncellemesinin rollback kapsamına girmez.
Antigravity harness adı ve receipt biçimi 3.11.0'da zaten desteklenir; bu değişiklik
yeni bir harness veya hariç tutma bileşeni kaydetmez. 3.11.0'a dönüldüğünde mevcut
köprü komutları çalışabilir, ancak yeni `bridge --install` / `--uninstall` komutları
bulunmaz. Global köprüyü de kaldırmak istiyorsanız rollback öncesinde
`python3 beyin.py bridge --harness antigravity --uninstall` çalıştırın.

### 2. Manuel İnceleme ve Yazdırma (`--print-config`)

Dosyaları diske yazmadan yalnızca üretilecek JSON konfigürasyonunu görmek isterseniz `--print-config` kullanabilirsiniz:

```sh
python3 "/tam/yol/Beynim/beyin.py" bridge --harness claude \
  --project-root "/tam/yol/Projelerim" --print-config
```

Bu mod dosyaları değiştirmez. Üretilen `hooks` olay gruplarını ilgili global
dosyanın mevcut olay listelerine manuel olarak ekleyebilirsiniz.
Önce mevcut ayarı yedekleyin. Dosyanın tamamını bu çıktıyla değiştirmeyin; diğer
ayarları ve hook'ları koruyun. Aynı köprüyü iki kez eklemeyin; bir proje için tek
hedef Beyin seçin. Komutlar mevcut Python ve kurulu scriptin mutlak yolunu taşır;
Windows'ta PowerShell encoded argümanları kullanılır. Başka bilgisayara taşırken
orada yeniden üretin. Codex sürümünüzün hook özelliğini etkinleştirin, `/hooks`
güven incelemesini tamamlayın ve istemcide yeni oturum açın. Araç güven hash'i veya
kullanıcı onayı üretmez.

JSON biçimi 21 Eylül 2026'da [Codex hooks](https://developers.openai.com/codex/hooks)
ve [Claude hooks](https://code.claude.com/docs/en/hooks) birincil belgeleriyle
karşılaştırıldı. Antigravity biçimi (adlandırılmış hook, `PreInvocation` ve `Stop` için
düz liste, `conversationId` ve `workspacePaths` alanları, komutun `hooks.json`
klasöründe çalışması) Antigravity CLI 1.2.16 ikilisine gömülü `hooks.json` belgesinden
alındı ve 10 Ekim 2026'da aynı sürümle Windows 11'de gerçek bir oturumda denendi;
1.2.3'e (macOS) gömülü belge de aynı biçimi tarif eder. macOS ve Linux'ta canlı teslim
denenmedi. CLI simülasyonu gerçek Desktop hook teslimi kanıtı değildir;
kurulumdan sonra her istemcide aşağıdaki geri okumayı yapın.

## Kapsam ve ayrı bütçe

- Yalnız `SessionStart`, `Stop`, `PreCompact`, `SessionEnd` iletilir.
  `UserPromptSubmit` ve `PostToolUse` hiç iletilmez.
- `--event SessionStart --event SessionEnd` ile olay kümesi daraltılabilir.
- Antigravity'de `PreCompact` ve `SessionEnd` yoktur. Vault adaptörüyle aynı sınırlar kullanılır:
  `PreInvocation` yalnız `invocationNum: 0` iken `SessionStart`, `Stop` yalnız `fullyIdle: true`
  iken `Stop` sayılır. Hook komutu `hooks.json` klasöründe çalıştığı için proje, payload'daki ilk
  `workspacePaths` girdisidir; işlem çalışma dizinine düşülmez. Çalışma alanlarından biri vault
  veya kurulu başka bir vault ise köprü sessiz kalır, olaylar o vault'un yerel hook'larına kalır.
- `--context-chars 1500` başlangıç talimatı sınırıdır (0–4000). `0` enjeksiyonu
  kapatır. Tam talimat sığmazsa kesik komut yerine boş çıktı verilir.
- Başlangıçta proje etiketi, receipt oturum kimliği, mutlak CLI yolu ve kaynaklı
  kayıt talimatı verilir. Companion, Last-Session, eski receipt veya kişisel not
  gövdesi harici projeye otomatik taşınmaz. Ajan gerektiğinde açık proje
  filtresiyle `context` çağırır. Issue'daki geçici sarmalayıcıdan bu noktada
  bilinçli olarak daha dar bir davranış seçildi.
- İstisna, isteğe bağlı `preferences --project-context on` ayarıdır: açıkken
  aynı projenin en yeni receipt özeti ve tarihi gelmiş görevleri kısa bir blok
  olarak eklenir, başka projelerden yalnız sayı gelir. Kapılar ve sınırlar
  [tercihler belgesinde](PREFERENCES.md#projeyi-tanıyan-oturum-başı).
- Yerel `auto_sync: false` / manual profil köprüyü susturur; `context_mode: off`
  başlangıç metnini susturur. Global seçenekler yerel tercih dosyasını değiştirmez.
  Mevcut kontrol aralığı korunur.
- Vault, alt klasörleri ve kendi `.beyin-runtime.json` dosyası olan başka
  vault'lar atlanır. Gerçek yollar çözülür; symlink ile kapsam dışına çıkılmaz.
  Ayrı vault kurulumu olmadan özel yerel hook bağladıysanız aynı projeye global
  köprüyü ayrıca bağlamayın.
- Cwd önceliği: payload, Claude için `CLAUDE_PROJECT_DIR`, işlem çalışma dizini
  (Antigravity için yalnız ilk `workspacePaths` girdisi).
  Geçersiz/izin dışı cwd ve eksik oturum kimliği kayıt oluşturmaz.
- Kuyruk yalnız olay/harness/oturum hash'i, sınırlı proje etiketi, proje yolunun
  hash'i ve zamanı saklar. Tam cwd, prompt ve transcript saklanmaz. Aynı adlı
  klasörler hash ile ayrılır. Proje etiketi yerel metaveridir; sır içeren klasör
  adı kullanmayın. `no_memory: true` ve iç iş işareti kayıt oluşturmaz.
- Jev çağrısı, API anahtarı veya yeni daemon gerekmez. Global başlangıç otomatik
  update kontrolü de başlatmaz. Mevcut yerel worker senkronizasyonu kullanılır.

## Kaynaklı kayıt ve doğrulama

İş bitince ajan mevcut yetkisi kapsamında vault'ta sonuç kaynağı oluşturur;
hook'un verdiği `session` ile aşağıdaki biçimde receipt gönderir:

```json
{"event_id":"benzersiz-is-sonucu","summary":"Kaynakta belgelenen sonuç.",
 "refs":["notes/proje-sonucu.md"],"session":"HOOKUN_VERDIGI_OTURUM_HASHI"}
```

```sh
python3 "/tam/yol/Beynim/beyin.py" receipt --harness claude --file receipt.json --json
python3 "/tam/yol/Beynim/beyin.py" doctor --json
```

`refs` var olan vault-relative kaynaklara gider. Harici repo dosyalarını veya
konuşmaları sırf hook çalıştı diye kopyalamayın. Checkpoint yalnız olayın
görüldüğünü gösterir; iş sonucu veya görev tamamlanması değildir.
`doctor --json` içindeki `receipt-gaps.json.checkpoints` alanında proje etiketi
ve `project_id` bulunur. Prompt olayları alınmadığı için her turun eksiksiz
receipt kapsamı iddia edilmez. Ajan kayıt yazmadan kapanırsa worker özet uydurmaz.

Vault'un kendi Antigravity hook'u (`.agents/hooks.json`) da proje etiketini aynı kuralla,
ilk `workspacePaths` girdisinden alır. 3.9.0 ve öncesinde vault içindeki Antigravity
oturumları hook klasörünün adıyla (`.agents`) etiketleniyordu. Oturum kimliği değişmediği
için süren bir oturum bölünmez; etiketi ve `project_id` değeri ilk yeni olayda güncellenir,
yeni olay göndermeyen eski satırlar `.agents` olarak kalır. İlk çalışma alanı vault
dışındaki bir proje olan vault oturumu o projenin `project_id` değerini taşır (Claude ve
Codex'te payload `cwd` alanı için de böyledir); `--project-context on` iken bu oturumun
receipt özeti o projenin oturum başında görünebilir.

İzole testler üç harness, kapsam/çift tetiklenme korumaları, proje kökeni,
idempotence, eski checkpoint tablosu geçişi, receipt ile gap kapanması ve gerçek
kurulu ZIP'ten üretilen komutun harici cwd'de yürütülmesini kapsar. Kişisel global
ayarlar testlerde değiştirilmez. Gerçek istemcide yeni oturum açıp `doctor`
ile olay görüldüğünü ayrıca doğrulayın.

## Kapatma ve geri alma

Köprüyü kaldırmak için doğrudan `--uninstall` komutunu çalıştırabilirsiniz:

```sh
# Antigravity köprüsünü ve GEMINI.md işaretçisini temizleme:
py -3 beyin.py bridge --harness antigravity --uninstall

# Claude Code için:
python3 beyin.py bridge --harness claude --uninstall

# Codex için:
python3 beyin.py bridge --harness codex --uninstall
```

Otomatik kaldırma (`--uninstall`):
- İlgili global ayar dosyasını (`hooks.json` veya `settings.json`) bularak yalnızca `beyin_v3_bridge` ile ilgili hook tanımlarını siler; diğer tüm kullanıcı ayarlarını ve özel hook'larını korur.
- Değişiklik öncesinde var olan dosyanın `.bak` yedeğini alır.
- Antigravity için `~/.gemini/config/GEMINI.md` içerisindeki köprü bloğunu (`<!-- beyin-v3-bridge:start --> ... <!-- beyin-v3-bridge:end -->`) güvenle temizler; dosya başka metin içermiyorsa dosyayı da siler.

Manuel kaldırma durumunda, global ayardan yalnız eklenen köprü handler'larını kaldırıp yeni oturum açın;
diğer handler'ları koruyun. Geçici susturma için manual profil kullanılabilir;
bu yerel otomatik işleri de durdurur.

Güncelleme sarmalayıcıyı diğer yönetilen runtime dosyalarıyla günceller; global
ayarlar kullanıcıya ait kalır. Köprüyü içermeyen eski sürüme rollback veya
uninstall yapmadan **önce global handler'ları kaldırın** (`--uninstall`). Olmayan scripti global
hook'ta bırakmak istemci hatası üretir. Yeniden etkinleştirirken güncel JSON'u
üretin ve gerekli güven incelemesini tamamlayın.
