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
| [YOL-HARITASI.md](YOL-HARITASI.md) | Avenox'a ekleyeceğimiz özelleştirmeler ve durumları |
| [skills/](skills/README.md) | Fork'un kendi skill'leri ve kurulumu |
| [hooks/](hooks/README.md) | Fork'un hook'ları: git-devir (kapanışta wip+push, açılışta pull) |

## Şu anki durum (2026-11-16)

- Ana vault'ta avenoxbeyin **3.13.0** kurulu (resmi paket), şirket 5090 makinesinde.
- Köprü (repo oturumları), ucuz kayıt ajanı ve haftalık gözden geçirme henüz yok;
  bkz. [YOL-HARITASI.md](YOL-HARITASI.md).
