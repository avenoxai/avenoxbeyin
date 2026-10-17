# D4 kurulu OCR probe ve bağımlılık sınırı — 17 Ekim 2026

Sözleşme `workspace/0.1.0`. D dalı mevcut C teslimini `c4ede08` dahil gerçek merge ile aldı. Ortak PDF/DOCX çıkarımı ve Yakala runtime'ı C sahibindedir; D ikinci okuyucu yazmaz.

`beyin_v3_parser_probe.document_capabilities(vault, timeout=20, include_ocr=True)` aynı kurulu C reader'ına üç sentetik belge verir: text PDF, paragraf/tablo DOCX ve raster TR/EN PDF. Dönen G0 Capability listesinde `pdf_text`, `docx_text`, `ocr_tr_en` vardır. `include_ocr=False` yalnız açık text-only kontrolü içindir; ilk teslimin OCR kabulünü karşılamaz. OCR ready için Türkçe ve İngilizce metin ile fiziksel sayfa 1 konumu birlikte doğrulanır. Bu smoke kontrolü karmaşık gerçek belge kalitesi veya canlı istemci kabulü değildir.

Raster fixture `tests/fixtures/v3/workspaces/runtime/ocr-scan-tr-en.pdf` değişmeden installer-only taşıyıcıya eklenir; kurulu hedef `.claude/parser-assets/ocr-probe-tr-en.pdf`, SHA256 `b3eb25619abfd7404835bf172104fae9e711f48088bd56d745d4e84d8a6a3f2a`. Kaynak yeniden üretimi `scripts/vendor_workspace_pypdf.py` ile aynı pinli wheel ve fixture'dan deterministiktir. Yeni allowlist veya ayrı indirme yolu yoktur; asset mevcut installer/update journal'ıyla planlanır, yedeklenir ve geri alınır. Kullanıcı belgeleri paketlenmez. Probe registry/index/yerel kullanıcı ayarı yazmaz; OCR reader yalnız özel geçici kopya oluşturur ve temizler.

## Native önkoşul

Text PDF wheel'i ve stdlib DOCX kurulumdan gelir. OCR ayrıca PATH üzerinde Poppler `pdftoppm`, Tesseract ve Tesseract'ın gördüğü **tur + eng** dil verisini ister. Bunlar wheel'in parçası değildir. Okuyucunun gerçek OCR sonucu eksik aracı `missing_ocr_dependency`, eksik dili `missing_ocr_languages` olarak verir; D probe `missing_dependency` ve somut kurulum/yeniden deneme adımını detail'de korur. Bozuk pinli probe veya wheel `failed` olur. Native araç veya dil paketleri indirilmez; varlıklarını söylemek ready değildir.

`SETUP-V3.md` / `SETUP-WINDOWS.md` ön kontrolleri ve mevcut sistem kurulum yolu kullanılır. `beyin_v3_document_setup.document_setup(vault)` aynı gerçek probe'dan somut sistem paket argv adımlarını üretir. Debian/Ubuntu apt-get, Arch pacman, Fedora dnf, macOS mevcut Homebrew ve Windows mevcut winget yolu vardır. Yeni paket yöneticisi kurulmaz; native kurulum ve ayrıcalık mevcut ajan/OS izin yolunda kalır. Windows'ta Tesseract tur/eng verisi ve yeni süreç PATH'i ayrıca doğrulanır; iki winget komutunun başarısı OCR ready değildir.

`install_v3.py --workspace-documents` raporu kurucu sonucuna bağlar. Dosyalar kurulmuş ama belge yeteneği hazır değilse exit 5; raporda teknik `installed` ile belge sonucu ayrı kalır. Plan modu yazmaz ve yalnız mevcut kurulum readiness'ini raporlar. Native binary/dil dosyaları ZIP'e vendor edilmez. Offline kullanım kurulu araç veya sistem paket önbelleği/deposu ister. Dağıtım imzası, sürüm seçimi ve native geri alma OS paket yöneticisinindir; vault journal'ı eski sistem paketlerini kaldırmaz. C extractor kimliği `TESSDATA_PREFIX` ve tur/eng gerçek hash'lerini de içerir (`d4de964`). Temiz offline bundle kapsamı vaat edilmedi. Üç OS gerçek kurulum/izin/PATH kabulü hâlâ açıktır. Mevcut Linux native kurulumu üzerinde gerçek OCR smoke kanıtı, bu açık işleri kapatmaz. Windows/macOS canlı native kontrolü çalıştırılmadı. Claude canlı kabulü `skipped_by_user`; Linux Codex + Hermes güncel canlı matristir ve bu probe testiyle geçmiş sayılmaz.

## Ajan önkoşulu ve sonraki doğrulama

Balkon örneğindeki `daily_usage_verification_pending` ve `fresh_client_session_pending` uygulama sonrasıdır; skill bunları spec unresolved yerine plan verify_steps / OperationResult üzerinden izler. Planın kullanıcı yetkisi exact write-set oluşunca C1'den alınır. Gerçek eksik event/provision capability'si ready diye eklenmez. Önceki balkon spec/planı tarihli kanıt olarak korundu; uygulanmadı.


## Sistem paket kaynakları

Paket adları için birincil kaynaklar: [Tesseract kurulum kılavuzu](https://tesseract-ocr.github.io/tessdoc/Installation.html), [Homebrew Poppler](https://formulae.brew.sh/formula/poppler), [Homebrew dil verisi](https://formulae.brew.sh/formula/tesseract-lang), [Debian Türkçe veri](https://packages.debian.org/en/trixie/tesseract-ocr-tur), [Arch Türkçe veri](https://archlinux.org/packages/extra/any/tesseract-data-tur/), [Fedora Türkçe veri](https://packages.fedoraproject.org/pkgs/tesseract-tessdata/tesseract-langpack-tur/), [winget Poppler manifestleri](https://github.com/microsoft/winget-pkgs/tree/master/manifests/o/oschwartz10612/Poppler), [winget Tesseract manifestleri](https://github.com/microsoft/winget-pkgs/tree/master/manifests/u/UB-Mannheim/TesseractOCR). Bunlar yerel kurulumun başarılı olduğu kanıtı değildir.
