# README görselleri

## cover.png

İkinci Beyin için özel üretilmiş kavramsal kapak. Gerçek ürün ekranı değildir.
OpenAI imagegen aracıyla 12 Kasım 2026'da üretildi; orijinal PNG kullanıldı.

Üretim prompt'u [cover-prompt.txt](cover-prompt.txt) içinde tutulur. Kapak sürüm
numarası veya test sonucu içermez; sürüm güncellenince yeniden üretilmesi gerekmez.

## web-tasks.png

V3.13.0'ın gerçek yerel web arayüzünün Görevler ekranı. README için oluşturulan
örnek bir vault, beş not, sekiz görev ve beş iş sonucu kullanıldı. Kişisel veri yoktur.

Paket kurulduktan sonra kayıtlar kurulu `note-create`, `task-create` ve `receipt`
komutlarıyla oluşturuldu. `web ac --read-only --no-browser` ile açılan arayüz yeni
bir headless Chromium oturumunda 1660 × 850 boyutunda görüntülendi. Ürün HTML/CSS'i
ve ekran görüntüsü değiştirilmedi. Çekim sonunda yerel sunucu kapatıldı.

Bu görsel otomatik ajan/hook teslimini kanıtlamaz; örnek verilerle arayüzü gösterir.
