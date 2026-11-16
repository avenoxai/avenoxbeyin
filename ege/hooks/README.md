# git-devir: makineler arası git devri

Aynı repolarda birden çok makinede (şirket PC'si, MacBook, ev PC'si) çalışırken
"kapatırken commit + push, açarken pull" işini otomatik yapan Claude Code hook'u.
Tek dosya: [`git_devir.py`](git_devir.py). Yalnız Python standart kütüphanesi; Windows, macOS ve
Linux'ta çalışır.

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

Her makinede bir kez (`<...>` kısımlarını o makineye göre doldur):

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

## Test

```sh
python -m unittest discover -s ege/hooks -p "test_*.py" -v
```

Testler geçici klasörde yerel "bare" repolarla çalışır; gerçek `~/.claude` dosyalarına dokunmaz.
