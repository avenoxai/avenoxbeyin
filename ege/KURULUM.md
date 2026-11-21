# Kurulum: makine başına tek komut

Bu belge fork'un **çekirdek katmanını** bir makineye kurar: git-devir ve proje hook'ları,
`beyin-yazici` alt ajanı, global `CLAUDE.md` kuralı ve avenox köprüsü. Hepsi tek komutla,
[`kur.py`](kur.py) ile gelir.

Durum: komut yazıldı ve test edildi; **henüz hiçbir makinede çalıştırılmadı.**

## Ne kurulur?

| Adım | Nereye | Ne |
|---|---|---|
| 1 | `~/.claude/git-devir.json` | Ortak ayar: makine etiketi, repo kökleri, vault yolu, Python ve `proje.py` yolu |
| 2 | `~/.claude/settings.json` | `SessionStart` → `git_devir.py start` ve `proje.py start`; `SessionEnd` → `git_devir.py end`. Var olan bütün hook'lar (avenox köprüsü dahil) korunur |
| 3 | `~/.claude/agents/`, `~/.claude/skills/` | [`agents/`](agents/) altındaki ajanlar ve global kurulabilen skill'ler. `.yalniz-vault` dosyası taşıyan skill (ör. `beyin-skill-onerici`) atlanır |
| 4 | `~/.claude/CLAUDE.md` | [`claude/CLAUDE-kural.md`](claude/CLAUDE-kural.md) içeriği, `<!-- ege-beyin:start -->` … `<!-- ege-beyin:end -->` işaretleri arasında. İşaretlerin dışındaki metne dokunulmaz |
| 5 | vault'taki `beyin.py` | `bridge --harness claude --project-root … --install`. Yalnız `--ayarlar` verilirse ayrıca dört `preferences` ayarı |

- Her adım tekrar çalıştırılabilir; ikinci çalıştırma hiçbir şeyi değiştirmez.
- Değişen her dosyanın eski hâli önce `~/.claude/ege-kur-yedek/<tarih-saat>/` altına kopyalanır.
- `CLAUDE_CONFIG_DIR` ayarlıysa `~/.claude` yerine o klasör kullanılır.
- Vault'a bu komut kendisi yazmaz; 5. adım yalnız vault'un kendi `beyin.py` komutunu çağırır.

## Önkoşullar

1. **Avenox vault'ta kurulu** (resmi release paketinden, bütün makinelerde aynı sürüm). Sıra ve
   ayrıntı: [MAKINELER.md](MAKINELER.md), "Her makinede aynı adımlar" 1–3. Vault kökünde `beyin.py`
   görünmeli. Yoksa `kur.py` ilk dört adımı yapar, köprü adımını atlar ve bunu söyler.
2. **Obsidian Sync bitmiş** olmalı (`30 Projeler/` klasörü gelmiş olsun). Repo ↔ klasör bağı
   her proje klasöründeki `Proje.md` dosyasında durur; Markdown olduğu için ek bir Sync ayarı gerekmez.
3. **Python 3.11+** ve **git** kurulu. Hook'lar, `kur.py`'yi çalıştırdığın Python ile çalışır
   (başka bir Python için `--python <yol>`).
4. **Claude Code** kurulu ve giriş yapılmış.

## 1. Bir kereye mahsus: GitHub girişi

git-devir kapanışta push, açılışta fetch yapar ve **asla şifre sormaz**; kimlik bilgisi kayıtlı
değilse sessizce başarısız olur ve bir sonraki açılışta not düşer. Bu yüzden her makinede bir kez
giriş yap:

```sh
gh auth login        # GitHub.com → HTTPS → tarayıcıyla giriş
gh auth setup-git    # git'in bu girişi kullanmasını sağlar
```

`gh` yoksa: özel bir repoda bir kez elle `git fetch` çalıştır, açılan tarayıcı penceresinde giriş
yap (Git Credential Manager kimliği saklar) ya da SSH anahtarı kullan. Kontrol:

```sh
git ls-remote <özel-bir-reponun-adresi>    # şifre sormadan dal listesi gelmeli
```

## 2. Fork'u klonla ya da güncelle

```sh
git clone https://github.com/kutluege/egeavenoxbeyin.git
cd egeavenoxbeyin
git switch ege/tercihler
```

Zaten varsa: `git pull`. Klasör **vault'un dışında** durur. Hook'lar bu klasördeki dosyaları
çalıştırır; klasörü taşırsan 3. adımı yeniden çalıştır.

## 3. Önce dene, sonra kur

`<repo-koku>` = repolarının durduğu klasör, `<vault-yolu>` = Obsidian vault'u. Önce `--dry-run` ile
ne yapılacağına bak (hiçbir dosya değişmez), sonra aynı komutu `--dry-run` olmadan çalıştır.

Şirket PC'si (Windows, PowerShell):

```powershell
py -3 ege\kur.py --machine is-5090 --project-root "<repo-koku>" --vault "<vault-yolu>" --dry-run
py -3 ege\kur.py --machine is-5090 --project-root "<repo-koku>" --vault "<vault-yolu>"
```

Ev PC'si (Windows, PowerShell):

```powershell
py -3 ege\kur.py --machine ev --project-root "$HOME\Kod" --vault "<vault-yolu>" --dry-run
py -3 ege\kur.py --machine ev --project-root "$HOME\Kod" --vault "<vault-yolu>"
```

MacBook:

```sh
python3 ege/kur.py --machine macbookneo --project-root ~/Kod --vault "<vault-yolu>" --dry-run
python3 ege/kur.py --machine macbookneo --project-root ~/Kod --vault "<vault-yolu>"
```

Seçenekler:

- `--project-root` birden çok kez verilebilir.
- `--machine` kısa etikettir (`is-5090`, `ev`, `macbookneo`), bilgisayar adı değil. Kartların dosya
  adına ve wip dallarına girer; sonradan değiştirme.
- `--ayarlar`: avenox'ta `preferences --project-context on`, `--secret-filter on`,
  `--context-dedupe on` ve `--inbox-report on --inbox-folder "00 Gelen"` komutlarını da çalıştırır.
  Bu ayarlar [TERCIHLER.md](TERCIHLER.md)'de hâlâ "onay bekliyor"; onaylayınca ekle.
- `--force`: kopyalanan bir ajan/skill dosyasını elle değiştirdiysen `kur.py` ona dokunmaz ve
  bildirir; üzerine yazdırmak için bunu ekle (eskisi yedeğe alınır).
- git-devir ayarları: `--auto-owner`, `--auto-host`, `--max-file-mb` ([hooks/README.md](hooks/README.md)).
- `beyin-yazici` tanımındaki `model: sonnet` bir takma addır: o an geçerli Sonnet sürümünü izler,
  belirli bir sürüme (ör. 5.5) sabitlemez. Sabitlemek istersen `agents/beyin-yazici.md` içindeki
  satıra tam model kimliğini yaz ve `kur.py`'yi yeniden çalıştır.

Komut her adımı ve köprü için çalıştırdığı her `beyin.py` komutunu sonucuyla yazdırır. Köprü adımı
başarısız olursa önceki adımlar geri alınmaz; sorunu giderip aynı komutu yeniden çalıştır.

## 4. Doğrula

1. Aynı komutu bir daha çalıştır: özet satırı `0 dosya değişikliği yapıldı; 0 sorun.` olmalı.
2. Repo kökünün altındaki bir repoda:

   ```sh
   python <fork>/ege/hooks/proje.py context
   ```

   `Proje: <ad> (<sahip/repo>) → 30 Projeler/<ad>` ya da "vault'ta klasör yok … kullanıcıya sor"
   satırını görmelisin.
3. Açık Claude Code oturumlarını kapat, aynı repoda **yeni** oturum aç. Oturum başında aynı
   `Proje:` satırı gelir; `/agents` listesinde `beyin-yazici` görünür.
4. Klasör yoksa ajan sana sorar; "evet" de. `30 Projeler/<ad>/` altında `Proje.md`, `Kararlar.md`,
   `Kurallar.md` ve `Oturumlar/` oluşur.
5. Küçük bir değişiklik yaptır ve işi bitir. `30 Projeler/<ad>/Oturumlar/` altında yeni bir kart,
   vault'ta da yeni bir iş kaydı olmalı (vault klasöründe `python beyin.py recap --days 1`).
6. İkinci makineyi kurduktan ve Sync bittikten sonra aynı repoyu orada aç: oturum başında ilk
   makinenin kartı `BAŞKA MAKİNEDEN` etiketiyle görünmeli.

### İlk kurulumda canlı kontrol: kayıt gerçekten `beyin-yazici`'den mi geçiyor?

Oturumda iki ayrı talimat vardır: avenox köprüsü ve Stop hook'u modele "`note-create` ve `receipt`
çalıştır" der; global kural ve `proje.py start` satırı ise "bunları kendin çalıştırma, ham bilgiyi
ve `Receipt session=` değerini `beyin-yazici`'ye ver" der. Modelin ikincisine uyduğu otomatik testle
doğrulanamaz; **ilk kurulumda bir kez elle bak:**

1. Vault klasörü olan bir repoda yeni oturum aç, tek satırlık bir değişiklik yaptır (ör. README'ye
   bir cümle) ve başka bir şey söyleme. Model bitirmek isterken avenox onu bir kez durdurur
   ("no receipt was written").
2. Modelin ne yaptığına bak: `beyin-yazici` alt ajanını çağırmalı; `beyin.py note-create` ya da
   `beyin.py receipt` komutunu **kendisi çalıştırmamalı**.
3. Sonucu kontrol et: vault klasöründe `python beyin.py recap --days 1` tek yeni kayıt göstermeli,
   özetin ilk satırı `Proje: <ad>` olmalı, kaynaklarında `Oturumlar/` altındaki yeni kart bulunmalı
   ve oturum ikinci kez durdurulmadan kapanmalı.

Model komutları kendi çalıştırdıysa ya da iki kayıt oluştuysa: `/memory` ile `CLAUDE.md` bloğunun
yüklendiğine bak ve durumu not et; kural metninin ([claude/CLAUDE-kural.md](claude/CLAUDE-kural.md))
güçlendirilmesi gerekir.

## 5. Güncelleme

```sh
git pull
python ege/kur.py --machine <etiket> --project-root "<repo-koku>" --vault "<vault-yolu>"
```

Aynı komut yalnız değişenleri yeniler: ajan dosyası, kural bloğu, hook komutları. Avenox'u
güncelledikten (`beyin.py update`) sonra da aynı komutu çalıştır; köprü yeniden kurulur.

## 6. Kaldırma

```sh
python ege/kur.py --uninstall --dry-run
python ege/kur.py --uninstall
```

- Kaldırılanlar: git-devir ve proje hook girdileri, `CLAUDE.md`'deki yönetilen blok, bu komutun
  kopyaladığı ve sonradan değişmemiş ajan/skill dosyaları.
- Bırakılanlar: `git-devir.json`, `~/.claude/git-devir/` (log, notlar, `sustur` listesi), yedekler,
  vault'taki her şey (`Proje.md`, kartlar, kararlar, kurallar, avenox ayarları) ve avenox köprüsü.
- Avenox köprüsünü de kaldırmak için vault klasöründe:
  `python beyin.py bridge --harness claude --uninstall`

## Sorun olursa

| Belirti | Bak |
|---|---|
| Oturum başında `Proje:` satırı yok | Repo `--project-root` altında mı? Kökünde `.git-devir-off` var mı? `proje.py context` ne diyor? |
| "vault'ta klasör yok" ama klasör var | Klasör adı repo adından farklıysa: `proje.py init --ad "<klasör adı>"` ile bağla (klasöre `Proje.md` yazar) |
| Klasör sorusu hiç gelmiyor | Bu makinede daha önce "hayır" denmiş olabilir: `~/.claude/git-devir/proje/sustur.json`; `proje.py init` geri alır |
| Kart yazılmadı | `beyin-yazici` cevabındaki "Yapılamayan" satırı; `proje.py resolve` çıkış kodu 3 ise klasör yok |
| Push/fetch notları | 1. adımdaki GitHub girişi; `~/.claude/git-devir/log.txt` |
| `kur.py` "dokunulmadı" diyor | O dosyayı elle değiştirmişsin; farkı gör, gerekiyorsa `--force` |
