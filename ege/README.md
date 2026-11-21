# Ege'nin beyni: avenoxbeyin üzerine kişisel düzen

Bu fork, [avenoxai/avenoxbeyin](https://github.com/avenoxai/avenoxbeyin) V3'ü temel alır ve onu
Ege'nin çalışma düzenine göre genişletir. Amaç: **aktif olarak ne yaptığımı, hangi projelerde
çalıştığımı, her projede kaldığım yeri ve kurallarımı bilen; telefondan da erişilebilen bir ikinci beyin.**

## Fork kuralı

- Kişisel tasarım, tercih ve eklentiler **yalnız bu `ege/` klasöründe** durur. Upstream dosyalarını
  (`scripts/`, `template/`, `docs/`) zorunlu olmadıkça değiştirmeyiz; böylece upstream yeni sürümleri
  çakışmasız alınır.
- Upstream değişikliklerini almak:
  ```sh
  git fetch upstream
  git merge upstream/main
  ```
- Vault'a kurulan sürüm her zaman **resmi release** paketidir (`beyin.py update`). Bu fork'taki
  eklentiler (skill, hook, ayar) ayrı kurulur; vault'u fork'un main dalından kurmayız.
- Bu depo **herkese açık**: gerçek not içeriği, e-posta, makine adı, şirket iç bilgisi, şifre ya da
  yol buraya yazılmaz.

## Belgeler

| Dosya | İçerik |
|---|---|
| [SISTEM.md](SISTEM.md) | Avenox nasıl çalışır: basit anlatım, oturum döngüsü, hook'lar, sınırlar |
| [TERCIHLER.md](TERCIHLER.md) | Verilmiş kararlar, onay bekleyen öneriler, açık sorular |
| [MAKINELER.md](MAKINELER.md) | Her bilgisayar için kurulum kurgusu ve sıra |
| [KURULUM.md](KURULUM.md) | Çekirdek katmanı bir makineye kuran tek komut (`kur.py`): adımlar, doğrulama, kaldırma |
| [YOL-HARITASI.md](YOL-HARITASI.md) | Avenox'a ekleyeceğimiz özelleştirmeler ve durumları |
| [skills/](skills/README.md) | Fork'un kendi skill'leri ve kurulumu |
| [hooks/](hooks/README.md) | Fork'un hook'ları: git-devir (kapanışta wip+push, açılışta pull) ve proje (repo → vault projesi, oturum kartları, kararlar) |
| [agents/](agents/beyin-yazici.md) | Fork'un alt ajanları: `beyin-yazici` (kart, karar ve iş kaydını Sonnet ile yazar) |
| [claude/](claude/CLAUDE-kural.md) | Global `CLAUDE.md` dosyasına eklenen kural bloğu |

## Şu anki durum (2026-11-16)

- **Kurulu olan:** ana vault'ta avenoxbeyin **3.13.0** (resmi paket), şirket 5090 makinesinde.
  Başka hiçbir şey hiçbir makineye kurulmadı.
- **Yazıldı ve test edildi, kurulmadı:** git-devir hook'u, proje çözücü ve oturum başı hook'u
  (oturum kartları, `Kararlar.md`), `beyin-yazici` alt ajanı, global `CLAUDE.md` kuralı, tek komutluk
  kurucu `kur.py` (avenox köprüsünü ve istenirse ayarları da kurar), `beyin-skill-onerici` skill'i.
  Kurmak için: [KURULUM.md](KURULUM.md).
- **Henüz yok:** haftalık gözden geçirme, öğrenme koçu, `scheduled-check`, telefon/uzaktan erişim;
  bkz. [YOL-HARITASI.md](YOL-HARITASI.md).
