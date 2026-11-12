---
name: gecmis-import
description: Eski sohbet geçmişini (ChatGPT, Claude, Gemini Takeout dışa aktarımları) beyin kasasının günlük log biçimine çevirip daily/ altına yazar. Tetik - "geçmiş import", "geçmişimi aktar", "takeout", "ChatGPT geçmişi", "eski sohbetlerimi beyne yükle", "import chat history". Kullanma - tek not kaydetmek veya kasada aramak (beyin), sağlık kontrolü (beyin-doktor).
---

# Geçmiş İçe Aktarımı

Eski sohbet arşivini beynin normal hattına sokar. Dışa aktarım dosyası yerelde okunur,
sohbetler aylara bölünür ve `daily/import-YYYY-MM-part-NNN.md` dosyalarına yazılır.
Akşam derleyicisi bu dosyaların içeriğini normal günlük loglar gibi okur ve özetleme için
kullanıcının kendi Claude aboneliği üzerinden Claude'a gönderir. Dışa aktarım dosyasının
kendisi yüklenmez ve veriler başka hiçbir yere gönderilmez.

Özel veya hassas sohbetler, kullanıcı tarih ya da anahtar kelime filtresiyle hariç tutmadıkça
içe aktarıma ve sonraki derlemeye dahil olur.

## Zorunlu onay kapısı

Agent aşağıdaki adımları sırayla tamamlamak ZORUNDADIR. Kullanıcının izni olmadan dışa aktarım
dosyasını açmak, ayrıştırmak veya `daily/` altına dosya yazmak YASAKTIR.

1. Agent veri akışını açıkça anlatmak ZORUNDADIR: dışa aktarım yerelde okunur, seçilen sohbetler
   yerel `daily/` dosyalarına yazılır, akşam derleyicisi bu dosyaların içeriğini özetleme için
   kullanıcının kendi Claude aboneliği üzerinden Claude'a gönderir. Bunun dışında hiçbir yere
   yükleme veya gönderim yapılmaz.
2. Agent özel ve hassas sohbetlerin varsayılan olarak dahil olacağını söylemek ZORUNDADIR.
   Kullanıcıya başlangıç ve bitiş tarihi sınırı ile bir veya daha fazla anahtar kelimeden oluşan
   hariç tutma listesi sunmak ZORUNDADIR. Eşleşme büyük küçük harf duyarsızdır ve eşleşen
   konuşmanın tamamı atlanır.
3. Agent dışa aktarım dosyasının yolunu kullanıcıdan istemek ve yolu yalnızca metin olarak almak
   ZORUNDADIR. Yolu tahmin edemez veya dosyayı henüz açamaz. Ardından yalnızca yerel, salt okunur
   önizleme taraması için açık izin istemek ve yanıtı beklemek ZORUNDADIR. Açık izin yoksa
   dosyayı açamaz. İzin verilirse betiği `--preview` ve seçilen filtrelerle çalıştırır. Bu mod
   hiçbir çıktı dosyası yazmaz.
4. Agent önizlemedeki içe aktarılacak sohbet sayısını, tarih aralığını ve tarih, anahtar kelime,
   boş içerik veya geçersiz kayıt nedeniyle atlanacak sayıları kullanıcıya göstermek ZORUNDADIR.
5. Agent kullanıcıya dürüst vazgeçme seçeneklerini de söylemek ZORUNDADIR: içe aktarılan
   dosyaları hiç derlemeyebilir veya akşam derlemesinden önce istemediği aylık parça dosyasını
   silebilir. Kullanıcı vazgeçerse işlem durur.
6. Agent seçilen tarih aralığını ve anahtar kelime listesini tekrar yazıp şu açık onayı istemek
   ZORUNDADIR: `Evet, bu önizleme ve filtrelerle içe aktar.` Kullanıcı açıkça evet demedikçe
   içe aktarım ve yazma işlemi başlatılamaz. Belirsiz yanıt onay sayılmaz.

Bu onay talimatı agent içindir. Betiğe otomatik onay, etkileşimli onay veya onayı atlayan bir
bayrak ekleme.

## Adımlar

1. Onay kapısını tamamla ve kullanıcıdan aldığın dışa aktarım yolunu kullan. Tahmin etme.
2. Dosya türünü doğrula.
   - ChatGPT: dışa aktarım zip'i içindeki `conversations.json`. Zip açılmadıysa kullanıcıdan
     açmasını iste veya açık izinle yerelde `unzip` kullan.
   - Claude: dışa aktarım içindeki `conversations.json`.
   - Gemini: Google Takeout içindeki `My Activity/Gemini Apps/MyActivity.json`.
3. Dosya boyutunu `wc -c < <yol>` ile ölç. 50 MB üstündeyse betik son 12 takvim ayını
   otomatik sınır olarak uygular. Bunu önizlemede söyle. Kullanıcının daha dar tarih sınırı varsa
   daha dar olan sınır geçerli olur.
4. Aşağıdaki uygun betiği `.claude/scripts/.state/import-chatgpt.py` gibi bir geçici yola yaz.
   Önce `--preview` ile çalıştır. İkinci açık onaydan sonra aynı filtrelerle `--preview` olmadan
   çalıştır.
5. Her anahtar kelime için ayrı `--exclude-keyword` kullan. Tarihler dahil olan günlerdir.

```text
python3 .claude/scripts/.state/import-chatgpt.py conversations.json \
  --preview --start 2025-01-01 --end 2025-12-31 \
  --exclude-keyword "sağlık" --exclude-keyword "özel proje"
```

6. Betiğin özet çıktısını kullanıcıya aynen aktar. Mevcut bir hedef dosya varsa betik üzerine
   yazmayı reddeder. Bu durumda kullanıcıdan açıkça yeni bir çıktı klasörü seçmesini veya mevcut
   dosyaları kendisinin ele almasını iste. Dosyayı silme, birleştirme ya da üzerine yazma.
7. Derleme planını aşağıdaki `Sonra ne olur` bölümüne göre anlat.

## ChatGPT betiği

ChatGPT dışa aktarımında her sohbet bir `mapping` ağacıdır. Dallanma varsa bütün erişilebilir
düğümler kaynak sırasına göre bir kez yazılır. Sistem ve araç mesajları günlük dosyasına yazılmaz.
Mesaj ve konuşma metinleri kısaltılmaz. Aylık sınır dolduğunda konuşmalar atılmak yerine yeni bir
parça dosyasına geçilir. Tek bir konuşma sınırdan büyükse içerik kaybolmasın diye o parça sınırı
aşabilir.

```python
#!/usr/bin/env python3
"""ChatGPT conversations.json arşivini yerel günlük parça dosyalarına dönüştür."""

import argparse
import datetime as dt
import json
import os
import sys

MAX_EXPORT_BYTES = 50 * 1024 * 1024
RECENT_MONTHS = 12
MAX_FILE_CHARS = 200000


def iso_date(value):
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("tarih YYYY-MM-DD olmalı: %s" % value) from exc


def arguments():
    parser = argparse.ArgumentParser(description="ChatGPT geçmişini yerelde içe aktar")
    parser.add_argument("source", help="conversations.json dosyası veya onu içeren klasör")
    parser.add_argument("--preview", action="store_true", help="yalnızca önizle, dosya yazma")
    parser.add_argument("--start", type=iso_date, help="dahil başlangıç tarihi, YYYY-MM-DD")
    parser.add_argument("--end", type=iso_date, help="dahil bitiş tarihi, YYYY-MM-DD")
    parser.add_argument(
        "--exclude-keyword",
        action="append",
        default=[],
        help="eşleşen sohbeti atla, birden çok kez kullanılabilir",
    )
    parser.add_argument("--out-dir", default="daily", help="çıktı klasörü, varsayılan daily")
    args = parser.parse_args()
    if args.start and args.end and args.start > args.end:
        parser.error("başlangıç tarihi bitiş tarihinden sonra olamaz")
    return args


def load_export(path):
    if os.path.isdir(path):
        path = os.path.join(path, "conversations.json")
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = data.get("conversations", [])
    if not isinstance(data, list):
        raise ValueError("dışa aktarım bir sohbet listesi değil")
    return path, data


def content_parts(message):
    if not isinstance(message, dict):
        return []
    content = message.get("content") or {}
    if content.get("content_type") not in ("text", "multimodal_text", None):
        return []
    texts = []
    for part in content.get("parts") or []:
        if isinstance(part, str) and part.strip():
            texts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            if part["text"].strip():
                texts.append(part["text"])
    return texts


def message_text(message):
    if not isinstance(message, dict):
        return None, None
    role = (message.get("author") or {}).get("role")
    if role not in ("user", "assistant"):
        return None, None
    metadata = message.get("metadata") or {}
    if metadata.get("is_visually_hidden_from_conversation"):
        return None, None
    parts = content_parts(message)
    if not parts:
        return None, None
    return role, "\n".join(parts)


def walk_nodes(mapping):
    roots = [
        node_id
        for node_id, node in mapping.items()
        if isinstance(node, dict) and not node.get("parent")
    ]
    seeds = roots + [node_id for node_id in mapping if node_id not in roots]
    seen = set()
    for seed in seeds:
        stack = [seed]
        while stack:
            node_id = stack.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                continue
            yield node
            children = node.get("children") or []
            for child in reversed(children):
                stack.append(child)


def conversation_turns(mapping):
    turns = []
    for node in walk_nodes(mapping):
        role, text = message_text(node.get("message"))
        if role:
            turns.append((role, text))
    return turns


def conversation_time(conversation, mapping):
    stamp = conversation.get("create_time") or conversation.get("update_time")
    if isinstance(stamp, (int, float)) and stamp > 0:
        return float(stamp)
    for node in walk_nodes(mapping):
        message = node.get("message") or {}
        stamp = message.get("create_time")
        if isinstance(stamp, (int, float)) and stamp > 0:
            return float(stamp)
    return None


def searchable_text(conversation, mapping):
    values = [str(conversation.get("title") or "")]
    for node in walk_nodes(mapping):
        values.extend(content_parts(node.get("message")))
    return "\n".join(values).casefold()


def recent_cutoff(today):
    month_number = today.year * 12 + today.month - 1 - (RECENT_MONTHS - 1)
    year, month_index = divmod(month_number, 12)
    return dt.date(year, month_index + 1, 1)


def select_conversations(conversations, size, start, end, keywords):
    stats = {
        "source": len(conversations),
        "invalid": 0,
        "date": 0,
        "keyword": 0,
        "empty": 0,
    }
    automatic_start = None
    if size > MAX_EXPORT_BYTES:
        automatic_start = recent_cutoff(dt.datetime.now(dt.timezone.utc).date())
    effective_start = start
    if automatic_start and (effective_start is None or automatic_start > effective_start):
        effective_start = automatic_start
    folded_keywords = [word.casefold() for word in keywords if word.strip()]
    selected = []
    for conversation in conversations:
        if not isinstance(conversation, dict):
            stats["invalid"] += 1
            continue
        mapping = conversation.get("mapping") or {}
        if not isinstance(mapping, dict):
            stats["invalid"] += 1
            continue
        stamp = conversation_time(conversation, mapping)
        if stamp is None:
            stats["invalid"] += 1
            continue
        moment = dt.datetime.fromtimestamp(stamp, dt.timezone.utc)
        day = moment.date()
        if (effective_start and day < effective_start) or (end and day > end):
            stats["date"] += 1
            continue
        haystack = searchable_text(conversation, mapping)
        if any(word in haystack for word in folded_keywords):
            stats["keyword"] += 1
            continue
        turns = conversation_turns(mapping)
        if not turns:
            stats["empty"] += 1
            continue
        selected.append(
            {
                "title": str(conversation.get("title") or "başlık yok"),
                "moment": moment,
                "turns": turns,
            }
        )
    selected.sort(key=lambda item: item["moment"])
    return selected, stats, automatic_start, effective_start


def conversation_block(record):
    moment = record["moment"]
    title = record["title"].replace("\r", " ").replace("\n", " ").strip()
    heading = "### Oturum (%s UTC) ChatGPT: %s\n\n" % (
        moment.strftime("%Y-%m-%d %H:%M"),
        title or "başlık yok",
    )
    lines = []
    for role, text in record["turns"]:
        label = "**User:**" if role == "user" else "**Assistant:**"
        lines.append("%s %s\n" % (label, text))
    return heading + "\n".join(lines)


def part_header(month, part_number):
    return (
        "# Günlük Log: %s (içe aktarım, parça %03d)\n\n"
        "Kaynak: ChatGPT dışa aktarımı. Bu dosya makine tarafından yazıldı.\n\n"
        "## Oturumlar\n\n" % (month, part_number)
    )


def build_parts(selected, out_dir):
    months = {}
    for record in selected:
        month = record["moment"].strftime("%Y-%m")
        months.setdefault(month, []).append(conversation_block(record))
    plans = []
    for month in sorted(months):
        part_number = 1
        header = part_header(month, part_number)
        blocks = []
        total = len(header)
        for block in months[month]:
            separator = "" if not blocks else "\n"
            if blocks and total + len(separator) + len(block) > MAX_FILE_CHARS:
                text = header + "\n".join(blocks)
                name = "import-%s-part-%03d.md" % (month, part_number)
                plans.append((os.path.join(out_dir, name), text, len(blocks)))
                part_number += 1
                header = part_header(month, part_number)
                blocks = []
                total = len(header)
                separator = ""
            blocks.append(block)
            total += len(separator) + len(block)
        if blocks:
            text = header + "\n".join(blocks)
            name = "import-%s-part-%03d.md" % (month, part_number)
            plans.append((os.path.join(out_dir, name), text, len(blocks)))
    return plans


def write_exclusive(plans):
    claimed = []
    try:
        for path, text, count in plans:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            claimed.append([path, descriptor, text, count])
    except OSError:
        for path, descriptor, _text, _count in claimed:
            os.close(descriptor)
            os.unlink(path)
        raise
    try:
        for item in claimed:
            with os.fdopen(item[1], "w", encoding="utf-8", newline="\n") as handle:
                handle.write(item[2])
            item[1] = None
    except OSError:
        for path, descriptor, _text, _count in claimed:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        raise


def print_selection(selected, stats, automatic_start, effective_start, preview):
    skipped = stats["invalid"] + stats["date"] + stats["keyword"] + stats["empty"]
    print("ÖNİZLEME" if preview else "İÇE AKTARIM")
    print("kaynak sohbet: %d" % stats["source"])
    print("yazılacak sohbet: %d" % len(selected))
    if selected:
        print(
            "tarih aralığı: %s .. %s"
            % (selected[0]["moment"].date(), selected[-1]["moment"].date())
        )
    else:
        print("tarih aralığı: yok")
    print(
        "atlanan sohbet: %d (tarih: %d, anahtar kelime: %d, boş: %d, geçersiz: %d)"
        % (skipped, stats["date"], stats["keyword"], stats["empty"], stats["invalid"])
    )
    if automatic_start:
        print("50 MB sınırı: son 12 takvim ayı, otomatik başlangıç %s" % automatic_start)
    if effective_start:
        print("etkin başlangıç tarihi: %s" % effective_start)


def main():
    args = arguments()
    try:
        path, conversations = load_export(args.source)
        size = os.path.getsize(path)
        selected, stats, automatic_start, effective_start = select_conversations(
            conversations, size, args.start, args.end, args.exclude_keyword
        )
        print_selection(selected, stats, automatic_start, effective_start, args.preview)
        if args.preview:
            print("dosya yazılmadı")
            return 0
        plans = build_parts(selected, args.out_dir)
        if not plans:
            print("yazılan dosya: 0")
            return 0
        os.makedirs(args.out_dir, exist_ok=True)
        try:
            write_exclusive(plans)
        except FileExistsError as exc:
            print(
                "HATA: mevcut içe aktarım dosyasının üzerine yazılmadı: %s" % exc.filename,
                file=sys.stderr,
            )
            return 3
        print("yazılan dosya: %d" % len(plans))
        print("yazılan sohbet: %d, atlanan sohbet: %d" % (
            sum(count for _path, _text, count in plans),
            stats["invalid"] + stats["date"] + stats["keyword"] + stats["empty"],
        ))
        for output, text, count in plans:
            print("  %s  %d sohbet  %d karakter" % (output, count, len(text)))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("HATA: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

## Claude betiği

Claude dışa aktarımının şeması düzdür. Aynı onay kapısını, `--preview`, tarih ve anahtar kelime
filtrelerini, kayıpsız parça üretimini ve `write_exclusive` yazımını kullan. ChatGPT'e özgü
`conversation_turns` yerine aşağıdaki çözümleyiciyi çağır. Sistem kayıtlarını yazma.

```python
def claude_turns(conversation):
    turns = []
    for message in conversation.get("chat_messages") or []:
        role = {"human": "user", "assistant": "assistant"}.get(message.get("sender"))
        if not role:
            continue
        texts = []
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
        fallback = message.get("text")
        if not texts and isinstance(fallback, str) and fallback.strip():
            texts.append(fallback)
        if texts:
            turns.append((role, "\n".join(texts)))
    return turns
```

Zaman damgasını yalnızca stdlib ile şöyle çöz:

```python
created = str(conversation.get("created_at") or "")
moment = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
if moment.tzinfo is None:
    moment = moment.replace(tzinfo=dt.timezone.utc)
stamp = moment.timestamp()
```

## Gemini Takeout

Takeout arşivinde `My Activity/Gemini Apps/` klasörü vardır. JSON seçildiyse
`MyActivity.json` bir kayıt listesidir. Bu format sohbet bütünlüğünü korumaz, her kayıt tek bir
istemdir. Gemini içe aktarımını istem günlüğü gibi yaz: aynı günün kayıtlarını tek bir
`### Oturum (YYYY-MM-DD) Gemini` bloğunda topla ve her kaydı `**User:**` satırı yap. Aynı onay,
önizleme, filtre, parça ve çakışma kuralları geçerlidir. Takeout HTML ise kullanıcıdan JSON
formatında yeniden dışa aktarmasını iste.

## Sonra ne olur

İçe aktarım bittiğinde kullanıcıya dürüst özeti ver:

- Yazılan ve atlanan sohbet sayılarını ayrı göster. Her parça dosyasının sohbet ve karakter
  sayısını betik çıktısından aktar.
- `daily/import-*.md` dosyaları yereldir. Akşam derleyicisi içeriklerini özetleme için
  kullanıcının kendi Claude aboneliği üzerinden Claude'a gönderir. Başka yere gönderim yapılmaz.
- Derleyici akşamları bir tur çalışır ve her turda değişen logları işler. Büyük bir arşiv birkaç
  akşama yayılabilir.
- Kullanıcı beklemek istemezse önce `python3 .claude/scripts/compile.py --dry-run`, sonra açık
  onayıyla `python3 .claude/scripts/compile.py` çalıştırılabilir. Her tur abonelik limitinden pay
  tüketir.
- Kullanıcı içeriği Claude'a göndermek istemezse derleyiciyi çalıştırmamalı ve ilgili aylık parça
  dosyalarını akşam derlemesinden önce silmelidir.
- 50 MB sınırı nedeniyle daha eski arşiv atlandıysa bunu açıkça söyle ve ayrı bir tarih aralığıyla
  ikinci tur isteyip istemediğini sor.

Son adım olarak `beyin-doktor` çalıştırmayı öner. Derleme durumu satırından ilerleme izlenebilir.
