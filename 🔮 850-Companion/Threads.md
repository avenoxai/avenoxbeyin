# Threads

## Active Threads

### Ana sunucu bakım devamı: iki repo düzeltmesi + sertifika yenileme doğrulaması
- **Durum (2026-09-24):** Sunucu bakımı tamamlandı (güncelleme+reboot+temizlik ~1.5GB; 21 uygulama sağlıklı). Sunucu tarafında iki sessiz hata düzeltildi (twigest ölü vhost, LE auto-renew cron kuruldu). İki bulgu repo düzeltmesi bekliyor, kullanıcı kararıyla yalnız raporlandı: (1) **netadestek-auth** SMTP host kodda 46.224.160.42:465 hardcode → crash-loop (RestartCount=5); (2) **twigest-frontend** proxy hedefi `localhost:8000/api` hardcode → günde 42-210 başarısız proxy. `factory` uygulaması kullanıcı kararıyla kaldırıldı.
- **Sahip:** ajan (repo düzeltmeleri, ilgili proje oturumlarında), kullanıcı (deploy onayı).
- **Sonraki adım:** (1) netadestek-auth repo'sunda SMTP host'u konfigüre edilebilir yap → deploy → test mailiyle crash yokluğu doğrula; (2) twigest-frontend proxy destination'ı `BACKEND_INTERNAL_URL`'e bağla → proxy hata sayısının sıfırlandığını doğrula; (3) hedipage-frontend sertifika yenilemesinin cron'la gerçekleştiğini doğrula (26 Eylül+).
- **Kaynak:** `issues/2026-09-24-ana-sunucu-bakim-ve-sessiz-hata-bulgulari.md`, `entities/ana-sunucu.md`.

### Jev ana sisteme bağlanıyor: `on` modu, kalibrasyon 8/8
- **Durum (2026-09-24, 3. tur):** `jev on` canlı (context/review/answer; auto_context kapalı); anahtar `~/.config/beyin/jev.env` (0600) + `state/jev.json` env_file + timeout 6.0. Eşik kalibrasyonu 8 gerçek iş örneğiyle **8/8**: ACCEPT = supports ∧ conf≥0.85 ∧ asserted; REJECT = contradicts; aksi review.
- **Kapı tasarımı (deep-research isterince):** deterministik ön geçiş (test yoksa model çağrılmadan RET) → tek toplu Jev çağrısı (her checklist maddesi Noul + Choice accept/needs_revision/reject + risk Score) → kod-taraflı politika; her karar rubric+model versiyonu damgalı. "Belirsiz kalma" çekimserliktir (KoBBQ %95 unknown): açık "yetersiz kanıt" opsiyonu eklenecek, belirsiz = review'e yönlendir.
- **Kaynak:** `decisions/2026-09-24-yan-semantik-cache-ve-erisim-karari.md` (kalibrasyon bölümü), `analyses/2026-09-24-jev-rag-reach-deep-research.md`.
- **Sonraki adım:** kapıyı gerçek iş kapanışlarında kullanmaya başla; ilk 20-30 gerçek örnekte politika sapması ölçülürse eşik revize edilir.

### Yan-semantik arama cache'i (hybrid kabul edildi)
- **Durum (2026-09-24, 3. tur):** `hybrid` komutu (motor lexikal + semantik RRF k=60) 14 gerçek sorguda top1 0.57/top3 0.79/MRR 0.69; saf semantik e5-small 0.43/0.57, e5-large 0.36/0.64 → **e5-small korunur**, model swap reddedildi. Anlamlı yazımdan sonra `build` (şu an 470 kayıt, 55.6s).
- **Kaynak:** `decisions/2026-09-24-yan-semantik-cache-ve-erisim-karari.md` (ölçüm tablosu), AGENTS.md bulma akışı.
- **Sonraki adım:** gerçek kullanımda kaçırma kalitesi izlenir; gerekirse reranker (bge-reranker-v2-m3) veya sync-integrasyonu motor update sonrası ele alınır.

### Reddit/X kaynak erişimi (last30days kurulu; X anahtarsız)
- **Durum (2026-09-24):** last30days upstream `~/.agents/skills/last30days` kuruldu (anahtarsız skip yolu); Reddit = RSS + Arctic Shift keyless; X için cookie/bearer yok, opsiyonel. Agent-Reach kurulmadı (login-state Reddit = ToS gri alan). Daemon'suz tek koşu tasarımı raporda.
- **Kaynak:** `analyses/2026-09-24-jev-rag-reach-deep-research.md`.
- **Sonraki adım:** ilk gerçek "last30days" koşusuyla Reddit yolu canlı doğrulanacak.

### leo bellek bütçesi: 512 MB swap + iki omp oturumu
- **Durum (2026-09-25, güncel):** **zram kuruldu (kullanıcı kararı "zram + küçük swap"):** `zram-tools` aktif — `/dev/zram0` 13.6G (prio 100), 512M swapfile korunur; boot'ta otomatik (`zramswap` enabled). STT/whisper 24 Eyl'de tamamen kaldırılmıştı (available 16.9 GB). Whatsie (1.5 GB) dokunulmadan kaldı. Karar: `decisions/2026-09-25-guvenlik-katmani-ve-stabilite.md` + `issues/2026-09-25-swap-512m-oom-chrome.md`.
- **Sahip:** ajan (OOM tekrarı izlemesi), kullanıcı (bellek ağırlı kullanım kararı).
- **Sonraki adım:** OOM-killer tekrarı olup olmadığının izlemesi; tekrar ederse omegeleme (per-app `oom_score_adj`) ayrı kararla.
- **Kaynak:** `swapon --show`, `systemctl is-enabled zramswap`, `journalctl -k` (2026-09-24 18:33:00 OOM).

### Bare-metal Linux göçü
- **Durum:** 23 Eylül itibarıyla karar kesin (kullanıcı beyanı): ana dizüstünde Windows silinip Linux kurulacak. Distro seçildi: **Kubuntu 26.04.1 LTS** (Plasma 6.6 LTS + Bullet-Proof KDE); kullanıcı indirmeye başladı. GNOME memnuniyetsizliği var; Pop!_OS/COSMIC live'da ikinci aday, Zorin elendi. **Kasa göçü tamamlandı (ajan):** beyin kasası `/home/taha/Desktop/beyin` altında çalışıyor — runtime state `~/.local/state/beyin-v3/25623790ad4c6df1` (init + sync 456 kayıt), OMP kancası Linux yollarına çevrildi, `doctor`/`context`/hook enjeksiyonu canlı doğrulandı (commit `9adfdf2`). Bulunan göç-dışı kalıntı: wiki sayfalarının `project_id:` alanı motorun `--project` filtresiyle eşleşmiyor (427/456 kayıt `project: None`); Windows döneminde de böyleydi. **Windows-path temizliği (2026-09-24, ajan):** beyin + Kotivon genel tarama; aktif katman (config, script, hook, entite pointerleri) Linux yollarına çevrildi (commit `4328df5`, `a0e3742`), tarihsel kanıt/raw/receipts korunarak dokunulmadı. Açık göç işleri: `~/.ssh` dizini yok (twigest deploy anahtarı taşınmadı), `~/.config/claude-seo` yok, node/npm yok (hedipage brand PNG üretimi ve twigest Vercel CLI işlemede çalışmaz), a-sirketi-jev'in 19 Eylül Codex transkripti hiçbir yere taşınmamış (kaynak kayıt içindeki Windows yolu canlı değil), motor 3.3.0 güncellemesi mevcut (kullanıcı kararı bekliyor). **FL2000 sürücü (2. tur):** Secure Boot kapandı, modül canlı; EDID gelmediği için KWin'in seçtiği 2560x1080 "kapsam dışı" verdi, 1920x1080@60'a alındı; monitör onayı bekliyor.
- **Tema kararı (2026-09-24, kullanıcı düzeltmesiyle kesin; ajan kurdu):** Ajanın Nordic bluish önerisi kullanıcı tarafından geçersiz kılındı ("çok ruhsuz"); Sweet de elendi ("çok çocuksu"). Kabul: **We10XOS-dark** (yeyushengfan258 ailesi). **Tam yığın canlı:** look-and-feel + We10XOSDark renk + We10XOS-dark plasma stili + aurorae dekor + Win10OS-cursors + We10X ikonlar + duvar kağıdı; Kvantum motorunu kullanıcı kurdu (1.1.5-1), ajan `kvantummanager --set We10XOS-dark` + `widgetStyle=kvantum` ile bağladı (Qt5+Qt6 plugin kanıtlı). Panel düzeni bozulmadı (appletsrc md5 aynı), screenshot ile doğrulandı. İsteğe bağlı: GTK/SDDM eşlemeleri. Karar: `decisions/2026-09-24-kde-tema-we10xos-dark.md`.
- **Sahip:** kullanıcı (kurulum ve deneme), ajan (geçiş planı, tooling taşınması).
- **Sonraki adım:** `project_id` ↔ motor `project` eşleşmesi kullanıcı kararı bekliyor. **FL2000: sürücü 1.15 yazıldı, derlendi, canlı; izleme listesi issue sayfası 11. turda** — (1) 1080p@2bpp uzun dönem wedge sıklığı, (2) KWin record zoo temizliği (dikkatli), (3) ADCDS/fl2000drm'e upstream yama önerisi (kullanıcı kararı), (4) rotation stall sınıfı churn sonrası gözlem. Güncel yerleşim: dikey panel sol (0,0 1080x1920 rot8), ana monitör orta (1080,0), laptop sağ (3640,180); bekçi v6 düzeni churn'de geri getiriyor; fmt=1 RGB565 kalıcı (modprobe.d).
- **Kaynak:** `decisions/2026-09-23-windows-silip-linux-gomus.md`, `sources/2026-09-23-windows-silme-linux-gomus-beyani.md`, `issues/2026-09-23-fl2000-mok-enroll-bekliyor.md`.

### Alacak/tahsilat: Tunahan 1.000 TL kaldı, Avni kapandı, Hakan Us +2.000
- **Durum (2026-09-27, kullanıcı beyanı; banka/makbuz doğrulaması yok):** Avni'nin bekleyen **1.500 TL'si alındı** (18 Eylül "gönderecek" beyanı kapandı). Hakan Us TradingView botu işine **2.000 TL daha** ekstra ödeme attı; iş için beyan edilen toplam **7.000 TL** (3.000 tam bedel + 2.000 + 2.000 ekstra). Eylül beyan edilen iş geliri **12.500 TL**; bekleyen tek alacak **Tunahan 1.000 TL** (5.000'lik alacağın kalanı, vade belirsiz).
- **Sahip:** kullanıcı (takip), ajan (kayıt).
- **Sonraki adım:** Tunahan'ın son 1.000 TL'si gelirse zinciri kapat; vade sorulabilir. Beyanların `raw/sessions/` kopyası hâlâ yok.
- **Kaynak:** `Finans/Gelir Kayıtları.md` (tam tablo), `Müşteriler/Avni.md`, `Müşteriler/Hakan Us.md`; zincir öncesi: `syntheses/2026-09-21-tunahan-tahsilat.md`, `sources/sessions/2026-09-21-tunahan-tahsilat-beyani.md`.

### Beyan oturumlarının ham transkript arşivi
- **Durum:** 21 Eylül beyanının OMP oturum kopyası `raw/sessions/` altında yok; kaynak fiş canlı transkript yolunu (`~/.omp/agent/sessions/...`) işaret ediyor. 18-19 Eylül Hakan Us beyanında da aynı durum vardı.
- **Sahip:** kullanıcı (transkriptleri `raw/sessions/` altına alma alışkanlığı), ajan (fiş güncellemesi).
- **Sonraki adım:** Kopya eklendiğinde ilgili kaynak fişlerin `Sources` bölümünü gerçek vault yoluyla güncelle.
- **Kaynak:** `sources/sessions/2026-09-21-tunahan-tahsilat-beyani.md`, `sources/sessions/2026-09-18-hakanus-tradingview-odeme-beyani.md`.

## Closed Threads
