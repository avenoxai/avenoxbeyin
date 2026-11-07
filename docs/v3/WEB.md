# İsteğe bağlı yerel web görünümü

`beyin.py web`, vault'un görevlerini, son receipt'lerini, hafıza dosyalarının boyutunu ve
doktor özetini tarayıcıda gösteren küçük bir yerel sunucudur ([#371](https://github.com/avenoxai/avenoxbeyin/issues/371),
[#423](https://github.com/avenoxai/avenoxbeyin/pull/423)). Tamamen isteğe bağlıdır: kurulum,
güncelleme ve hook'lar onu hiçbir zaman başlatmaz; masaüstü kısayolu ya da arka plan servisi
kurulmaz. Yalnız sen komutu çalıştırınca açılır.

```sh
python3 beyin.py web ac              # sunucuyu başlatır, tarayıcıyı açar
python3 beyin.py web ac --read-only  # yalnız okuma: görev, eşitleme ve sıkıştırma kapalı
python3 beyin.py web ac --no-browser # adresi yalnız yazdırır
python3 beyin.py web durum
python3 beyin.py web durdur
```

Windows'ta `py -3 beyin.py web ac`. Varsayılan port 7373-7393 arasındaki ilk boş porttur;
`--port` ile seçilebilir. Sunucu 30 dakika kullanılmazsa kendiliğinden kapanır, `Ctrl+C` ya da
`beyin.py web durdur` da kapatır.

## Ne gösterir, ne yazar

| Bölüm | Kaynak | Yazma |
| --- | --- | --- |
| Kokpit | `doctor` özeti, açık görev sayıları, son receipt'ler | yok |
| Görevler | `tasks/` altındaki görev kayıtları (`kind: task`) | yeni görev `task-create`, durum değişikliği `task-update` |
| Arama | ajanların bağlam için kullandığı dizin | yok |
| Receipt'ler | son 14 günün receipt'leri | yok |
| Hafıza | `companion-compact --dry-run` planı | `companion-compact` (eski girdileri arşive aynen taşır, silmez) |

Üst köşedeki **Eşitle** düğmesi `beyin.py sync` ile aynı işi yapar. Web görünümü vault'a kendi
başına dosya yazmaz: her yazma, CLI komutunun kendisi aynı süreçte çalıştırılarak yapılır; revizyon
denetimi, kilitler, sır süzgeci ve projeksiyon doğrulaması CLI'dakiyle aynıdır. Eşzamanlı bir
değişiklikte görev güncellemesi `revision conflict` ile reddedilir; sayfa yenilenip yeniden denenir.

## Gizlilik

Okumalar çekirdek dizinin kendi kapısından geçer: `visibility: private` notlar ve görevler,
`trust: untrusted` kayıtlar, kasa sınıfı klasörler (Kasa, Şifre, Müşteri, Finans...), dizinden
hariç tutulan klasörler ve kaynağı dizinden sonra değişmiş notlar arama sonuçlarında, görev
listesinde ve önizlemede görünmez; kimliği bilinse bile açılamaz ya da güncellenemez. Gizli bir
kaynağa bağlı receipt özeti gösterilmez. Herhangi bir vault yolu doğrudan istenemez; önizleme
yalnız dizindeki bir kaydın kimliğiyle açılır.

## Güvenlik

- Sunucu yalnız `127.0.0.1` üzerinde dinler. Her istekte `Host` başlığı bu dinleyiciyi
  (`127.0.0.1:<port>` ya da `localhost:<port>`) göstermeli; `Origin` ve `Sec-Fetch-Site` varsa
  aynı kaynağı göstermeli. Başka bir site, DNS rebinding ile de olsa yanıt alamaz.
- Her açılışta yeni bir belirteç üretilir. Tarayıcı bu belirteçle bir kez gelir; sunucu ayrı bir
  `HttpOnly; SameSite=Strict` oturum çerezi verir ve belirteci adres çubuğundan hemen kaldırır.
  Belirteç yalnız state klasöründeki `web/running.json` dosyasında (izin 0600) tutulur.
- Her yazma isteği oturum çerezi, aynı kaynaktan `Origin`, `application/json` gövdesi ve o oturumun
  CSRF belirtecini taşımalıdır. CORS başlığı hiç gönderilmez.
- Sayfa, stil ve betik `beyin_v3_web_ui.py` içinden gelir; CDN, web fontu ya da uzak görsel yoktur.
  İçerik güvenlik politikası yalnız aynı kaynaktaki bu dosyalara izin verir; vault metni sayfaya
  her zaman düz metin olarak yazılır.

Python standart kütüphanesi dışında bağımlılık yoktur; dış ağa bağlanmaz.
