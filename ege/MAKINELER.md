# Makineler ve kurulum kurgusu

## Genel yapı

```
 TELEFON                           EV PC (5060 Ti) = beyin sunucusu
 ┌──────────────┐                  ┌─────────────────────────────────┐
 │ Claude app   │──Remote Control─►│ claude remote-control (vault'ta)│
 │ (soru, CV,   │                  │  + avenox hafızası              │
 │  mail, görev)│                  │  + mail aracı (yalnız taslak)   │
 ├──────────────┤                  │  + Obsidian Sync                │
 │ Obsidian app │──────┐           └──────────────▲──────────────────┘
 │ (not, fikir) │      │                          │
 └──────────────┘      ▼                          │
                 OBSIDIAN SYNC  ◄─────────────────┤
                       ▲                          │
 MACBOOK NEO / ŞİRKET 5090 / ŞİRKET VM / 4090     │
 repolarda ajan ─► köprü ─► iş kayıtları ─────────┘
```

**Altın kural:** Yakalama makineleri yalnız **yeni dosya ekler** (kayıt, kart, görev). Mevcut
notları düzenleyen yapısal işler (haftalık dağıtım) **tek editör makinede** yapılır. Böylece
Sync çakışması olmaz; bağlantısı kopan makine de güvenlidir.

## Makine tablosu

| Makine | Ajan / hesap | Vault nasıl gelir | Köprü kökü | Rol | Durum |
|---|---|---|---|---|---|
| Şirket 5090 (Windows) | Claude (şahsi/Enterprise) | Obsidian uygulaması | repo kökü (❓) | Yakalama, editör adayı | Avenox 3.13.0 kurulu; köprü yok |
| Ev 5060 Ti (Windows) | Claude şahsi | Obsidian uygulaması | `~/Kod` | Beyin sunucusu, editör adayı | Kurulmadı |
| MacBook Neo | Claude şahsi + Codex | Obsidian uygulaması | `~/Kod` | Yakalama | Kurulmadı |
| Şirket VM (RTX6000, Linux) | Claude Enterprise | Obsidian Headless (`ob`), önce pull-only | `$HOME` | Yakalama | Erişim bekleniyor (görev `blocked`) |
| 2×4090 (uzak) | ❓ | Obsidian Headless | `~/Kod` | Yakalama | Bağlantı güvenilmez; varken tek seferde |
| iPhone | — | Obsidian app | — | Not, Claude app ile erişim | Kısayol `00 Gelen/<tarih>.md`'ye |

## Her makinede aynı adımlar

1. Sync'in bitmesini bekle: `🔮 850-Companion/Core.md` görünmeli (kurulum çıktısı `companion: existing`).
2. Sync ayarında `daily/v3` ve `knowledge/v3` hariç.
3. Avenox resmi paket, aynı sürüm:
   `install_v3.py --vault <vault> --exclude-component adapters --exclude-component harnesses/antigravity`
4. Köprü: `beyin.py bridge --harness claude --project-root <kök> --install`
   (Codex olan makinede ayrıca `--harness codex` ve Codex `/hooks` güven onayı; Codex kotası dar
   olduğu için `--context-chars` düşük tutulur).
5. Ayarlar: `TERCIHLER.md`'deki onaylı `preferences` satırları.
6. Global Claude dosyaları (`~/.claude` senkronlanmaz): hook'lar, `beyin-yazici` ajanı, global
   `CLAUDE.md` kuralı. Ana kopyalar bu fork'ta durur (`ege/hooks/`, `ege/agents/`, `ege/claude/`,
   `ege/skills/`), vault'ta değil; her makinede tek komutla, `kur.py` ile kurulur
   ([KURULUM.md](KURULUM.md)). Aynı komut 4. adımdaki köprüyü de kurar, `--ayarlar` ile 5. adımı da
   yapar. `orkestra` skill'i henüz fork'ta değil; eklenince aynı komutla gelir.
7. Doğrulama: o makinede bir deneme kaydı yazdır, başka makinede `recap` ile görünmesini kontrol et.

## Ev PC'si (beyin sunucusu) ek adımları

1. BIOS: "Restore on AC power loss = Power On" (akıllı priz için) veya Wake-on-LAN.
2. Windows otomatik oturum açma + başlangıç görevi: Obsidian (Sync) ve vault'ta
   `claude remote-control --name beyin`.
3. Telefondan: priz aç → 1–2 dk → Claude app'te ev PC'si görünür → işin bitince "kapat".
4. Mail aracı (IMAP/SMTP MCP) yalnız taslak yetkisiyle.
5. `scheduled-check` açılışta çalışır; telefondan sorulduğunda dizin güncel olur.

Alternatif: beynin 7/24 açık olması gerekirse ~5 €/ay VPS; aynı kurgu, ama şifreli vault yabancı
sunucuda açık durur. Önce ev PC'si.

## Kurulum sırası

1. Şirket 5090: köprü, proje bağlamı, `beyin-yazici`, ayarlar.
2. Skill'ler: haftalık gözden geçirme, öğrenme koçu, CV, mail taslağı.
3. Ev PC'si: avenox, Remote Control, açılış zinciri, mail.
4. Telefon: Claude + Obsidian app, Yakala kısayolu.
5. MacBook Neo (Codex dahil).
6. Şirket VM (erişim gelince), 4090 (bağlantı varken).
