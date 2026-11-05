# V3 kurulumu: ajan için kısa rehber

Kullanıcı tek mesajlık kurulumu istediyse kanonik, checksum doğrulamalı akış
[beyin.md](beyin.md) dosyasındadır. Bu dosya açılmış release paketi veya repo
checkout'u içinden kurulum için kısa başvurudur.

Kullanıcı bu dosyayı takip etmeni istediğinde mevcut desteklenen istemci oturumunda çalış. Yeni hesap, API anahtarı, Mem0, sunucu veya paket yöneticisi kurma. Ortak motor yalnız Python standart kütüphanesini kullanır.

1. Kullanıcının seçtiği vault yolunu kullan. Belirsizse yalnız hangi klasörü kullanacağını sor. Yeni vault isteniyorsa klasörü oluştur; mevcut notları taşıma veya silme.
2. İşletim sistemini ve çalışan Python'u doğrula: macOS/Linux `python3`, Windows `py -3` veya çalışan `python`. Python 3.11+ gerekir. Yorumlayıcı yoksa tek mesajlık akışta [beyin.md](beyin.md) içindeki resmi işletim sistemi yolunu uygula; kuruluymuş gibi devam etme.
3. Resmi sürüm ZIP dosyasını açtığın paket klasöründen (veya kaynak repo kökünden) çalıştır: `python3 scripts/install_v3.py --vault "VAULT_YOLU"`. Windows'ta aynı komutun başında `py -3` kullan. Installer yerel yedek alır, ortak motoru ve istemci bağlantılarını vault'a kopyalar. Kiro için `.kiro/hooks/beyin-v3.json` adaptörünü içeren kaynak sürümünü veya paketi kullan. DSH kullanılıyorsa kurulumdan sonra profil yamasını elle tamamla ([DSH kurulumu](docs/v3/DSH.md)). Sistem servisi yüklemez.
4. Kurulan CLI ile kontrol et: `python3 "VAULT_YOLU/beyin.py" doctor`. Hataları ve skill çakışmalarını açıkça bildir. Çalışan servis veya gerçek istemci testi olmadan 'her şey sağlıklı' deme.
5. Codex kullanılıyorsa vault klasöründe yeni oturum açıp `/hooks` üzerinden yeni hook tanımlarını incele ve güven. Trust hashlerini elle yazma; kurulumda trust bypass kullanma. Claude için yeni oturum aç. Antigravity içinde vault klasörünü workspace olarak açıp klasöre güven; headless çalıştırırken `--add-dir "VAULT_YOLU"` kullan. Kiro IDE veya CLI V3 için vault klasörünü aç, Agent Hooks panelinde veya `/hooks` ile Beyin hook'larını incele ve yeni oturum başlat ([Kiro kurulumu](docs/v3/KIRO.md)). Modelden, sentetik bir nottaki bilgiyi yalnız hook context üzerinden döndürmesini isteyerek bağlantıyı doğrula. Gerçek özel veriyi test çıktısına alma. Hermes Agent için hook JSON yerine eklenti bağlantısı gerekir: `docs/v3/HERMES.md` içindeki link + `hermes plugins enable beyin-v3` + yeniden başlatma adımlarını uygula. OpenCode için güven adımı yok; vault klasöründe OpenCode açmak eklentiyi yükler (`docs/v3/OPENCODE.md`). OMP için de güven adımı yok; vault klasöründe OMP açmak `.omp/hooks/pre/beyin-v3.ts` hook'unu yükler (`docs/v3/OMP.md`). DSH için `beyin.py doctor --json` çıktısındaki `dsh.manual_setup.block` girdilerini profil yamasına ekle ve `.claude/dsh/beyin-v3-hooks.json` dosyasını kontrol et; profil değişikliği yalnız DSH yeniden başlatılınca etkin olur (`docs/v3/DSH.md`).
6. Kurucu `beyin`, `beyin-doktor` ve `beyin-guncelle` başlangıç skill'lerini kurar. Skill'lerin ortak erişim noktası `.agents/skills/`; Claude karşılığı `.claude/skills/`, Kiro karşılığı `.kiro/skills/`. Mevcut skill'ler korunur. Ek skill için yalnız kullanıcının seçtiği dizini `skill-import --source "SKILL_DIZINI"` ile al. Farklı değişiklik varsa kullanıcı metnini koruyup conflict bildir.
7. Mevcut companion kimliğini koru. Yeni kurulumdaki Core boşsa kısa konuşmayla hitap, çalışma alanı ve düşünme ortağından beklentileri öğren; beyin skill'inin kimlik/süreklilik protokolüyle notlara işle. Mevcut kullanıcıya yeniden onboarding yapma. Kurucu başlangıç notlarını yalnız bir kez oluşturur; bunlar kullanıcıya aittir ve güncellemede ezilmez.
8. İş bitince kullanıcıya yalnız vault yolu, çalışan istemci bağlantıları ve varsa tek sonraki adımı söyle. JSON/debug dökümü, global ayarlar veya kişisel bilgileri sohbet çıktısına taşıma.

## Kullanıcıya açıklanacak sınır

Not kaydedildiğinde dosya kalıcıdır. İndeks oturum başı, mesaj gönderimi ve tur sonu gibi hook noktalarında tazelenir; istemciler kapalıyken sürekli arka plan hizmeti çalışmaz. Gerektiğinde kurulan CLI `sync` komutu anında tazeler. Çalışma sonuçları yapılandırılmış receipt ile kaydedilir; bütün özel sohbet geçmişi kendiliğinden içeri alınmaz.

Basit scalar YAML ve JSON frontmatter desteklenir. Karmaşık YAML görünür uyarı üretir; sessizce yanlış metadata çıkarılmaz. Önce kaynak not okunarak ihtiyaç duyulan alanlar anlaşılır. Yeni görev `task-create --file TASK_JSON` ile oluşturulur; JSON şeması kurulu beyin skill'indedir. Kaynak task güncellemesi `task-update` ile expected revision kullanır; conflict durumunda güncel kaydı oku.

İsteğe bağlı Jev danışmanı kurulumun parçası değildir: varsayılan kapalıdır, yalnız kendi TypeSafe API anahtarı olan kullanıcılar içindir ve kurucu bunu sormaz. Yerel Laya sağlayıcısı da aynı şekilde isteğe bağlıdır ve yalnız gölge modda çalışır (`jev shadow --provider laya`; `jev on --provider laya` reddedilir); Laya'yı veya PyTorch'u Beyin'in ortamına kurma, kullanıcı isterse JEV.md'deki ayrı kurulum adımlarını izle. Kullanıcı açıkça istemedikçe gündeme getirme; isterse `python3 beyin.py jev status` durumu gösterir, `jev shadow`/`jev on`/`jev off` değiştirir. Anahtarı komuta yazma. Sınırlar: [docs/v3/JEV.md](docs/v3/JEV.md).

Güncelleme: vault içinde `python3 beyin.py update`; yalnız kontrol için `update --check`. Geri alma: `python3 beyin.py rollback`. Kesilen güncellemeyi sürdürmek için `python3 beyin.py recover`. Windows'ta `py -3` kullan. Değiştirilmiş yönetilen dosyada conflict varsa kullanıcı değişikliğini koru; `git reset` veya elle dosya kopyalayarak geçme. İlk temiz kurulum geri alınınca giriş komutu da kaldırılır; tekrar kurmak için resmi ZIP installer'ını kullan. Ayrıntılar: [güncelleme rehberi](docs/v3/UPDATE.md).

Platform kanıtı ve kalan doğrulamalar: [docs/v3/PLATFORM-TESTS.md](docs/v3/PLATFORM-TESTS.md).

## Kullanıcının tüketim tercihi

Kurulumdan sonra kullanıcı isterse beyin skill'i ile Normal/Ekonomik/Manuel tercihini uygula. Varsayılanı sormadan değiştirme. “15 dakikada kontrol” yerel olay kontrolleri arasındaki minimum aralıktır; model çağıran periyodik iş kurma. `beyin.py preferences` mevcut ayarı gösterir. [Tercihler](docs/v3/PREFERENCES.md).

Yeni kurulum ve V3.1 güncellemesi sonrası sürüm bildirimi varsayılan açık: günde en fazla bir kez GitHub sürüm bilgisi okunur, not gönderilmez, otomatik kurulum yapılmaz. Kapatma: `python3 beyin.py preferences --update-notifications off`. Eski V3.0.2 için ilk geçiş `python3 beyin.py update` komutudur.
