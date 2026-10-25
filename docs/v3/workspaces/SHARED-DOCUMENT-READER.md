# Ortak belge servisi — C runtime / D dağıtım sınırı

17 Ekim 2026, `workspace/0.1.0`. K10 ortak okuyucu kararı uyarınca PDF pypdf 6.21.0, DOCX stdlib ZIP/XML; PDF'nin metinsiz sayfalarında Poppler + Tesseract `tur+eng`. Bu belge yeni bir workspace şeması tanımlamaz; mevcut chunk/fragment/extractor/hash/capability sonucunun ortak girişini belgeler. Alan başına parser veya ikinci çıkarım hattı yok.

## Gerçek arayüz

`beyin_v3_sources.extract_document_bytes(vault, raw_bytes, 'pdf'|'docx', readers=None)` → `state`, `chunks` (`text`, `fragment`), `issues`, özgün `source_sha256`, `extractor`. Python çekirdeği pypdf import etmez; child `-I -S -B` pinli wheel'in hash'ini doğrular. Kaynak locator'ı helper'a geçmez. `readers` yalnız güvenilir yerel runtime/test adaptörü içindir; belge veya HTTP payload'ından alınmaz.

- Workspace `SourceResolver`: profil/binding/grant ve hash denetimini extraction/cache öncesinde ve sonuç yayımlanmadan yeniden uygular. Hazır cache bile yetki değildir. Native OCR dil verisinin gerçek byte hash’leri ve seçili TESSDATA_PREFIX extractor settings hash’ine dahildir; yalnız Tesseract binary’sinin değişimine bakılmaz. Memory-vault binding'inde scope'suz ikili attachment otomatik paylaşılmaz; belge klasörü ayrı onaylı directory source olarak bağlanır.
- Yakala `extract/process`: mevcut vault-local dosya kontrolünden geçen yakalanmış PDF/DOCX byte'larını aynı servise verir. Sonunda kaynak yolu ve özgün hash yeniden doğrulanır. Eski pdftotext çağrısı kaldırıldı. `dosya` yöntemi, kart/ham akışı ve kaynakların private/güvenilmeyen olması korunur. PDF fiziksel sayfa, DOCX blok/tablo-hücre konumu metinde ve `belge_cikarim` metadata'sında saklanır. `cikarim_durumu` hazır/eksik parça ayrımını taşır; partial metin tam hazır sayılmaz. Password/missing/failed boş başarıya dönüşmez. Ham çıktı kanonik öğrenim değildir.
- Mevcut HTTP ingest JSON/metin kabul eder, dosya yolu açmaz ve binary upload endpoint'i yoktur. İstek içindeki `file`/`source` alanlarını belge okuma yetkisine dönüştürmeyin. Yakala'nın işlenmiş ham metni ancak aktif ajanın kaynak incelemesi ve mevcut kanonik writer yoluyla güvenilir bilgiye dönüşür.
- Mevcut digest kanonik Markdown bilgi/not değişikliklerini raporlar. Private/untrusted Yakala ham metni digest'e doğrudan terfi etmez; aynı extraction sonucundan türeyen, ajan tarafından kaynaklı yazılmış bilgi mevcut digest girişidir. Ingest, extraction, digest ve semantik sentez ayrı sorumluluklardır; yeni ingest/digest motoru kurulmadı.

## Capability ve bağımlılıklar

D'nin `beyin_v3_parser_probe.document_capabilities(vault,include_ocr=True)` gerçek kurulu PDF/DOCX/OCR içerik+konum probe'udur (`9adeeda`; ilk text probe `935e227`). C `probe_capabilities` yalnız bu listenin name→state adaptörü ve workflow bulunabilirliği için tamamlayıcı haritadır. OCR kurulu pinli `ocr-probe-tr-en.pdf` asset'iyle bir kez çıkarılır; deprecated C `ocr_probe` argümanı yok sayılır ve ikinci OCR çalıştırmaz. Binary/dil varlığı OCR ready sayılmaz. Workflow alanı tüm katalog/ürün kabulünün yerine geçmez.

D4'e bağımlılık: wheel/lock/license korunacak; ortak helper ve sources modülü birlikte paketlenecek. Native Poppler `pdftoppm`, Tesseract ve **iki dil verisi** için kurulum/probe/offline/missing durumları açık raporlanacak. C yerel Linux'ta mevcut native kurulumla çalıştı; diğer makinede bulunabilirlik varsayılmaz. M04 kullanıcının seçimiyle ilk teslim zorunluluğudur. Pypdf kalite/hız üstünlüğü iddia edilmez.

## Sınırlar ve kanıt

Sentetik TR/EN text PDF, DOCX paragraf/tablo, raster TR/EN PDF ve text+raster karma PDF sınanır. Shared reader testi aynı PDF/DOCX'in workspace ve Yakala metin/fragment/hash eşleşmesini, raw trust sınırını ve arada kaynak değişiminde reddi gerçek servislerle sınar. Büyük gerçek belge, çok sütunlu karmaşık layout ve native/platform dağıtım korpusu son kabulde ayrıca gereklidir. Taranmış sayfa OCR en çok 20 sayfa; raster ölçeği 2400; byte/text/memory/timeout sınırları geçerlidir. Boş/okunamayan sayfa veya zaman aşımı açık kalır.

Aktif workspace modunda eski kapsamsız recap/triage/doctor/history/ingest-server/advisor CLI yolları reddedilir. Doğrudan Python triage ve remote advisor da kapalıdır. Typed yerel karşılık workspace-context ve scope kontrollü store işlemleridir; remote workspace rızası henüz ayrı uygulanmış API değildir. CLI başarısı canlı istemci teslimi değildir. M06 seçimi Linux Codex+Hermes; canlı model testi henüz tamamlanmadı.
