"""Static page, style and script of the optional web view (beyin_v3_web.py).

Shipped as Python text because the update package carries only beyin_v3*.py runtime files.
Nothing here loads from the network: no CDN, no web font, no remote image. The script builds
every element with textContent, never innerHTML, so vault text cannot become markup; the CSP
in beyin_v3_web.py allows only these same-origin files. The palette follows the contributed
"Citrus" design (#423): dark slate surfaces with a lemon accent.
"""
import html

APP_CSS = r"""
:root {
  --zemin: #0a0e14; --panel: #161b22; --panel-2: #1c2026; --cizgi: #30363d;
  --metin: #dfe2eb; --soluk: #8b949e; --limon: #f9d71c; --limon-koyu: #f5c518;
  --yesil: #67df70; --kirmizi: #ffb4ab; --mavi: #8ab4f8;
  --yazi: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--zemin); color: var(--metin); font: 14px/1.5 var(--yazi); }
a { color: var(--limon); }
button, input, select, textarea { font: inherit; color: inherit; }
button { background: var(--limon); color: var(--zemin); border: 0; border-radius: 4px; padding: 6px 12px;
  font-weight: 600; cursor: pointer; }
button:hover { background: var(--limon-koyu); }
button:disabled { opacity: .5; cursor: not-allowed; }
button.ikincil { background: transparent; color: var(--metin); border: 1px solid var(--cizgi); }
button.ikincil:hover { border-color: var(--limon); color: var(--limon); }
input, select, textarea { background: var(--zemin); border: 1px solid var(--cizgi); border-radius: 4px; padding: 6px 8px; }
input:focus, select:focus, textarea:focus, button:focus-visible { outline: 2px solid var(--limon); outline-offset: 1px; }
.kabuk { display: grid; grid-template-columns: 220px 1fr; min-height: 100vh; }
.yan { background: var(--zemin); border-right: 1px solid var(--cizgi); padding: 16px 12px; display: flex;
  flex-direction: column; gap: 16px; position: sticky; top: 0; height: 100vh; }
.marka { display: flex; gap: 10px; align-items: center; }
.marka .limon { font-size: 26px; }
.marka small { display: block; color: var(--soluk); font-family: var(--mono); font-size: 11px; word-break: break-all; }
.yan nav { display: flex; flex-direction: column; gap: 2px; }
.yan nav a { color: var(--metin); text-decoration: none; padding: 7px 10px; border-radius: 4px; }
.yan nav a:hover { background: var(--panel); color: var(--limon); }
.yan nav a.secili { background: var(--panel-2); color: var(--limon); box-shadow: inset 2px 0 0 var(--limon); font-weight: 600; }
.yan .alt { margin-top: auto; display: flex; flex-direction: column; gap: 8px; }
main { padding: 20px 24px; min-width: 0; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 0 0 10px; color: var(--limon); }
.aciklama { color: var(--soluk); margin: 0 0 16px; }
.gorunum[hidden] { display: none; }
.izgara { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px; }
.kart { background: var(--panel); border: 1px solid var(--cizgi); border-radius: 6px; padding: 12px 14px; }
.kart .etiket { color: var(--soluk); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.kart .deger { font-family: var(--mono); font-size: 22px; font-weight: 600; }
.rozet { display: inline-block; font-family: var(--mono); font-size: 11px; padding: 1px 7px; border-radius: 10px;
  border: 1px solid var(--cizgi); color: var(--soluk); }
.rozet.iyi { color: var(--yesil); border-color: var(--yesil); }
.rozet.uyari { color: var(--limon); border-color: var(--limon); }
.rozet.kotu { color: var(--kirmizi); border-color: var(--kirmizi); }
.liste { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.liste li { background: var(--panel); border: 1px solid var(--cizgi); border-radius: 6px; padding: 10px 12px; }
.ust-bilgi { color: var(--soluk); font-family: var(--mono); font-size: 11px; display: flex; gap: 8px; flex-wrap: wrap; }
.ozet { white-space: pre-wrap; overflow-wrap: anywhere; }
.arac-cubugu { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
.pano { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(220px, 1fr); gap: 10px; overflow-x: auto; padding-bottom: 8px; }
.sutun { background: var(--panel); border: 1px solid var(--cizgi); border-radius: 6px; padding: 10px; min-height: 120px; }
.sutun h3 { font-size: 12px; margin: 0 0 8px; color: var(--soluk); text-transform: uppercase; letter-spacing: .05em;
  display: flex; justify-content: space-between; }
.gorev { background: var(--panel-2); border: 1px solid var(--cizgi); border-radius: 5px; padding: 8px; margin-bottom: 8px; }
.gorev strong { display: block; overflow-wrap: anywhere; }
.gorev select { width: 100%; margin-top: 6px; }
form.form { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); align-items: end;
  background: var(--panel); border: 1px solid var(--cizgi); border-radius: 6px; padding: 12px; margin-bottom: 16px; }
form.form label { display: flex; flex-direction: column; gap: 4px; color: var(--soluk); font-size: 12px; }
form.form .genis { grid-column: 1 / -1; }
.ikili { display: grid; grid-template-columns: minmax(260px, 1fr) minmax(300px, 1.3fr); gap: 16px; }
.sonuc { cursor: pointer; }
.sonuc:hover, .sonuc:focus { border-color: var(--limon); outline: none; }
pre.onizleme { background: var(--panel); border: 1px solid var(--cizgi); border-radius: 6px; padding: 12px;
  white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--mono); font-size: 12px; max-height: 70vh; overflow: auto; margin: 0; }
meter { width: 100%; height: 10px; }
.bos { color: var(--soluk); font-style: italic; }
#bildirim { position: fixed; right: 16px; bottom: 16px; display: flex; flex-direction: column; gap: 6px; z-index: 10; }
#bildirim div { background: var(--panel); border: 1px solid var(--limon); border-radius: 6px; padding: 8px 12px; max-width: 420px; }
#bildirim div.hata { border-color: var(--kirmizi); }
.kilit { min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.kilit .marka { justify-content: center; }
.kilit .kart { max-width: 420px; width: 100%; text-align: center; padding: 28px; }
.kilit form { display: flex; flex-direction: column; gap: 10px; margin-top: 14px; }
.kapandi { position: fixed; inset: 0; background: rgba(10, 14, 20, .94); display: flex; align-items: center; justify-content: center;
  font-size: 18px; z-index: 20; }
@media (max-width: 760px) { .kabuk { grid-template-columns: 1fr; } .yan { position: static; height: auto; } .ikili { grid-template-columns: 1fr; } }
"""

APP_JS = r"""
'use strict';
(function () {
  const meta = function (name) { const node = document.querySelector('meta[name="' + name + '"]'); return node ? node.content : ''; };
  const CSRF = meta('beyin-csrf');
  const SALT_OKUNUR = meta('beyin-salt-okunur') === '1';
  const DURUM = { inbox: 'Gelen', active: 'Aktif', waiting: 'Bekliyor', blocked: 'Engellendi', done: 'Bitti', cancelled: 'İptal' };
  const TUR = { task: 'görev', note: 'not', receipt: 'receipt', inference: 'çıkarım', preference: 'tercih' };
  const DOKTOR = { ok: ['Sağlıklı', 'iyi'], never_seen: ['Hook henüz çalışmadı', 'uyari'], degraded: ['Dikkat gerekiyor', 'uyari'],
    failed: ['Hata', 'kotu'], invalid: ['Geçersiz', 'kotu'] };
  const HAFIZA = { within_limit: 'Sınır içinde', limit_off: 'Sınır kapalı', planned: 'Taşınacak', needs_rewrite: 'Elle kısaltılmalı',
    needs_attention: 'Dikkat', compacted: 'Taşındı', ok: 'Sağlıklı', dry_run: 'Önizleme', conflict: 'Başka bir işlem sürüyor' };
  if (!CSRF) { return; }

  function el(tag, props) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(function (pair) {
      const key = pair[0], value = pair[1];
      if (value === undefined || value === null) { return; }
      if (key === 'text') { node.textContent = String(value); }
      else if (key === 'class') { node.className = value; }
      else if (key in node) { node[key] = value; }
      else { node.setAttribute(key, String(value)); }
    });
    for (let i = 2; i < arguments.length; i += 1) {
      const child = arguments[i];
      if (child === null || child === undefined || child === false) { continue; }
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  function bosalt(node) { while (node.firstChild) { node.removeChild(node.firstChild); } return node; }
  function $(id) { return document.getElementById(id); }
  function zaman(value) {
    if (!value) { return ''; }
    const date = new Date(value);
    return isNaN(date) ? String(value) : date.toLocaleString('tr-TR', { dateStyle: 'medium', timeStyle: 'short' });
  }
  function bildir(text, hata) {
    const item = el('div', { class: hata ? 'hata' : '', role: hata ? 'alert' : 'status', text: text });
    $('bildirim').append(item);
    setTimeout(function () { item.remove(); }, hata ? 7000 : 3500);
  }

  async function api(path, body) {
    const options = { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-Beyin-CSRF'] = CSRF;
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (response.status === 401) { window.location.replace('/'); }
    if (!response.ok) { throw new Error((data && data.mesaj) || ('HTTP ' + response.status)); }
    return data;
  }

  // --- kokpit -----------------------------------------------------------
  function kart(etiket, deger, rozet) {
    return el('div', { class: 'kart' }, el('div', { class: 'etiket', text: etiket }),
      el('div', { class: 'deger', text: deger }), rozet || null);
  }
  async function kokpit() {
    const alan = bosalt($('kokpit-kartlar'));
    alan.append(el('p', { class: 'bos', text: 'Yükleniyor…' }));
    const sonuclar = await Promise.allSettled([api('/api/doktor'), api('/api/gorevler'), api('/api/makbuzlar')]);
    bosalt(alan);
    if (sonuclar[0].status === 'fulfilled') {
      const d = sonuclar[0].value;
      const durum = DOKTOR[d.durum] || [d.durum || 'bilinmiyor', 'uyari'];
      alan.append(kart('Doktor', durum[0], el('span', { class: 'rozet ' + durum[1], text: d.durum || '?' })));
      alan.append(kart('Bekleyen hook olayı', String(d.bekleyen_olay ?? 0)));
      alan.append(kart('Ayıklanan gizli bilgi', String(d.ayiklanan_gizli ?? 0)));
      const asan = (d.hafiza && d.hafiza.asan) || [];
      alan.append(kart('Hafıza dosyaları', asan.length ? asan.length + ' dosya sınırı aşıyor' : 'Sınır içinde',
        el('span', { class: 'rozet ' + (asan.length ? 'uyari' : 'iyi'), text: asan.join(', ') || 'tamam' })));
    } else {
      alan.append(kart('Doktor', 'Okunamadı', el('span', { class: 'rozet kotu', text: sonuclar[0].reason.message })));
    }
    if (sonuclar[1].status === 'fulfilled') {
      const sayim = {};
      sonuclar[1].value.gorevler.forEach(function (g) { sayim[g.durum] = (sayim[g.durum] || 0) + 1; });
      const acik = (sayim.inbox || 0) + (sayim.active || 0) + (sayim.waiting || 0) + (sayim.blocked || 0);
      alan.append(kart('Açık görev', String(acik), el('span', { class: 'rozet', text: (sayim.active || 0) + ' aktif · ' + (sayim.blocked || 0) + ' engellenmiş' })));
    }
    const liste = bosalt($('kokpit-makbuzlar'));
    if (sonuclar[2].status === 'fulfilled') {
      const items = sonuclar[2].value.makbuzlar.slice(0, 5);
      if (!items.length) { liste.append(el('li', { class: 'bos', text: 'Son ' + sonuclar[2].value.gun + ' günde receipt yok.' })); }
      items.forEach(function (m) { liste.append(makbuzSatiri(m)); });
    } else {
      liste.append(el('li', { class: 'bos', text: sonuclar[2].reason.message }));
    }
  }

  // --- görevler ---------------------------------------------------------
  let gorevVerisi = null;
  function durumSecici(secili, durumlar) {
    const secim = el('select', { 'aria-label': 'Durum', disabled: SALT_OKUNUR });
    durumlar.forEach(function (d) { secim.append(el('option', { value: d, text: DURUM[d] || d, selected: d === secili })); });
    return secim;
  }
  function gorevKarti(g, durumlar) {
    const secim = durumSecici(g.durum, durumlar);
    secim.addEventListener('change', async function () {
      secim.disabled = true;
      try {
        await api('/api/gorev/durum', { id: g.id, revizyon: g.revizyon, durum: secim.value });
        bildir('Görev güncellendi: ' + g.baslik);
      } catch (error) { bildir('Güncellenemedi: ' + error.message, true); }
      gorevler();
    });
    const bilgi = el('div', { class: 'ust-bilgi' });
    if (g.proje) { bilgi.append(el('span', { text: '#' + g.proje })); }
    if (g.sorumlu) { bilgi.append(el('span', { text: '@' + g.sorumlu })); }
    if (g.bitis) { bilgi.append(el('span', { text: 'bitiş ' + String(g.bitis).slice(0, 10) })); }
    if (g.oncelik) { bilgi.append(el('span', { text: 'öncelik ' + g.oncelik })); }
    if (g.kati) { bilgi.append(el('span', { text: 'kanıtlı kapanış' })); }
    return el('div', { class: 'gorev', title: g.kaynak || '' }, el('strong', { text: g.baslik }), bilgi, secim);
  }
  function panoyuCiz() {
    if (!gorevVerisi) { return; }
    const filtre = $('proje-filtre').value;
    const pano = bosalt($('gorev-pano'));
    gorevVerisi.durumlar.forEach(function (d) {
      const kartlar = gorevVerisi.gorevler.filter(function (g) { return g.durum === d && (!filtre || g.proje === filtre); });
      const sutun = el('div', { class: 'sutun' }, el('h3', {}, el('span', { text: DURUM[d] || d }), el('span', { text: String(kartlar.length) })));
      kartlar.forEach(function (g) { sutun.append(gorevKarti(g, gorevVerisi.durumlar)); });
      pano.append(sutun);
    });
  }
  async function gorevler() {
    try { gorevVerisi = await api('/api/gorevler'); }
    catch (error) { bosalt($('gorev-pano')).append(el('p', { class: 'bos', text: error.message })); return; }
    const projeler = Array.from(new Set(gorevVerisi.gorevler.map(function (g) { return g.proje; }).filter(Boolean))).sort();
    const filtre = $('proje-filtre'); const onceki = filtre.value;
    bosalt(filtre).append(el('option', { value: '', text: 'Tüm projeler' }));
    projeler.forEach(function (p) { filtre.append(el('option', { value: p, text: p, selected: p === onceki })); });
    const liste = bosalt($('proje-onerileri'));
    projeler.forEach(function (p) { liste.append(el('option', { value: p })); });
    const yeniDurum = $('yeni-durum');
    if (!yeniDurum.options.length) {
      gorevVerisi.durumlar.forEach(function (d) { yeniDurum.append(el('option', { value: d, text: DURUM[d] || d, selected: d === 'inbox' })); });
    }
    const not = [];
    if (gorevVerisi.eski) { not.push(gorevVerisi.eski + ' görev dosyası dizinden sonra değişti; Eşitle ile güncelle.'); }
    if (gorevVerisi.kisaltildi) { not.push('İlk ' + gorevVerisi.gorevler.length + ' görev gösteriliyor (toplam ' + gorevVerisi.toplam + ').'); }
    $('gorev-notu').textContent = not.join(' ');
    panoyuCiz();
  }
  async function gorevOlustur(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const veri = { baslik: form.baslik.value, aciklama: form.aciklama.value, sorumlu: form.sorumlu.value,
      proje: form.proje.value, durum: form.durum.value };
    form.querySelector('button').disabled = true;
    try {
      const sonuc = await api('/api/gorev', veri);
      bildir('Görev oluşturuldu: ' + sonuc.gorev.baslik);
      form.baslik.value = ''; form.aciklama.value = '';
      gorevler();
    } catch (error) { bildir('Görev oluşturulamadı: ' + error.message, true); }
    form.querySelector('button').disabled = false;
  }

  // --- arama ------------------------------------------------------------
  async function ara(event) {
    event.preventDefault();
    const sorgu = $('arama-sorgu').value.trim();
    const liste = bosalt($('arama-sonuclari'));
    if (sorgu.length < 2) { liste.append(el('li', { class: 'bos', text: 'En az iki harf yaz.' })); return; }
    try {
      const veri = await api('/api/ara?q=' + encodeURIComponent(sorgu));
      if (!veri.sonuclar.length) { liste.append(el('li', { class: 'bos', text: 'Sonuç yok.' })); }
      veri.sonuclar.forEach(function (s) {
        const satir = el('li', { class: 'sonuc', tabIndex: 0 },
          el('strong', { text: s.baslik }),
          el('div', { class: 'ust-bilgi' }, el('span', { text: TUR[s.tur] || s.tur || 'not' }), el('span', { text: s.kaynak || '' })),
          el('div', { class: 'ozet', text: s.ozet }));
        const ac = function () { onizle(s.id); };
        satir.addEventListener('click', ac);
        satir.addEventListener('keydown', function (e) { if (e.key === 'Enter') { ac(); } });
        liste.append(satir);
      });
    } catch (error) { liste.append(el('li', { class: 'bos', text: error.message })); }
  }
  async function onizle(id) {
    const alan = $('onizleme'); const baslik = $('onizleme-baslik');
    try {
      const kayit = await api('/api/kayit?id=' + encodeURIComponent(id));
      baslik.textContent = kayit.baslik + ' (' + kayit.kaynak + ')';
      alan.textContent = kayit.metin + (kayit.kisaltildi ? '\n\n[Kısaltıldı]' : '');
    } catch (error) { baslik.textContent = 'Önizleme'; alan.textContent = error.message; }
  }

  // --- receipt'ler --------------------------------------------------------
  function makbuzSatiri(m) {
    const bilgi = el('div', { class: 'ust-bilgi' }, el('span', { text: zaman(m.zaman) }));
    (m.kaynaklar || []).slice(0, 6).forEach(function (k) { bilgi.append(el('span', { text: k })); });
    const gizli = m.gizlenen || {};
    if (gizli.private) { bilgi.append(el('span', { text: gizli.private + ' gizli kaynak gösterilmiyor' })); }
    return el('li', {}, bilgi, el('div', { class: 'ozet', text: m.ozet_gizli ? 'Özet gizli bir kaynağa bağlı; gösterilmiyor.' : m.ozet }));
  }
  async function makbuzlar() {
    const liste = bosalt($('makbuz-listesi'));
    try {
      const veri = await api('/api/makbuzlar');
      $('makbuz-notu').textContent = 'Son ' + veri.gun + ' gün, ' + veri.toplam + ' receipt. Özetler ajan beyanıdır; kaynaklarla doğrula.';
      if (!veri.makbuzlar.length) { liste.append(el('li', { class: 'bos', text: 'Receipt yok.' })); }
      veri.makbuzlar.forEach(function (m) { liste.append(makbuzSatiri(m)); });
    } catch (error) { liste.append(el('li', { class: 'bos', text: error.message })); }
  }

  // --- hafıza -----------------------------------------------------------
  async function hafiza() {
    const liste = bosalt($('hafiza-listesi'));
    try {
      const veri = await api('/api/hafiza');
      $('hafiza-notu').textContent = (veri.dizin ? veri.dizin + ': ' : '') + (HAFIZA[veri.durum] || veri.durum || '') + (veri.neden ? ' · ' + veri.neden : '');
      if (!veri.dosyalar.length) { liste.append(el('li', { class: 'bos', text: 'Sınırı olan hafıza dosyası yok.' })); }
      veri.dosyalar.forEach(function (f) {
        const olcu = el('meter', { min: 0, max: f.sinir || 1, value: Math.min(f.karakter || 0, f.sinir || 1) });
        const tasinacak = f.tasinacak ? ' · taşınacak ' + f.tasinacak + ' karakter' : '';
        liste.append(el('li', {}, el('strong', { text: f.ad }),
          el('div', { class: 'ust-bilgi' }, el('span', { text: (f.karakter ?? 0) + ' / ' + (f.sinir || 'sınırsız') + ' karakter' + tasinacak }),
            el('span', { class: 'rozet', text: HAFIZA[f.durum] || f.durum || '' })), olcu));
      });
    } catch (error) { liste.append(el('li', { class: 'bos', text: error.message })); }
  }
  async function sikistir() {
    if (!window.confirm('Eski Last-Session/Threads girdileri arşive aynen taşınacak; hiçbir şey silinmez. Devam edilsin mi?')) { return; }
    try {
      const sonuc = await api('/api/hafiza/sikistir', {});
      bildir('Sıkıştırma: ' + (HAFIZA[sonuc.durum] || sonuc.durum) + (sonuc.neden ? ' · ' + sonuc.neden : ''));
    } catch (error) { bildir('Sıkıştırılamadı: ' + error.message, true); }
    hafiza();
  }

  // --- gezinme ------------------------------------------------------------
  const YUKLE = { kokpit: kokpit, gorevler: gorevler, arama: null, makbuzlar: makbuzlar, hafiza: hafiza };
  function goster() {
    let ad = window.location.hash.replace('#', '');
    if (!(ad in YUKLE)) { ad = 'kokpit'; }
    document.querySelectorAll('.gorunum').forEach(function (g) { g.hidden = g.id !== 'gorunum-' + ad; });
    document.querySelectorAll('.yan nav a').forEach(function (a) { a.classList.toggle('secili', a.dataset.gorunum === ad); });
    if (YUKLE[ad]) { YUKLE[ad]().catch(function (error) { bildir(error.message, true); }); }
  }
  async function esitle() {
    const dugme = $('esitle'); dugme.disabled = true;
    try {
      const sonuc = await api('/api/esitle', {});
      bildir('Eşitleme: ' + sonuc.durum + ' (' + (sonuc.dizinlenen ?? 0) + ' kaynak, ' + sonuc.cakisma + ' çakışma)');
      goster();
    } catch (error) { bildir('Eşitlenemedi: ' + error.message, true); }
    dugme.disabled = SALT_OKUNUR;
  }
  async function kapat() {
    if (!window.confirm('Web sunucusu kapatılsın mı?')) { return; }
    try { await api('/api/kapat', {}); } catch (error) { bildir(error.message, true); return; }
    document.body.append(el('div', { class: 'kapandi', text: 'Sunucu kapandı. Yeniden açmak için: beyin.py web ac' }));
  }
  let sonEtkinlik = 0;
  function etkinlik() {
    const simdi = Date.now();
    if (simdi - sonEtkinlik < 60000) { return; }
    sonEtkinlik = simdi;
    api('/api/etkinlik', {}).catch(function () {});
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('mod-rozeti').textContent = SALT_OKUNUR ? 'Salt okunur' : 'Yazma açık';
    $('mod-rozeti').classList.add(SALT_OKUNUR ? 'iyi' : 'uyari');
    $('esitle').disabled = SALT_OKUNUR;
    $('sikistir').disabled = SALT_OKUNUR;
    $('yeni-gorev').hidden = SALT_OKUNUR;
    $('esitle').addEventListener('click', esitle);
    $('kapat').addEventListener('click', kapat);
    $('sikistir').addEventListener('click', sikistir);
    $('yeni-gorev').addEventListener('submit', gorevOlustur);
    $('arama-formu').addEventListener('submit', ara);
    $('proje-filtre').addEventListener('change', panoyuCiz);
    ['keydown', 'pointerdown', 'wheel'].forEach(function (name) { window.addEventListener(name, etkinlik, { passive: true }); });
    window.addEventListener('hashchange', goster);
    api('/api/durum').then(function (d) {
      $('kasa-adi').textContent = d.kasa + (d.surum ? ' · v' + d.surum : '');
      $('bosta-notu').textContent = d.bosta_kapanma_dk + ' dakika kullanılmazsa kapanır.';
    }).catch(function () {});
    goster();
  });
})();
"""

_INDEX = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="beyin-csrf" content="{csrf}">
<meta name="beyin-salt-okunur" content="{read_only}">
<title>Beyin Web</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="/app.css">
<script src="/app.js" defer></script>
</head>
<body>
<div class="kabuk">
  <aside class="yan">
    <div class="marka"><span class="limon" aria-hidden="true">🍋</span><div><strong>Beyin Web</strong><small id="kasa-adi"></small></div></div>
    <nav aria-label="Bölümler">
      <a href="#kokpit" data-gorunum="kokpit">Kokpit</a>
      <a href="#gorevler" data-gorunum="gorevler">Görevler</a>
      <a href="#arama" data-gorunum="arama">Arama</a>
      <a href="#makbuzlar" data-gorunum="makbuzlar">Receipt'ler</a>
      <a href="#hafiza" data-gorunum="hafiza">Hafıza</a>
    </nav>
    <div class="alt">
      <span id="mod-rozeti" class="rozet"></span>
      <button id="esitle" type="button">Eşitle</button>
      <button id="kapat" type="button" class="ikincil">Sunucuyu kapat</button>
      <small id="bosta-notu" class="aciklama"></small>
    </div>
  </aside>
  <main>
    <section id="gorunum-kokpit" class="gorunum">
      <h1>Kokpit</h1>
      <p class="aciklama">Doktor özeti, açık görevler ve son receipt'ler. Gizli (private) ve hariç tutulan notlar bu görünüme hiç gelmez.</p>
      <div id="kokpit-kartlar" class="izgara"></div>
      <h2>Son receipt'ler</h2>
      <ul id="kokpit-makbuzlar" class="liste"></ul>
    </section>
    <section id="gorunum-gorevler" class="gorunum" hidden>
      <h1>Görevler</h1>
      <p class="aciklama">tasks/ altındaki görevler. Yeni görev ve durum değişikliği, CLI'daki task-create ve task-update komutlarıyla aynı yoldan yazılır.</p>
      <form id="yeni-gorev" class="form" autocomplete="off">
        <label>Başlık<input name="baslik" required maxlength="160"></label>
        <label>Sorumlu<input name="sorumlu" required maxlength="80" placeholder="ör. ben"></label>
        <label>Proje<input name="proje" maxlength="80" list="proje-onerileri"></label>
        <label>Durum<select name="durum" id="yeni-durum"></select></label>
        <label class="genis">Açıklama<textarea name="aciklama" rows="2" maxlength="4000"></textarea></label>
        <div><button type="submit">Görev oluştur</button></div>
      </form>
      <datalist id="proje-onerileri"></datalist>
      <div class="arac-cubugu"><label>Proje <select id="proje-filtre"></select></label><span id="gorev-notu" class="aciklama"></span></div>
      <div id="gorev-pano" class="pano"></div>
    </section>
    <section id="gorunum-arama" class="gorunum" hidden>
      <h1>Arama</h1>
      <p class="aciklama">Ajanların gördüğü dizinde arar: gizli, güvenilmeyen ve hassas klasördeki notlar sonuçlara girmez.</p>
      <form id="arama-formu" class="arac-cubugu"><input id="arama-sorgu" type="search" maxlength="200" placeholder="Ara…" aria-label="Arama"><button type="submit">Ara</button></form>
      <div class="ikili">
        <ul id="arama-sonuclari" class="liste"></ul>
        <div><h2 id="onizleme-baslik">Önizleme</h2><pre id="onizleme" class="onizleme"></pre></div>
      </div>
    </section>
    <section id="gorunum-makbuzlar" class="gorunum" hidden>
      <h1>Receipt'ler</h1>
      <p id="makbuz-notu" class="aciklama"></p>
      <ul id="makbuz-listesi" class="liste"></ul>
    </section>
    <section id="gorunum-hafiza" class="gorunum" hidden>
      <h1>Hafıza</h1>
      <p class="aciklama">Last-Session ve Threads boyutları. Sıkıştırma, eski girdileri özel arşive aynen taşır (beyin.py companion-compact); hiçbir şey silinmez.</p>
      <p id="hafiza-notu" class="aciklama"></p>
      <ul id="hafiza-listesi" class="liste"></ul>
      <p><button id="sikistir" type="button">Sıkıştır</button></p>
    </section>
  </main>
</div>
<div id="bildirim" aria-live="polite"></div>
</body>
</html>
"""

_UNLOCK = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beyin Web: oturum</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="/app.css">
</head>
<body>
<div class="kilit"><div class="kart">
  <div class="marka"><span class="limon" aria-hidden="true">🍋</span><strong>Beyin Web</strong></div>
  <p>{message}</p>
  <form method="get" action="/">
    <input type="password" name="token" required autocomplete="off" aria-label="Oturum belirteci" placeholder="Oturum belirteci">
    <button type="submit">Aç</button>
  </form>
</div></div>
</body>
</html>
"""


def index_page(csrf, read_only):
    return _INDEX.format(csrf=html.escape(csrf, quote=True), read_only='1' if read_only else '0')


def unlock_page(failed):
    message = ('Belirteç geçersiz ya da bu sunucu yeniden başladı. ' if failed else '') + \
        'Oturum açmak için terminalde beyin.py web ac çalıştır ya da yazdırdığı adresteki belirteci gir.'
    return _UNLOCK.format(message=html.escape(message))
