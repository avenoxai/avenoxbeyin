#!/usr/bin/env bash
# avenoxbeyin v1/v2.0/v2.1/v2.2 -> v2.3 upgrade. Single process, transactional, fail loud.
#
# Why this file exists: the upgrade used to live as a chain of fenced Bash blocks in SETUP.md that
# assigned shell variables in one block and used them in the next. Every Claude Bash call is a
# separate process, so those variables were empty later and the paths collapsed to "/daily",
# "/.beyin-version" and friends. Everything now runs in ONE process with an absolute --vault.
#
# Usage:
#   bash scripts/upgrade.sh --vault /abs/path/to/vault --stage check
#   bash scripts/upgrade.sh --vault /abs/path/to/vault --stage apply     [--confirm-rename] [--confirm-local-hooks]
#   bash scripts/upgrade.sh --vault /abs/path/to/vault --stage finalize
#
# Stages:
#   check     read only. Prints the plan and the confirmations the user must give. Mutates nothing.
#   apply     snapshot, rename, copy, merge, migrate. Does NOT write .beyin-version.
#   finalize  re-runs every gate, commits with an explicit allow-list, then writes .beyin-version
#             atomically as the very last filesystem write.
#
# Exit codes: 0 ok | 1 hard failure | 2 usage | 3 nothing to do | 10 needs --confirm-rename
#             11 needs --confirm-local-hooks
set -euo pipefail

BEYIN_TARGET_VERSION="2.3.0"
BEYIN_SCRIPT_VERSION="2.3.0"
BEYIN_MEMORY_DIR_NAME="🔮 850-Companion"
BEYIN_HOOK_FILES="lib.sh session-start.sh prompt-counter.sh session-end.sh pre-compact.sh"
BEYIN_SCRIPT_FILES="flush.py compile.py _portalock.py render_codex_hooks.py render_antigravity_hooks.py antigravity_hooks.py graf_kontrol.py"
BEYIN_SKILL_DIRS="beyin-doktor gecmis-import"
BEYIN_BACKUP_ROOT="${BEYIN_BACKUP_ROOT:-$HOME/.avenoxbeyin-yedek}"

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'HATA: %s\n' "$*" >&2; exit 1; }

canon_dir() { CDPATH= cd -- "$1" 2>/dev/null && pwd -P; }

# ---------------------------------------------------------------- arguments
VAULT=""
STAGE=""
CONFIRM_RENAME=0
CONFIRM_LOCAL_HOOKS=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --vault)                VAULT="${2:-}"; shift 2 ;;
    --vault=*)              VAULT="${1#--vault=}"; shift ;;
    --stage)                STAGE="${2:-}"; shift 2 ;;
    --stage=*)              STAGE="${1#--stage=}"; shift ;;
    --confirm-rename)       CONFIRM_RENAME=1; shift ;;
    --confirm-local-hooks)  CONFIRM_LOCAL_HOOKS=1; shift ;;
    --version)              say "$BEYIN_SCRIPT_VERSION"; exit 0 ;;
    -h|--help)              sed -n '2,25p' "$0"; exit 0 ;;
    *)                      printf 'Bilinmeyen argüman: %s\n' "$1" >&2; exit 2 ;;
  esac
done

case "$STAGE" in
  check|apply|finalize) ;;
  "") printf '%s\n' "Eksik argüman: --stage check|apply|finalize" >&2; exit 2 ;;
  *)  printf 'Geçersiz --stage: %s\n' "$STAGE" >&2; exit 2 ;;
esac

[ -n "$VAULT" ] || { printf '%s\n' "Eksik argüman: --vault (mutlak yol)" >&2; exit 2; }
case "$VAULT" in
  /*) ;;
  *)  printf '%s\n' "--vault mutlak bir yol olmalı, göreli yol kabul edilmiyor: $VAULT" >&2; exit 2 ;;
esac

# ---------------------------------------------------------------- paths
SELF_DIR=$(canon_dir "$(dirname -- "$0")") || die "kendi konumum çözümlenemedi"
REPO=$(canon_dir "$SELF_DIR/..") || die "repo kökü çözümlenemedi"
[ -d "$REPO/template/.claude/hooks" ] || die "repo kökü yanlış görünüyor, template/.claude/hooks yok: $REPO"

[ -d "$VAULT" ] || die "vault klasörü yok: $VAULT"
V=$(canon_dir "$VAULT") || die "vault yolu çözümlenemedi: $VAULT"

[ -n "$V" ]      || die "vault yolu boş çözümlendi"
[ "$V" != "/" ]  || die "vault olarak / kabul edilmiyor"
[ "$V" != "$HOME" ] || die "vault olarak ev dizini kabul edilmiyor"
[ "$V" != "$REPO" ] || die "vault repo'nun kendisi olamaz: $V"
case "$V/" in "$REPO/"*) die "vault repo'nun içinde olamaz: $V" ;; esac
case "$REPO/" in "$V/"*) die "repo vault'un içinde olamaz: $V" ;; esac

# ---------------------------------------------------------------- v1 markers
[ -f "$V/CLAUDE.md" ] || die "v1 işareti yok: $V/CLAUDE.md bulunamadı. Bu bir beyin vault'u değil."
MEM_DIR=$(find "$V" -mindepth 1 -maxdepth 1 -type d -name "🔮 850-*" -print 2>/dev/null | head -1)
[ -n "$MEM_DIR" ] || die "v1 işareti yok: '🔮 850-*' hafıza klasörü bulunamadı. Bu bir beyin vault'u değil."
MEM_NAME=$(basename "$MEM_DIR")

command -v python3 >/dev/null 2>&1 || die "python3 bulunamadı. v2 makine katmanı python3 olmadan kurulamaz (macOS: xcode-select --install)."

CUR_VERSION=""
[ -f "$V/.beyin-version" ] && CUR_VERSION=$(sed -n '1p' "$V/.beyin-version" 2>/dev/null || printf '')

STATE_DIR="$V/.claude/scripts/.state"
MANIFEST="$STATE_DIR/upgrade-manifest.txt"
STAGE_MARK="$STATE_DIR/upgrade-stage"

# ---------------------------------------------------------------- helpers
git_id() {
  # Always pass an identity so an unset global git config cannot turn a real failure into
  # "nothing to commit". Uses the user's own identity when it exists.
  GIT_N=$(git -C "$V" config user.name  2>/dev/null || printf '')
  GIT_E=$(git -C "$V" config user.email 2>/dev/null || printf '')
  [ -n "$GIT_N" ] || GIT_N="avenoxbeyin"
  [ -n "$GIT_E" ] || GIT_E="beyin@localhost"
}

assert_no_secret_staged() {
  # Only ADDING or MODIFYING a secret path is a leak. A staged DELETION of one is the cure
  # (untrack_ignored_secrets stages exactly that), so status D must not trip the alarm,
  # otherwise the cure itself aborts the upgrade.
  BAD=$(git -C "$V" diff --cached --name-status 2>/dev/null \
        | awk '$1 !~ /^D/ { $1=""; sub(/^[ \t]+/, ""); print }' \
        | grep -E '(^|/)\.env$|settings\.local\.json|\.yedek|\.bak$|\.orig$|\.pem$|\.key$' || printf '')
  if [ -n "$BAD" ]; then
    git -C "$V" reset -q >/dev/null 2>&1 || :
    say "Sahnelenmesi yasak dosyalar bulundu ve sahne temizlendi:"
    printf '%s\n' "$BAD"
    die "sır taşıyabilecek dosya commit'e girmek üzereydi, yükseltme durduruldu"
  fi
}

untrack_ignored_pycache() {
  # .gitignore now carries __pycache__/ and *.pyc, but an ignore rule never untracks a path that
  # is already in the index. A vault upgraded before those rules existed keeps its compiled
  # bytecode tracked: the finalize gate imports the scripts, python rewrites the .pyc files, and
  # "git add" stages them into the upgrade commit on every single run. Untrack them here, before
  # the snapshot, and leave every file on disk.
  #
  # Deliberately separate from untrack_ignored_secrets: bytecode is not a secret, so this must not
  # print the "revoke your API key" warning that function ends with.
  command -v git >/dev/null 2>&1 || return 0
  [ -d "$V/.git" ] || return 0
  TRACKED_PYC=$(git -C "$V" ls-files -- '*.pyc' '*/__pycache__/*' 2>/dev/null | sort -u || printf '')
  [ -n "$TRACKED_PYC" ] || return 0
  PYC_COUNT=$(printf '%s\n' "$TRACKED_PYC" | grep -c . || printf '0')
  printf '%s\n' "$TRACKED_PYC" | while IFS= read -r PYC; do
    [ -n "$PYC" ] || continue
    git -C "$V" rm --cached -q -- "$PYC" >/dev/null 2>&1 \
      || die "derlenmiş Python dosyası izlemeden çıkarılamadı: $PYC"
  done
  say "izlemeden çıkarıldı (derlenmiş Python, dosyalar diskte duruyor): $PYC_COUNT adet"
}

record() { printf '%s\n' "$1" >> "$MANIFEST"; }

copy_file() {
  # copy_file <src-abs> <dst-abs> <manifest-relpath>
  cp "$1" "$2" || die "kopyalanamadı: $1 -> $2"
  [ -s "$2" ] || die "kopya boş çıktı: $2"
  record "$3"
}

ensure_gitignore() {
  GI="$V/.gitignore"
  [ -f "$GI" ] || : > "$GI"
  # Guarantee the file ends with a newline before appending.
  if [ -s "$GI" ] && [ "$(tail -c 1 "$GI" | wc -l | tr -d ' ')" = "0" ]; then
    printf '\n' >> "$GI"
  fi
  GI_ADDED=0
  while IFS= read -r LINE; do
    [ -n "$LINE" ] || continue
    if ! grep -qxF "$LINE" "$GI" 2>/dev/null; then
      printf '%s\n' "$LINE" >> "$GI"
      GI_ADDED=$((GI_ADDED + 1))
    fi
  done <<'IGN'
.claude/settings.local.json
.env
*.yedek
*.yedek-*
*.bak
*.orig
.claude/hooks/.state/
.claude/scripts/.state/*
!.claude/scripts/.state/.gitkeep
.DS_Store
.obsidian/workspace*
.obsidian/cache
__pycache__/
*.pyc
IGN
  say "gitignore: $GI_ADDED satır eklendi"
}

untrack_ignored_secrets() {
  # A v1 vault created on a machine without a global ignore rule has
  # .claude/settings.local.json COMMITTED. Adding the path to .gitignore does not untrack an
  # already-tracked file, so the later "git add -u" restages it, assert_no_secret_staged kills
  # the run, and the upgrade dead-ends with every gate green. Untrack it here, before the
  # snapshot, so the ignore rule can actually take effect.
  command -v git >/dev/null 2>&1 || return 0
  [ -d "$V/.git" ] || return 0
  UNTRACKED_ANY=0
  for SECRET in ".claude/settings.local.json" ".env"; do
    if git -C "$V" ls-files --error-unmatch -- "$SECRET" >/dev/null 2>&1; then
      git -C "$V" rm --cached -q -- "$SECRET" || die "izlemeden çıkarılamadı: $SECRET"
      say "izlemeden çıkarıldı (dosya diskte duruyor): $SECRET"
      UNTRACKED_ANY=1
    fi
  done
  # Any backup artefact that a previous half-run left tracked.
  TRACKED_BAK=$(git -C "$V" ls-files -- '*.yedek' '*.yedek-*' '*.bak' '*.orig' 2>/dev/null || printf '')
  if [ -n "$TRACKED_BAK" ]; then
    printf '%s\n' "$TRACKED_BAK" | while IFS= read -r B; do
      [ -n "$B" ] || continue
      git -C "$V" rm --cached -q -- "$B" >/dev/null 2>&1 || :
      say "izlemeden çıkarıldı (yedek artığı): $B"
    done
    UNTRACKED_ANY=1
  fi
  if [ "$UNTRACKED_ANY" = "1" ]; then
    say ""
    say "!! DİKKAT: bu dosyalar bundan sonra izlenmiyor, ama GEÇMİŞTE duruyorlar."
    say "!! İçlerinde API anahtarı varsa anahtarı sağlayıcıdan İPTAL ET ve yenile."
    say "!! Geçmişi temizlemek istersen: git filter-repo ya da BFG, yükseltmeden ayrı bir iş."
    say ""
  fi
}

local_hooks_report() {
  # Prints: "<count-of-v1-beyin-entries> <count-of-unrelated-entries>"
  python3 - "$V" <<'PY'
import json, os, sys
p = os.path.join(sys.argv[1], ".claude", "settings.local.json")
V1 = ("session-start.sh", "prompt-counter.sh", "session-end.sh", "pre-compact.sh")
EV = ("SessionStart", "UserPromptSubmit", "SessionEnd", "PreCompact")
try:
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
except FileNotFoundError:
    print("0 0")
    sys.exit(0)
except ValueError:
    # Present but not parseable. Not the same as absent: the user has a file we cannot
    # reason about, so the caller must stop rather than silently assume "no hooks".
    print("BOZUK gecersiz-json")
    sys.exit(0)
if not isinstance(d, dict):
    print("BOZUK json-nesnesi-degil")
    sys.exit(0)
mine = other = 0
for ev, matchers in (d.get("hooks") or {}).items():
    for m in (matchers or []):
        for h in (m.get("hooks") or []):
            c = h.get("command", "") or ""
            if ev in EV and any(b in c for b in V1):
                mine += 1
            else:
                other += 1
print(mine, other)
PY
}

# ---------------------------------------------------------------- preconditions shared by stages
if [ "$CUR_VERSION" = "$BEYIN_TARGET_VERSION" ] && [ "$STAGE" != "finalize" ]; then
  say "Bu vault zaten v$BEYIN_TARGET_VERSION damgalı: $V"
  say "Yapılacak bir şey yok. Eksik varsa 'beyin doktor' çalıştır."
  exit 3
fi
case "$CUR_VERSION" in
  ""|2.0.0|2.1.0|2.2.0|"$BEYIN_TARGET_VERSION") ;;
  *) die "uyumsuz sürüm damgası: '$CUR_VERSION' (desteklenen: boş v1, 2.0.0, 2.1.0, 2.2.0 veya $BEYIN_TARGET_VERSION)" ;;
esac

NEED_RENAME=0
[ "$MEM_NAME" = "$BEYIN_MEMORY_DIR_NAME" ] || NEED_RENAME=1

LOCAL_REPORT=$(local_hooks_report 2>/dev/null || printf '')
set -- $LOCAL_REPORT
LOCAL_MINE="${1:-}"
LOCAL_OTHER="${2:-}"
case "$LOCAL_MINE" in
  BOZUK)
    say ".claude/settings.local.json okunamıyor: ${LOCAL_OTHER:-bilinmeyen sebep}"
    say "Bu dosya bir JSON nesnesi olmalı, örneğin: {\"hooks\": { ... }}"
    say "Elle düzelt ya da geçici olarak vault dışına taşı, sonra yükseltmeyi tekrar çalıştır."
    die "yükseltme durduruldu, hiçbir şey değiştirilmedi"
    ;;
esac
case "$LOCAL_MINE" in
  ''|*[!0-9]*) die "settings.local.json taraması beklenen sayıyı vermedi: '${LOCAL_REPORT}'" ;;
esac
case "$LOCAL_OTHER" in
  ''|*[!0-9]*) die "settings.local.json taraması beklenen sayıyı vermedi: '${LOCAL_REPORT}'" ;;
esac

# ---------------------------------------------------------------- STAGE: check
if [ "$STAGE" = "check" ]; then
  step "PLAN (hiçbir şey değiştirilmedi)"
  say "vault           : $V"
  say "repo            : $REPO"
  say "hafıza klasörü  : $MEM_NAME"
  say "mevcut sürüm    : ${CUR_VERSION:-v1 (.beyin-version yok)}"
  say "git deposu      : $([ -d "$V/.git" ] && printf 'var' || printf 'yok, kurulacak')"
  say "settings.local  : $LOCAL_MINE adet v1 beyin kancası, $LOCAL_OTHER adet ilgisiz kanca girdisi"
  step "ONAY GEREKLİ"
  NEEDED=0
  if [ "$NEED_RENAME" = "1" ]; then
    NEEDED=1
    say "1) Hafıza klasörünün adı '$MEM_NAME'. v2 kancaları ve scriptleri sabit"
    say "   '$BEYIN_MEMORY_DIR_NAME' yolunu okur, bu yüzden yeniden adlandırma v2 için ZORUNLU."
    say "   İçerik hiç değişmez, sadece klasör adı değişir; ortağın ismi zaten dosyaların içinde."
    say "   Kullanıcı evet derse: --confirm-rename"
    say "   Kullanıcı hayır derse: yükseltme yapılmaz, sürüm damgası da yazılmaz."
  fi
  if [ "$LOCAL_MINE" != "0" ]; then
    NEEDED=1
    say "2) settings.local.json içinde $LOCAL_MINE adet v1 beyin kancası var. Temizlenmezse her olay"
    say "   iki kez tetiklenir. Sadece bu girdiler silinir; $LOCAL_OTHER ilgisiz kanca ve env, izin"
    say "   gibi bütün diğer anahtarlar korunur. Yedek repo ve vault DIŞINA 0600 ile yazılır."
    say "   Kullanıcı evet derse: --confirm-local-hooks"
    say "   Kullanıcı hayır derse: yükseltme yapılmaz, sürüm damgası da yazılmaz."
  fi
  [ "$NEEDED" = "1" ] || say "(ek onay gerekmiyor)"
  step "SONRAKİ ADIM"
  say "bash \"$REPO/scripts/upgrade.sh\" --vault \"$V\" --stage apply$([ "$NEED_RENAME" = "1" ] && printf ' --confirm-rename')$([ "$LOCAL_MINE" != "0" ] && printf ' --confirm-local-hooks')"
  exit 0
fi

# ---------------------------------------------------------------- STAGE: apply
if [ "$STAGE" = "apply" ]; then
  if [ "$NEED_RENAME" = "1" ] && [ "$CONFIRM_RENAME" != "1" ]; then
    say "ONAY GEREKLİ: hafıza klasörü '$MEM_NAME' -> '$BEYIN_MEMORY_DIR_NAME' olarak yeniden adlandırılmalı."
    say "Bu v2 için zorunlu. Kullanıcıya sor, evet derse --confirm-rename ile tekrar çalıştır."
    say "Hayır derse yükseltmeyi hiç başlatma: sürüm damgası yazılmayacak, vault v1 olarak kalacak."
    exit 10
  fi
  if [ "$LOCAL_MINE" != "0" ] && [ "$CONFIRM_LOCAL_HOOKS" != "1" ]; then
    say "ONAY GEREKLİ: settings.local.json içindeki $LOCAL_MINE adet v1 beyin kancası temizlenmeli."
    say "Temizlenmezse kancalar her olayda iki kez çalışır. Kullanıcıya sor, evet derse"
    say "--confirm-local-hooks ile tekrar çalıştır. Hayır derse yükseltme yapılmaz."
    exit 11
  fi

  mkdir -p "$STATE_DIR"
  : > "$MANIFEST"

  step "1/9 .gitignore (anlık görüntüden ÖNCE, sır sahnelenmesin diye)"
  ensure_gitignore
  untrack_ignored_secrets
  untrack_ignored_pycache

  step "2/9 anlık görüntü (doğrulanmış)"
  SNAP_OK=0
  if command -v git >/dev/null 2>&1; then
    if [ ! -d "$V/.git" ]; then
      git -C "$V" init -q || die "git init başarısız: $V"
      say "git deposu kuruldu"
    fi
    git_id
    git -C "$V" add -A || die "git add başarısız"
    assert_no_secret_staged
    HEAD_BEFORE=$(git -C "$V" rev-parse HEAD 2>/dev/null || printf 'NONE')
    STAGED=$(git -C "$V" diff --cached --name-only | wc -l | tr -d ' ')
    if [ "$STAGED" = "0" ]; then
      [ "$HEAD_BEFORE" != "NONE" ] || die "commit edilecek değişiklik yok ve HEAD de yok: anlık görüntü alınamadı"
      say "çalışma ağacı zaten temiz, mevcut HEAD anlık görüntü sayılıyor: $HEAD_BEFORE"
      SNAP_OK=1
    else
      git -C "$V" -c user.name="$GIT_N" -c user.email="$GIT_E" \
        commit -q -m "v2 yükseltmesi öncesi anlık görüntü" \
        || die "anlık görüntü commit'i başarısız oldu, $STAGED dosya sahnede kaldı. Yükseltme durduruldu."
      HEAD_AFTER=$(git -C "$V" rev-parse HEAD 2>/dev/null || printf 'NONE')
      [ "$HEAD_AFTER" != "NONE" ] || die "commit sonrası HEAD okunamadı"
      [ "$HEAD_AFTER" != "$HEAD_BEFORE" ] || die "commit sonrası HEAD değişmedi, anlık görüntü gerçekten alınmadı"
      say "anlık görüntü alındı: $HEAD_AFTER ($STAGED dosya)"
      SNAP_OK=1
    fi
  fi
  if [ "$SNAP_OK" != "1" ]; then
    say "git yok, vault DIŞINA doğrulanmış kopya alınıyor"
    mkdir -p "$BEYIN_BACKUP_ROOT"; chmod 700 "$BEYIN_BACKUP_ROOT" 2>/dev/null || :
    COPY_DST="$BEYIN_BACKUP_ROOT/$(basename "$V")-v1-$(date +%Y%m%d-%H%M%S)"
    cp -R "$V" "$COPY_DST" || die "yedek kopya alınamadı: $COPY_DST"
    SRC_N=$(find "$V" -mindepth 1 | wc -l | tr -d ' ')
    DST_N=$(find "$COPY_DST" -mindepth 1 | wc -l | tr -d ' ')
    [ "$SRC_N" = "$DST_N" ] || die "yedek doğrulaması başarısız: kaynak $SRC_N, kopya $DST_N"
    say "doğrulanmış yedek: $COPY_DST ($DST_N öğe)"
  fi

  step "3/9 hafıza klasörü adı"
  if [ "$NEED_RENAME" = "1" ]; then
    [ ! -e "$V/$BEYIN_MEMORY_DIR_NAME" ] || die "hedef klasör zaten var: $BEYIN_MEMORY_DIR_NAME. Elle çözülmeli."
    BEFORE_N=$(find "$MEM_DIR" -mindepth 1 | wc -l | tr -d ' ')
    if [ -d "$V/.git" ] && command -v git >/dev/null 2>&1; then
      git -C "$V" mv "$MEM_NAME" "$BEYIN_MEMORY_DIR_NAME" || die "git mv başarısız: $MEM_NAME"
    else
      mv "$MEM_DIR" "$V/$BEYIN_MEMORY_DIR_NAME" || die "mv başarısız: $MEM_NAME"
    fi
    [ -d "$V/$BEYIN_MEMORY_DIR_NAME" ] || die "yeniden adlandırma sonrası hedef klasör yok"
    [ ! -d "$MEM_DIR" ] || die "yeniden adlandırma sonrası eski klasör hâlâ duruyor"
    AFTER_N=$(find "$V/$BEYIN_MEMORY_DIR_NAME" -mindepth 1 | wc -l | tr -d ' ')
    [ "$BEFORE_N" = "$AFTER_N" ] || die "içerik sayısı değişti: önce $BEFORE_N, sonra $AFTER_N"
    say "'$MEM_NAME' -> '$BEYIN_MEMORY_DIR_NAME' ($AFTER_N öğe korundu)"
    MEM_DIR="$V/$BEYIN_MEMORY_DIR_NAME"
    MEM_NAME="$BEYIN_MEMORY_DIR_NAME"
  else
    say "zaten doğru: $MEM_NAME"
  fi

  step "4/9 klasörler"
  mkdir -p "$V/daily" "$V/knowledge/concepts" "$V/knowledge/connections" \
           "$V/.claude/scripts/.state" "$V/.claude/skills" "$V/.claude/hooks"
  for K in "daily/.gitkeep" "knowledge/concepts/.gitkeep" "knowledge/connections/.gitkeep" \
           ".claude/scripts/.state/.gitkeep"; do
    [ -f "$V/$K" ] || : > "$V/$K"
  done
  say "klasörler ve .gitkeep dosyaları yerinde"

  step "5/9 scriptler ve skill'ler (kod, üzerine yazılır)"
  for F in $BEYIN_SCRIPT_FILES; do
    copy_file "$REPO/template/.claude/scripts/$F" "$V/.claude/scripts/$F" ".claude/scripts/$F"
    say "  .claude/scripts/$F"
  done
  for S in $BEYIN_SKILL_DIRS; do
    mkdir -p "$V/.claude/skills/$S"
    copy_file "$REPO/template/.claude/skills/$S/SKILL.md" "$V/.claude/skills/$S/SKILL.md" ".claude/skills/$S/SKILL.md"
    say "  .claude/skills/$S/SKILL.md"
  done

  step "6/9 tohum dosyaları (sadece yoksa)"
  SEEDS_TMP=$(mktemp)
  printf '%s\n' "knowledge/index.md" "knowledge/log.md" > "$SEEDS_TMP"
  printf '%s\n' "$BEYIN_MEMORY_DIR_NAME/Kurallar.md" >> "$SEEDS_TMP"
  while IFS= read -r S; do
    if [ -f "$V/$S" ]; then
      say "  atlandı (zaten var): $S"
    else
      copy_file "$REPO/template/$S" "$V/$S" "$S"
      say "  eklendi: $S"
    fi
  done < "$SEEDS_TMP"
  rm -f "$SEEDS_TMP"

  step "7/9 kancalar (kod, üzerine yazılır)"
  for H in $BEYIN_HOOK_FILES; do
    copy_file "$REPO/template/.claude/hooks/$H" "$V/.claude/hooks/$H" ".claude/hooks/$H"
    chmod +x "$V/.claude/hooks/$H" || die "chmod +x başarısız: $H"
    bash -n "$V/.claude/hooks/$H" || die "sözdizimi hatası: $H"
    say "  $H (çalıştırılabilir, sözdizimi ✓)"
  done

  step "7b/9 ortak store, Codex ve Antigravity kanca kayıtları"
  mkdir -p "$V/.agents" "$V/.codex"

  if [ ! -e "$V/AGENTS.md" ] && [ ! -L "$V/AGENTS.md" ]; then
    ln -s "CLAUDE.md" "$V/AGENTS.md" || die "AGENTS.md symlink oluşturulamadı"
  elif [ -L "$V/AGENTS.md" ] && [ "$(readlink "$V/AGENTS.md" 2>/dev/null || :)" = "CLAUDE.md" ]; then
    :
  elif [ -f "$V/AGENTS.md" ] && cmp -s "$V/AGENTS.md" "$V/CLAUDE.md"; then
    rm -f "$V/AGENTS.md" || die "aynı AGENTS.md kaldırılamadı"
    ln -s "CLAUDE.md" "$V/AGENTS.md" || die "AGENTS.md symlink oluşturulamadı"
  else
    die "AGENTS.md mevcut ve CLAUDE.md ile aynı değil; iki router'ı elle birleştirip yeniden çalıştır"
  fi

  if [ ! -e "$V/.agents/skills" ] && [ ! -L "$V/.agents/skills" ]; then
    ln -s "../.claude/skills" "$V/.agents/skills" || die ".agents/skills symlink oluşturulamadı"
  elif [ -L "$V/.agents/skills" ] && [ "$(readlink "$V/.agents/skills" 2>/dev/null || :)" = "../.claude/skills" ]; then
    :
  else
    die ".agents/skills zaten bağımsız bir store; içerikleri .claude/skills ile elle birleştir"
  fi

  if [ ! -e "$V/.codex/hooks" ] && [ ! -L "$V/.codex/hooks" ]; then
    ln -s "../.claude/hooks" "$V/.codex/hooks" || die ".codex/hooks symlink oluşturulamadı"
  elif [ -L "$V/.codex/hooks" ] && [ "$(readlink "$V/.codex/hooks" 2>/dev/null || :)" = "../.claude/hooks" ]; then
    :
  else
    die ".codex/hooks zaten bağımsız bir store; içerikleri .claude/hooks ile elle birleştir"
  fi

  python3 "$V/.claude/scripts/render_codex_hooks.py" --vault "$V" --platform posix \
    >/dev/null || die ".codex/hooks.json üretilemedi"
  python3 "$V/.claude/scripts/render_antigravity_hooks.py" --vault "$V" --platform posix \
    >/dev/null || die ".agents/hooks.json üretilemedi"
  record "AGENTS.md"
  record ".agents/skills"
  record ".codex/hooks"
  record ".codex/hooks.json"
  record ".agents/hooks.json"
  say "  AGENTS.md -> CLAUDE.md"
  say "  .agents/skills -> .claude/skills"
  say "  .codex/hooks -> .claude/hooks"
  say "  .codex/hooks.json (mutlak yollar, SessionEnd 3s)"
  say "  .agents/hooks.json (Antigravity PreInvocation + Stop adaptörü)"

  step "8/9 settings.json kanca kaydı (birleştir, tekrar çalıştırılabilir)"
  python3 - "$V" "$REPO" <<'PY' || die "settings.json birleştirme başarısız"
import json, os, sys, tempfile
vault, repo = sys.argv[1], sys.argv[2]
dst = os.path.join(vault, ".claude", "settings.json")
src = os.path.join(repo, "template", ".claude", "settings.json")
with open(src, encoding="utf-8") as f:
    wanted = json.load(f).get("hooks", {})
try:
    with open(dst, encoding="utf-8") as f:
        cur = json.load(f)
except FileNotFoundError:
    cur = {}
except ValueError:
    print("MEVCUT settings.json bozuk JSON, birleştirme yapılamaz", file=sys.stderr)
    raise SystemExit(1)
cur.setdefault("hooks", {})

def commands(matchers):
    out = []
    for m in matchers or []:
        for h in (m.get("hooks") or []):
            out.append(h.get("command", "") or "")
    return out

added = 0
for event, matchers in wanted.items():
    have = commands(cur["hooks"].setdefault(event, []))
    for m in matchers:
        wants = [os.path.basename(c.strip('"')) for c in commands([m])]
        if any(any(b and b in h for h in have) for b in wants):
            continue
        cur["hooks"][event].append(m)
        have = commands(cur["hooks"][event])
        added += 1

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst))
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(cur, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, dst)
print("eklenen kanca girdisi:", added)
PY
  record ".claude/settings.json"

  step "9/9 settings.local.json geçişi"
  if [ "$LOCAL_MINE" = "0" ]; then
    say "settings.local.json içinde v1 beyin kancası yok, dokunulmadı"
  else
    mkdir -p "$BEYIN_BACKUP_ROOT"; chmod 700 "$BEYIN_BACKUP_ROOT" 2>/dev/null || :
    BK="$BEYIN_BACKUP_ROOT/$(basename "$V")-settings.local.json-$(date +%Y%m%d-%H%M%S)"
    ( umask 077; cp "$V/.claude/settings.local.json" "$BK" ) || die "yedek alınamadı: $BK"
    chmod 600 "$BK" || die "yedek izinleri ayarlanamadı: $BK"
    [ -s "$BK" ] || die "yedek boş: $BK"
    say "yedek (repo ve vault DIŞINDA, 0600): $BK"
    python3 - "$V" <<'PY' || die "settings.local.json geçişi başarısız"
import json, os, sys, tempfile
p = os.path.join(sys.argv[1], ".claude", "settings.local.json")
V1 = ("session-start.sh", "prompt-counter.sh", "session-end.sh", "pre-compact.sh")
EV = ("SessionStart", "UserPromptSubmit", "SessionEnd", "PreCompact")
with open(p, encoding="utf-8") as f:
    d = json.load(f)
before_keys = sorted(d)
hooks = d.get("hooks") or {}
removed = kept = 0
new_hooks = {}
for ev, matchers in hooks.items():
    new_matchers = []
    for m in (matchers or []):
        entries = m.get("hooks") or []
        keep = []
        for h in entries:
            c = h.get("command", "") or ""
            if ev in EV and any(b in c for b in V1):
                removed += 1
            else:
                keep.append(h)
                kept += 1
        if not entries:
            new_matchers.append(m)
            continue
        if keep:
            m2 = dict(m)
            m2["hooks"] = keep
            new_matchers.append(m2)
    if new_matchers:
        new_hooks[ev] = new_matchers
if new_hooks:
    d["hooks"] = new_hooks
else:
    d.pop("hooks", None)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.chmod(tmp, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, p)
print("silinen v1 kanca girdisi:", removed, "| korunan ilgisiz kanca girdisi:", kept)
print("önceki anahtarlar:", before_keys)
print("kalan anahtarlar :", sorted(d))
PY
  fi

  printf 'apply %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" > "$STAGE_MARK"

  step "APPLY TAMAM"
  say "Sürüm damgası HENÜZ yazılmadı. Bu bilinçli."
  LEFT=$(grep -rl "{{" "$V/knowledge" "$V/.claude/skills" "$V/$BEYIN_MEMORY_DIR_NAME" 2>/dev/null || printf '')
  if [ -n "$LEFT" ]; then
    say "Şu dosyalarda hâlâ {{PLACEHOLDER}} var, doldur:"
    printf '%s\n' "$LEFT"
  else
    say "Çözülmemiş {{PLACEHOLDER}} yok."
  fi
  say ""
  say "Sıradaki: placeholder'ları doldur, 'beyin doktor' çalıştır, sonra:"
  say "bash \"$REPO/scripts/upgrade.sh\" --vault \"$V\" --stage finalize"
  exit 0
fi

# ---------------------------------------------------------------- STAGE: finalize
step "FINALIZE: bütün kapılar yeniden çalıştırılıyor"
[ -f "$STAGE_MARK" ] || die "önce --stage apply çalıştırılmalı ($STAGE_MARK yok)"

FAIL=0
gate() { if [ "$2" = "ok" ]; then say "  ✓ $1"; else say "  ✗ $1 :: $2"; FAIL=$((FAIL + 1)); fi; }

R="ok"; [ "$MEM_NAME" = "$BEYIN_MEMORY_DIR_NAME" ] || R="hafıza klasörü hâlâ '$MEM_NAME'"
gate "hafıza klasörü adı" "$R"

for H in $BEYIN_HOOK_FILES; do
  R="ok"
  if [ ! -f "$V/.claude/hooks/$H" ]; then R="dosya yok"
  elif [ ! -x "$V/.claude/hooks/$H" ]; then R="çalıştırılabilir değil"
  elif ! bash -n "$V/.claude/hooks/$H" 2>/dev/null; then R="sözdizimi hatası"
  elif ! grep -q 'BEYIN_INVOKED_BY' "$V/.claude/hooks/$H"; then R="özyineleme koruması yok"
  fi
  gate "kanca $H" "$R"
done

for F in $BEYIN_SCRIPT_FILES; do
  R="ok"
  if [ ! -s "$V/.claude/scripts/$F" ]; then R="dosya yok veya boş"
  elif ! python3 -m py_compile "$V/.claude/scripts/$F" >/dev/null 2>&1; then R="derlenmiyor"
  fi
  gate "script $F" "$R"
done

for S in $BEYIN_SKILL_DIRS; do
  R="ok"; [ -s "$V/.claude/skills/$S/SKILL.md" ] || R="SKILL.md yok"
  gate "skill $S" "$R"
done

R="ok"
[ -L "$V/AGENTS.md" ] && [ "$(readlink "$V/AGENTS.md" 2>/dev/null || :)" = "CLAUDE.md" ] || R="AGENTS.md -> CLAUDE.md symlink yok"
gate "ortak router" "$R"

R="ok"
[ -L "$V/.agents/skills" ] && [ "$(readlink "$V/.agents/skills" 2>/dev/null || :)" = "../.claude/skills" ] || R=".agents/skills ortak store değil"
gate "ortak skill store" "$R"

R="ok"
[ -L "$V/.codex/hooks" ] && [ "$(readlink "$V/.codex/hooks" 2>/dev/null || :)" = "../.claude/hooks" ] || R=".codex/hooks ortak store değil"
gate "ortak kanca store" "$R"

R=$(python3 - "$V" <<'PY'
import json, os, sys
v = os.path.realpath(sys.argv[1])
p = os.path.join(v, ".codex", "hooks.json")
try:
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
except (OSError, ValueError) as exc:
    print("hooks.json okunamadı: %s" % exc)
    raise SystemExit(0)
wanted = {
    "SessionStart": ("session-start.sh", 15),
    "UserPromptSubmit": ("prompt-counter.sh", 5),
    "PreCompact": ("pre-compact.sh", 10),
    "SessionEnd": ("session-end.sh", 3),
}
bad = []
for event, (name, timeout) in wanted.items():
    found = []
    for matcher in (d.get("hooks", {}).get(event) or []):
        for hook in (matcher.get("hooks") or []):
            command = hook.get("command", "") or ""
            if name in command:
                found.append(hook)
    if len(found) != 1:
        bad.append("%s=%d" % (event, len(found)))
    elif not os.path.isabs(found[0].get("command", "").strip("'\"")):
        bad.append("%s göreli" % event)
    elif found[0].get("timeout") != timeout:
        bad.append("%s timeout" % event)
print("ok" if not bad else ", ".join(bad))
PY
) || R="kontrol çalışmadı"
gate "Codex mutlak kanca kaydı" "$R"

R=$(python3 - "$V" <<'PY'
import json, os, sys
v = os.path.realpath(sys.argv[1])
p = os.path.join(v, ".agents", "hooks.json")
try:
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
except (OSError, ValueError) as exc:
    print("hooks.json okunamadı: %s" % exc)
    raise SystemExit(0)
managed = d.get("avenox-beyin") if isinstance(d, dict) else None
bad = []
for event, action, timeout in (
    ("PreInvocation", "pre-invocation", 15),
    ("Stop", "stop", 5),
):
    entries = (managed or {}).get(event) or []
    if len(entries) != 1:
        bad.append("%s=%d" % (event, len(entries)))
        continue
    command = entries[0].get("command", "") or ""
    if "antigravity_hooks.py" not in command or action not in command:
        bad.append("%s komut" % event)
    elif entries[0].get("timeout") != timeout:
        bad.append("%s timeout" % event)
print("ok" if not bad else ", ".join(bad))
PY
) || R="kontrol çalışmadı"
gate "Antigravity kanca kaydı" "$R"

for D in "daily" "knowledge/concepts" "knowledge/connections" ".claude/scripts/.state"; do
  R="ok"; [ -d "$V/$D" ] || R="klasör yok"
  gate "klasör $D" "$R"
done
for F in "knowledge/index.md" "knowledge/log.md" "$BEYIN_MEMORY_DIR_NAME/Kurallar.md"; do
  R="ok"; [ -f "$V/$F" ] || R="dosya yok"
  gate "dosya $F" "$R"
done

R="ok"
if [ -f "$MANIFEST" ]; then
  PH=""
  while IFS= read -r M; do
    [ -n "$M" ] || continue
    [ -f "$V/$M" ] || continue
    case "$M" in *.md) ;; *) continue ;; esac
    if grep -q "{{" "$V/$M" 2>/dev/null; then PH="$PH $M"; fi
  done < "$MANIFEST"
  [ -z "$PH" ] || R="çözülmemiş placeholder:$PH"
fi
gate "placeholder çözümü" "$R"

R=$(python3 - "$V" <<'PY'
import json, os, sys
v = sys.argv[1]
V1 = ("session-start.sh", "prompt-counter.sh", "session-end.sh", "pre-compact.sh")
EV = ("SessionStart", "UserPromptSubmit", "SessionEnd", "PreCompact")
count = {e: 0 for e in EV}
for name in ("settings.json", "settings.local.json"):
    p = os.path.join(v, ".claude", name)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        continue
    except ValueError:
        print("bozuk JSON: %s" % name)
        raise SystemExit(0)
    for ev, matchers in (d.get("hooks") or {}).items():
        if ev not in EV:
            continue
        for m in (matchers or []):
            for h in (m.get("hooks") or []):
                c = h.get("command", "") or ""
                if any(b in c for b in V1):
                    count[ev] += 1
bad = ["%s=%d" % (e, n) for e, n in count.items() if n != 1]
print("ok" if not bad else "olay başına tam 1 kanca olmalı: " + ", ".join(bad))
PY
) || R="kontrol çalışmadı"
gate "etkin kanca sayısı (settings.json + settings.local.json)" "$R"

R="ok"
LEAK=$(find "$V" -path "$V/.git" -prune -o -type f \( -name "*.yedek" -o -name "settings.local.json.*" -o -name "*.yedek-*" \) -print 2>/dev/null | head -5)
[ -z "$LEAK" ] || R="vault içinde sır taşıyabilecek yedek var: $(printf '%s' "$LEAK" | tr '\n' ' ')"
gate "vault içinde yedek artığı yok" "$R"

R="ok"; grep -qxF ".claude/settings.local.json" "$V/.gitignore" 2>/dev/null || R=".gitignore settings.local.json'u korumuyor"
gate ".gitignore koruması" "$R"

if [ "$FAIL" != "0" ]; then
  say ""
  die "$FAIL kapı geçilemedi. Sürüm damgası YAZILMADI, vault v1 olarak kalıyor. Önce yukarıdakileri düzelt."
fi

step "commit (açık izin listesi)"
if command -v git >/dev/null 2>&1 && [ -d "$V/.git" ]; then
  git_id
  git -C "$V" reset -q >/dev/null 2>&1 || :
  ALLOW_TMP=$(mktemp)
  printf '%s\n' ".gitignore" "daily" "knowledge" ".claude/hooks" ".claude/scripts" \
                ".claude/skills" ".claude/settings.json" ".agents" ".codex" "AGENTS.md" \
                "$BEYIN_MEMORY_DIR_NAME" > "$ALLOW_TMP"
  while IFS= read -r P; do
    [ -n "$P" ] || continue
    [ -e "$V/$P" ] || continue
    git -C "$V" add -- "$P" || die "git add başarısız: $P"
  done < "$ALLOW_TMP"
  rm -f "$ALLOW_TMP"
  # A rename leaves the old path staged as a deletion; pick that up without add -A.
  git -C "$V" add -u -- . >/dev/null 2>&1 || :
  assert_no_secret_staged
  HEAD_BEFORE=$(git -C "$V" rev-parse HEAD 2>/dev/null || printf 'NONE')
  STAGED=$(git -C "$V" diff --cached --name-only | wc -l | tr -d ' ')
  if [ "$STAGED" = "0" ]; then
    say "sahnede değişiklik yok (yükseltme zaten commit edilmiş olabilir)"
  else
    git -C "$V" -c user.name="$GIT_N" -c user.email="$GIT_E" \
      commit -q -m "v2'ye yükseltildi" \
      || die "yükseltme commit'i başarısız, $STAGED dosya sahnede. Sürüm damgası YAZILMADI."
    HEAD_AFTER=$(git -C "$V" rev-parse HEAD 2>/dev/null || printf 'NONE')
    [ "$HEAD_AFTER" != "$HEAD_BEFORE" ] || die "commit sonrası HEAD değişmedi, commit gerçekten olmadı"
    say "commit: $HEAD_AFTER ($STAGED dosya)"
  fi
else
  say "git yok, commit atlandı (anlık görüntü apply aşamasında kopya olarak alınmıştı)"
fi

step "sürüm damgası (son işlem, atomik)"
STAMP_TMP="$V/.beyin-version.tmp.$$"
printf '%s\n' "$BEYIN_TARGET_VERSION" > "$STAMP_TMP" || die "damga geçici dosyası yazılamadı"
mv -f "$STAMP_TMP" "$V/.beyin-version" || { rm -f "$STAMP_TMP"; die "damga yerine konamadı"; }
[ "$(sed -n '1p' "$V/.beyin-version")" = "$BEYIN_TARGET_VERSION" ] || die "damga doğrulanamadı"
say ".beyin-version = $BEYIN_TARGET_VERSION"

if command -v git >/dev/null 2>&1 && [ -d "$V/.git" ]; then
  git_id
  git -C "$V" add -- ".beyin-version" >/dev/null 2>&1 || :
  git -C "$V" -c user.name="$GIT_N" -c user.email="$GIT_E" \
    commit -q -m "v2 sürüm damgası" >/dev/null 2>&1 \
    || say "UYARI: damga commit edilemedi, dosya diskte doğru. Vault'ta 'git status' ile bak."
fi

rm -f "$STAGE_MARK"
step "YÜKSELTME TAMAM"
say "vault: $V"
say "sürüm: $BEYIN_TARGET_VERSION"
exit 0
