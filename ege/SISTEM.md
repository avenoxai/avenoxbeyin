# Avenox nasıl çalışır (basit anlatım)

Sürüm: 3.13.0 üzerinden, kod okunarak doğrulandı.

## Benzetme

- **Defter** = Obsidian vault. Bütün notlar Markdown dosyası; tek gerçek kaynak bunlar.
- **Kâtip** = avenox. Defterle Claude arasında duran küçük bir yardımcı. **Model çağırmaz, arka
  planda servis çalıştırmaz.** Yalnız bir ajan oturumu açıkken hook'larla devreye girer.
- **Yazan ve düşünen** her zaman ajanın kendisidir (Claude / Codex). Kâtip hatırlatır, getirir, zorlar.
- **Obsidian Sync** defteri bütün cihazlara taşır. Avenox'un kendi senkron kodu yoktur.

## Parçalar

| Parça | Yer | Ne işe yarar |
|---|---|---|
| Kimlik | `🔮 850-Companion/Core.md` | Kim olduğum, nasıl çalıştığım |
| Kurallar | `🔮 850-Companion/Kurallar.md` | Düzeltmelerim; projeye özel olanlar `projects/<proje>/Kurallar.md` |
| Açık işler | `🔮 850-Companion/Threads.md` | Süren konular ve durumları |
| Kaldığım yer | `🔮 850-Companion/Last-Session.md` | Oturum kartları (en yeni üstte, ~3000 karakter, eskiler arşive) |
| Günlük gözlem | `🔮 850-Companion/Journal.md` | Ortak çalışmadan çıkan gözlemler |
| İş kayıtları | `receipts/` | Her iş bitince yazılan, **değişmeyen** kayıt; her biri ayrı dosya |
| Görevler | `tasks/` | Her görev bir dosya, revizyon numaralı |
| Otomatik görünümler | `daily/v3/`, `knowledge/v3/outcomes.md` | Kayıtların tarih sıralı listesi; her makine kendisi üretir, **Sync dışında tutulmalı** |
| Arama dizini | Vault dışında, makineye özel SQLite | BM25 + Türkçe ek kökleme; silinse notlardan yeniden kurulur |
| Program | `beyin.py`, `.claude/scripts/` | Komutlar ve hook betikleri |

## Bir oturumun hayatı (vault içinde)

| Hook | Ne zaman | Ne yapar |
|---|---|---|
| SessionStart | Oturum açılınca | Kurallar, açık işler, en yeni devir kartı ve son iş kaydını bütçeyle verir (3.13: önce bunlar ayrılır) |
| UserPromptSubmit | Her mesajda | Mesajdaki kelimelerle notlarda arar, ilgili paragrafları ekler (~4 sn sınır). "devam et" gibi belirsiz mesajda son 20 dk'nın kaynaklarını yeniden verir |
| PostToolUse | Dosya düzenlenince | "Bu oturumda değişiklik oldu" işareti |
| Stop | Ajan bitirmek isteyince | Değişiklik var ama iş kaydı yoksa **bir kez durdurur**: "kaydı yaz". Kayıtta öğrenim var deyip not açılmadıysa bir kez hatırlatır |
| PreCompact / SessionEnd | Sıkıştırma / kapanış | **Özet yazmaz**, yalnız iç işaretleri sıfırlar |

## Oturumlar arası süreklilik

- **Zorunlu olan tek şey iş kaydı (receipt).** Stop hook'u bunu dayatır.
- Oturum kartı (Last-Session), Threads, Kurallar ve bilgi notları **yalnız talimatla** yazılır
  (`AGENTS.md`, `beyin` skill'i). Ajan unutursa yazılmaz.
- Konuşma dökümü hiçbir yerde tutulmaz. Arkada özet çıkaran model yoktur.
- Yeni oturum en son yapılan işi görür; **o repoda en son yapılanı değil** (aşağıdaki köprü ayarı hariç).

## Vault dışı repolar: global köprü

- Varsayılan kurulum yalnız vault içindeki oturumları görür.
- `beyin.py bridge --harness claude --project-root <kök> --install` global ayara hook ekler:
  oturum başında kısa talimat, düzenleme gözlemi ve Stop'ta bir kez kayıt hatırlatması.
  Repo oturumlarında her mesajda otomatik arama **yoktur** (token tasarrufu); ajan gerekirse
  `beyin ara`/`context` ile kendisi bakar.
- `preferences --project-context on`: repoda oturum açınca **o projenin** son iş kaydını ve
  tarihi gelmiş görevlerini başa koyar. Sınır: proje kimliği **klasör yolunun hash'i**; aynı repo
  farklı makinelerde farklı yolda ise ayrı proje sayılır (görevler klasör adıyla eşleşir).

## Kendiliğinden gelen ek araçlar (hepsi model çağırmaz)

| Komut | İş |
|---|---|
| `triage` | Gelen'de bekleyenler, bayat görev/konu, haftalık bilgi özeti |
| `recap` | Son günlerin kaynak bağlantılı iş sonuçları |
| `audit` | Kopya, eskimiş ve çelişen notlar (yalnız rapor) |
| `beyin-kontrol` skill'i | Hafızadaki durum iddialarını git/dosya/kayıtlarla doğrular (salt okunur) |
| `learn` | Öğrenim adaylarını kalıcı nota çevirmeden önce biriktirir |
| `scheduled-check` | İşletim sistemi zamanlayıcısıyla sync + doctor; sorun varsa bildirim |
| `companion-compact` | Şişen Last-Session/Threads'i silmeden arşive taşır |
| `lint`, `moc`, `history` | Kırık bağlantı, içindekiler notu, revizyon geçmişi |
| Yakala | Telefondan/masaüstünden link, makale, PDF yakalama; `beyin-yakala` skill'i |

## Güvenlik notları

- Not düzenleme SHA-256 ön koşullu; arada değişen notu ezmez. Silme yapmaz.
- Adında `kasa`, `şifre`, `parola`, `kimlik`, `finans`, `müşteri`, `vergi`, `fatura`, `maaş`, `özel`,
  `gizli` geçen klasörler aramaya girmez. `Kişisel` bu listede **yok**.
- Yazma anındaki sır süzgeci (`--secret-filter`) **varsayılan kapalı**; açılması önerilir.
