# git-devir: makineler arası git devri

Aynı repolarda birden çok makinede (şirket PC'si, MacBook, ev PC'si) çalışırken
"kapatırken commit + push, açarken pull" işini otomatik yapan Claude Code hook'u.
Tek dosya: [`git_devir.py`](git_devir.py). Yalnız Python standart kütüphanesi; Windows, macOS ve
Linux'ta çalışır.

Bu klasörde iki hook var: **git-devir** (bu bölüm) ve **proje** (repo → vault projesi, oturum
kartları, kararlar; [aşağıda](#proje-proje-çözücü-oturum-kartları-ve-kararlar)).

Durum: **yazıldı ve test edildi, henüz hiçbir makineye kurulmadı.**

## Ne yapar?

### Oturum kapanırken (`SessionEnd` → `git_devir.py end`)

Hook 1 saniyeden çok daha kısa sürede döner: arka planda bağımsız bir işçi başlatır ve çıkar.
İşçi sırasıyla şunları yapar:

1. **Yarım işin yedeği (wip):** commit edilmemiş her şeyin (staged, unstaged, yeni dosyalar)
   anlık görüntüsünü alır ve `wip/<makine>/<dal>` uzak dalına koyar.
   - Geçici bir index kullanır (`GIT_INDEX_FILE`); senin dalın, index'in ve dosyaların **hiç
     değişmez**. `.gitignore` geçerlidir.
   - Commit mesajı: `wip(<makine>): <dal> <tarih>`, ebeveyni o anki `HEAD`.
   - Çalışma alanı `HEAD` ile aynıysa yedek alınmaz.
   - `max_file_mb` (varsayılan 50 MB) üstü dosyalar yedeğe girmez; bir sonraki açılışta haber verilir.
   - Gizli olabilecek dosya varsa (`.env`, `.env.*` (`.env.example` hariç), `*.pem`, `*.key`,
     `id_rsa*`, `*.p12`, `*.pfx`, `credentials*.json`, `secrets.*`) yedek **iptal edilir** ve
     haber verilir.
2. **Commit'lenmiş işin push'u:** dalın upstream'i varsa ve dal yalnız **ileride** ise (geride
   değil) düz `git push` yapar. Ayrışmışsa veya push reddedilirse yalnız not bırakır.
3. **Temizlik:** çalışma alanı temiz ve her şey push edilmişse bu makinenin kendi
   `wip/<makine>/<dal>` dalını siler.

### Oturum açılırken (`SessionStart` → `git_devir.py start`)

Toplam bütçe yaklaşık 5 saniye.

1. `git fetch origin` (en çok 4 sn). Bitmezse bunu tek satırda söyler; fetch arka planda sürer.
2. Çalışma alanı temiz ve dal yalnız **geride** ise fast-forward ile çeker. Değilse (geride ama
   değişiklik var, ayrışmış, push edilmemiş commit var) yalnız uyarır ve çalıştırılacak komutu yazar.
3. Başka bir makinenin `origin/wip/<diğer-makine>/<dal>` dalı yerel `HEAD`'den yeniyse ve içeriği
   farklıysa modele söyler: *"Başka makinede (`ev`) yarım iş var … almak için kullanıcıya sor."*
   **Asla kendiliğinden uygulamaz.**
4. Önceki kapanışlardan kalan notları gösterir ve siler.
5. Söyleyecek bir şey yoksa hiçbir şey yazmaz. Söylediği her şey en çok 800 karakterdir.

## Hangi repolarda çalışır?

Aşağıdakilerin **hepsi** doğruysa:

- Klasör, `origin` remote'u olan bir git reposunun içinde.
- Repo kökü ayardaki `project_roots` köklerinden birinin altında.
- Repo Obsidian vault'unun içinde değil (`vault_path`) ve kökünde `beyin.py` yok.
- Repo kökünde `.git-devir-off` dosyası yok.
- `GIT_DEVIR_OFF=1` ortam değişkeni ayarlı değil.

Rebase, merge, cherry-pick, revert veya bisect sürüyorsa ya da `HEAD` bağımsızsa (detached)
hiçbir şey yapmaz, yalnız not bırakır.

### Tam otomatik ve yalnız uyarı

- `origin` adresi (https veya ssh) `sahip/repo` olarak çözülür.
- Sunucu `auto_hosts` (varsayılan `github.com`) içinde **ve** sahip `auto_owners` (varsayılan
  `kutluege`) içindeyse repo **tam otomatiktir**: wip yedeği, push, pull.
- Diğer bütün remote'lar (ör. şirketin git sunucusu) **yalnız uyarı** modundadır: push yok,
  pull yok, wip yok; yalnız "push edilmemiş commit var / gerisindesin" notları.

## Güvenlik garantileri

- **Dalını asla force-push etmez.** Force yalnız bu makinenin kendi `wip/<makine>/<dal>`
  dalına yapılır; dalın kendisi yalnız düz `git push` ile, yalnız ileri giderken gönderilir.
- **Çalışma alanına, index'e ve dalına dokunmaz** (yedek geçici index ile alınır). Açılışta tek
  değişiklik, temiz bir dalın fast-forward edilmesidir.
- **Başka makinenin yarım işini asla otomatik uygulamaz**; yalnız haber verir.
- **Şirket (otomatik olmayan) remote'ları yalnız uyarı** alır.
- Her ağ çağrısı zaman sınırlıdır (fetch 4 sn, push 30 sn) ve `GIT_TERMINAL_PROMPT=0`,
  `GCM_INTERACTIVE=never` ile çalışır: şifre sormaz, takılmaz. Hatalar not olur.
- Hook oturumu asla bozmaz: her hata yutulur, çıkış kodu her zaman 0.
- Notlar ve log makinede `~/.claude/git-devir/` altında durur, hiçbir repoya yazılmaz.

## Kurulum

Önerilen yol tek komuttur: [`ege/kur.py`](../kur.py) git-devir'i, proje hook'unu, `beyin-yazici`
ajanını ve global kuralı birlikte kurar ([../KURULUM.md](../KURULUM.md)). Yalnız git-devir'i kurmak
istersen, her makinede bir kez (`<...>` kısımlarını o makineye göre doldur):

```sh
python ege/hooks/install_git_devir.py --machine <ad> --project-root <repo-koku> --vault <vault-yolu> --dry-run
python ege/hooks/install_git_devir.py --machine <ad> --project-root <repo-koku> --vault <vault-yolu>
```

- `--machine`: kısa etiket (`is-5090`, `ev`, `mac` …), hostname **değil**.
- `--project-root` birden çok kez verilebilir.
- İsteğe bağlı: `--auto-owner`, `--auto-host` (tekrarlanabilir), `--max-file-mb`, `--python`.
- Önce `--dry-run` ile ne yazılacağına bak; hiçbir dosya değişmez.
- Kurulum `~/.claude/git-devir.json` dosyasını yazar ve `~/.claude/settings.json` içine bir
  `SessionStart` ve bir `SessionEnd` girdisi ekler. Önce zaman damgalı yedek alır
  (`settings.json.git-devir-<tarih>.bak`), var olan bütün hook'ları (avenox köprüsü dahil) korur.
  Tekrar çalıştırmak bir şey değiştirmez.
- `CLAUDE_CONFIG_DIR` ayarlıysa `~/.claude` yerine o klasör kullanılır.
- Hook, kurulumu yapan Python ile ve bu klasördeki `git_devir.py` ile çalışır; repo klasörünü
  taşırsan kurulumu tekrar çalıştır.

Kaldırma (yalnız git-devir girdilerini siler, diğer hook'lara dokunmaz):

```sh
python ege/hooks/install_git_devir.py --uninstall
```

`git-devir.json` ve `~/.claude/git-devir/` yerinde kalır; istersen elle sil.

Tek bir repoda kapatmak için kökünde `.git-devir-off` dosyası oluştur (gerekirse `.gitignore`'a
ekle). Bir oturum için kapatmak: `GIT_DEVIR_OFF=1`.

## Ayar dosyası: `~/.claude/git-devir.json`

| Alan | Anlamı | Varsayılan |
|---|---|---|
| `machine` | Bu makinenin kısa etiketi; wip dalının adında kullanılır | (zorunlu) |
| `project_roots` | Repoların durduğu kökler (liste) | (zorunlu) |
| `vault_path` | Obsidian vault'u; içindeki repolara dokunulmaz | boş |
| `auto_owners` | Tam otomatik sahipler | `["kutluege"]` |
| `auto_hosts` | Tam otomatik sunucular | `["github.com"]` |
| `max_file_mb` | wip yedeğine girecek en büyük dosya (MB) | `50` |
| `remote_aliases` | İleri düzey/test: `{"<remote adresi>": "<politikada kullanılacak adres>"}` | `{}` |
| `python`, `proje_py` | `ege/kur.py` yazar: hook'ları çalıştıran Python ve `proje.py` yolu; `beyin-yazici` buradan okur | (yalnız `kur.py` kurulumunda) |

Dosya yoksa veya bozuksa hook hiçbir şey yapmaz.

## Başka makinenin yarım işini almak

Açılışta "Başka makinede (`ev`) yarım iş var" denirse, önce bak:

```sh
git diff HEAD origin/wip/ev/main
```

Almak için iki yol (ikisi de commit atmaz, değişiklikler çalışma alanına gelir):

```sh
# 1) Dosyaları olduğu gibi getir (aynı dosyalardaki yerel değişikliklerin üstüne yazar)
git checkout origin/wip/ev/main -- .

# 2) Birleştirerek getir (çakışma olursa normal merge gibi çözülür)
git merge --squash origin/wip/ev/main
```

Sonra normal commit at. O makine bir sonraki temiz kapanışında kendi wip dalını siler.
Not: 1. yol wip'te silinmiş dosyaları silmez.

## Bilinen sınırlar

- Terminal penceresi zorla kapatılırsa (pencereyi kapatma, işlem öldürme, elektrik) `SessionEnd`
  çalışmayabilir. Bir sonraki açılış yine "push edilmemiş commit var" diye uyarır; commit
  edilmemiş değişiklik ise o makinede durur.
- Push için etkileşimsiz kimlik bilgisi gerekir (SSH anahtarı veya kayıtlı credential helper).
  Şifre gerekiyorsa push sessizce başarısız olur ve bir sonraki açılışta not olarak görünür.
- Upstream'i olmayan yeni dallar push edilmez (wip yedeği yine alınır); not `git push -u` komutunu söyler.
- Açılıştaki fast-forward, uzak dalı yeni fetch edilmiş bilgiyle `git merge --ff-only @{u}` olarak
  yapar (ikinci bir ağ çağrısı olmasın diye `git pull --ff-only` yerine). Takip edilmeyen
  (untracked) dosyalar pull'u engellemez; git onların üstüne yazmayı zaten reddeder.
- `compact` ile başlayan oturumlarda açılış adımı atlanır.

# proje: proje çözücü, oturum kartları ve kararlar

Tek dosya: [`proje.py`](proje.py). Açılan repoyu vault'taki proje klasörüne bağlar, o projede
"nerede kalmıştım" bilgisini oturum başında verir ve kayıt yazıcısının (`beyin-yazici` alt ajanı,
[../agents/beyin-yazici.md](../agents/beyin-yazici.md)) kullandığı komutları sağlar.
Ayar dosyası, kapılar ve remote çözümü git-devir ile ortaktır (`git_devir.py` içinden kullanılır).

Durum: **yazıldı ve test edildi, henüz hiçbir makineye kurulmadı.** Kurulum: [../KURULUM.md](../KURULUM.md).

## Proje kimliği ve vault klasörü

- Kimlik `origin` remote'undaki **`sahip/repo`**'dur (https ve ssh adresleri aynı sonuca çözülür);
  görünen ad repo adıdır. Remote yoksa ya da çözülemiyorsa kimlik `local/<klasör adı>` olur.
  Klasör yolu kimliğe girmez; aynı repo her makinede aynı projedir.
- Vault klasörü şu sırayla aranır:
  1. **`Proje.md` bağı:** `30 Projeler/*/Proje.md` dosyalarından frontmatter'ında
     `proje_id: sahip/repo` yazan klasör (büyük/küçük harfe duyarsız). Dosyayı `init` bir kez
     yazar; klasörün adı sonradan değişse de bağ klasörle birlikte gider. Bağ Markdown olduğu için
     Obsidian Sync onu her makineye taşır (`.json` dosyalarını varsayılan ayarda taşımaz).
     Aynı `proje_id` iki klasörde yazıyorsa seçim yapılmaz, kullanıcıya sorulur.
  2. **Ad eşleşmesi:** `30 Projeler/` altında adı repo adına eşit olan ve `Proje.md`'si başka bir
     kimlik söylemeyen klasör. Karşılaştırma büyük/küçük harfe, boşluk, `-` ve `_` farkına bakmaz;
     `I İ ı i` tek harf sayılır. Bu tutmazsa ikinci denemede Türkçe işaretler de yok sayılır
     (`ogrenme-kocu` ↔ `Öğrenme Koçu`). Başka bir kimliğe bağlı klasör ad eşleşmesine girmez (iki
     farklı sahipte aynı adlı repo karışmaz). Birden çok klasör eşleşirse seçim yapılmaz, sorulur.
  3. Bulunamazsa proje **çözülmemiş** sayılır. Hook klasör açmaz; modele "kullanıcıya sor" der.
- Kullanıcının "hayır, klasör açma" cevabı (`sustur`) vault'a yazılmaz; yalnız o makinede
  `~/.claude/git-devir/proje/sustur.json` içinde durur. Yani her makine bir kez sorar. Klasör başka
  makinede açılıp Sync ile gelirse yerel "hayır" yok sayılır.
- Geriye dönük: vault'ta eski bir `_Sistem/projeler.json` varsa 1 ile 2 arasında yalnız **okunur**;
  hiçbir komut onu yazmaz.

## Klasör düzeni

```text
30 Projeler/<ad>/
  Proje.md                               # proje_id: sahip/repo (bir kez yazılır, repo ↔ klasör bağı)
  Oturumlar/2026-11-16-1742-is-5090.md   # iş başına YENİ kart: YYYY-AA-GG-SSDD-<makine>.md
  Oturumlar/2026-11-16-2210-ev.md
  Kararlar.md                            # en yeni karar üstte
  Kurallar.md                            # yalnız bu projenin kuralları
🔮 850-Companion/Kurallar.md             # avenox'un genel kuralları + proje başına bir işaretçi satırı
```

- `Proje.md` frontmatter'ı: `proje_id`, `ad` (repo adı), `olusturma` (tarih); gövde tek satır.
  Proje başına bir kez yazılan yeni dosya olduğu için "yakalama makineleri yalnız yeni dosya
  ekler" kuralına uyar.
- **Kart, tek bir `Durum.md` dosyasının yerini alır.** Her makine yalnız yeni dosya ekler, hiçbir
  makine başkasının kartını düzenlemez; bu yüzden Obsidian Sync'te çakışma olmaz.
  "Kaldığım yer" = dosya adına göre en yeni kart.
- Kart içeriği (gövde en çok 1200 karakter; aşan kart yazılmaz, kısaltılması istenir):
  frontmatter `proje`, `proje_id`, `makine`, `tarih`, `dal`, `commit` (varsa kısa sha); gövde
  `Hedef`, `Yapılan`, `Açık adımlar`, `Sonraki somut adım`, varsa `Kararlar`.
- Aynı dakikada aynı makineden ikinci kart `-2`, `-3` … ekiyle yazılır.
- `Kararlar.md` girdisi:

  ```markdown
  ## 2026-11-16 · Karar metni
  - Neden: …
  - Alternatifler: …
  - Makine: is-5090
  ```

  Yeni girdi ilk girdinin önüne eklenir; dosyanın geri kalanı bayt bayt aynı kalır. Dosya okuma ile
  yazma arasında değiştiyse (sha256 farklıysa) yazılmaz ve bildirilir.
- Kural (`proje.py kural`): `- 2026-11-16 · kural metni` biçiminde tek madde, en çok 400 karakter.
  - Proje kuralı projenin `Kurallar.md` dosyasının sonuna eklenir. Ayrıca avenox'un genel
    `Kurallar.md` dosyasında o proje için işaretçi satırı yoksa bir kez eklenir:
    `- [kapsam: <klasör adı>, <repo adı>] <ad> işinde önce 30 Projeler/<ad>/Kurallar.md dosyasını oku.`
    (avenox bu etiketi yalnız o projenin oturumlarında bağlama alır).
  - `--genel` ile kural avenox'un genel `Kurallar.md` dosyasına gider (`## Kurallar` bölümü varsa
    onun sonuna, yoksa dosyanın sonuna).
  - Aynı metin ikinci kez yazılmaz. `karar` ile aynı sha256 koruması geçerlidir; dosyanın geri
    kalanı değişmez. İşaretçi eklenemezse kural yine yazılmış sayılır ve durum bildirilir.

## Oturum açılırken (`SessionStart` → `proje.py start`)

- Kapılar git-devir ile aynıdır (ayardaki `project_roots` altında, vault'un içinde değil, kökte
  `beyin.py` ya da `.git-devir-off` yok, `GIT_DEVIR_OFF=1` değil); tek fark `origin` remote'unun
  zorunlu olmamasıdır.
- Proje çözüldüyse en çok 1500 karakterlik blok:
  - `Proje: <ad> (<sahip/repo>) → <vault klasörü>`
  - en yeni kartın tarihi, makinesi, dalı, `Açık adımlar` ve `Sonraki somut adım` bölümleri;
    kart başka makinedense `BAŞKA MAKİNEDEN` diye belirtilir
  - `Kararlar.md`'deki son 3 karar, birer satır
  - `Kurallar.md`'de madde varsa: `Bu projenin kuralları: <yol> (işe başlamadan oku)`
  - kayıt satırı: anlamlı iş bitince, ya da avenox köprüsü/Stop hook'u `note-create`/`receipt`
    çalıştırmayı isteyince, model bunları kendi çalıştırmaz; ham bilgiyi ve `Receipt session=`
    değerini `beyin-yazici` alt ajanına verir ve hook böyle karşılanır
- Çözülmediyse tek satır: kullanıcıya `30 Projeler/<ad>` klasörünün açılıp açılmayacağını sor ve
  çalıştırılacak `proje.py init` komutu. **Hook hiçbir zaman klasör oluşturmaz.**
  Git reposu olmayan düz klasörde bu soru hiç sorulmaz.
- `resume` ve `compact` ile gelen oturumda, o oturuma daha önce gösterilen blok değişmediyse
  tekrar yazılmaz.
- Ağ kullanmaz; iki kısa `git` çağrısı ve birkaç dosya okuması yapar (ölçülen süre ~0,3 sn).
  Her hata yutulur, çıkış kodu her zaman 0, hata durumunda çıktı yoktur.

## Komutlar

`<python> ege/hooks/proje.py <komut> [--cwd <repo>]` (`--cwd` verilmezse bulunulan klasör):

| Komut | Ne yapar | Kim çalıştırır |
|---|---|---|
| `start` | SessionStart hook'u (stdin'de payload) | Claude Code |
| `context` | Aynı bloğu düz metin yazdırır (kapısız; elle bakmak için) | sen |
| `resolve` | Kimlik, ad, sunucu, vault klasörü (JSON) | `beyin-yazici` |
| `init [--ad <klasör>]` | `30 Projeler/<ad>/` altında `Proje.md`, `Kararlar.md`, `Kurallar.md`, `Oturumlar/` açar; var olan dosyanın üstüne yazmaz. `--ad` var olan bir klasörse repoyu ona bağlar; klasör başka bir projeye bağlıysa reddeder | ana model, **kullanıcı "evet" dedikten sonra** |
| `sustur` | Bu makinede bu repo için bir daha klasör sorulmaz (vault'a yazmaz) | ana model, kullanıcı "hayır" dedikten sonra |
| `kart --dosya <json>` | Yeni oturum kartı (`--dosya -` stdin okur; ya da `--hedef --yapilan --acik --sonraki --karar`) | `beyin-yazici` |
| `karar --karar … --neden … [--alternatif …]` | `Kararlar.md`'ye bir karar ekler (`--sha256` ile ön koşul verilebilir) | `beyin-yazici` |
| `kural --kural … [--genel]` | Projenin (ya da `--genel` ile avenox'un genel) `Kurallar.md` dosyasına tarihli bir kural ekler | ana model, kullanıcı düzeltince hemen; `beyin-yazici` |

Çıkış kodları: `0` tamam · `1` hata (ayar yok, geçersiz girdi, anahtar/şifre gibi görünen metin) ·
`2` yazılmadı, yeniden dene (kart sınırı aşıldı ya da dosya arada değişti) · `3` proje çözülemedi.
`start` ve `context` her durumda `0` döner.

## Bilinen sınırlar (proje)

- **`Kararlar.md` ve iki `Kurallar.md` ortak dosyalardır** (proje başına tek dosya bilinçli bir
  tercihtir). Komutlar yazmadan önce sha256'yı denetler, ama bu yalnız aynı makinedeki yarışı
  yakalar: iki makine çevrimdışıyken aynı dosyaya ekleme yaparsa Obsidian Sync çakışma kopyası
  oluşturabilir ya da iki sürümü birleştirir. Böyle bir kopya görürsen iki dosyadaki girdileri elle
  birleştir. Kartlar ve `Proje.md` bundan etkilenmez (yeni dosyalar); kararların tek satırlık
  özeti kartta da durur.
- Bir proje klasörü tek bir `proje_id` taşır; iki repoyu aynı klasöre bağlamak desteklenmez.
- Kartın zorunlu olduğunu denetleyen bir Stop kontrolü yoktur; kartı `beyin-yazici` iş kaydıyla birlikte yazar.
- Receipt'in kendisinde proje ya da makine alanı yoktur (upstream şeması). Bağ, özetin ilk satırı
  (`Proje: <ad>`), `--event-id` ve receipt'in gösterdiği kart üzerinden kurulur.

# Test

```sh
python -m unittest discover -s ege -p "test_*.py" -v
```

Üç dosya: `hooks/test_git_devir.py`, `hooks/test_proje.py`, `test_kur.py`. Testler geçici klasörde
yerel "bare" repolar, sahte remote adresleri, geçici bir vault ve geçici `CLAUDE_CONFIG_DIR` ile
çalışır; gerçek `~/.claude` dosyalarına ve gerçek vault'a dokunmaz.
