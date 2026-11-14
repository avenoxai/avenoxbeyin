#!/bin/bash
[ -n "${BEYIN_INVOKED_BY:-}" ] && exit 0
# Inject relational memory, rules, recent journal context, and the knowledge index.

BEYIN_HOOK_DIR=$(CDPATH= cd "$(dirname "$0")" 2>/dev/null && pwd)
. "$BEYIN_HOOK_DIR/lib.sh" 2>/dev/null || exit 0

BEYIN_MEMORY_DIR="$BEYIN_PROJECT_DIR/🔮 850-Companion"
mkdir -p "$BEYIN_STATE_DIR" 2>/dev/null || :
beyin_cleanup_session_state

BEYIN_SESSION_KEY=$(beyin_session_key 2>/dev/null || :)
if [ -n "$BEYIN_SESSION_KEY" ]; then
  BEYIN_SESSION_START_FILE="$BEYIN_STATE_DIR/session_start_time.$BEYIN_SESSION_KEY"
  BEYIN_PROMPT_COUNT_FILE="$BEYIN_STATE_DIR/prompt_count.$BEYIN_SESSION_KEY"
  date '+%s' > "$BEYIN_SESSION_START_FILE" 2>/dev/null || :
  printf '%s\n' 0 > "$BEYIN_PROMPT_COUNT_FILE" 2>/dev/null || :
fi

BEYIN_LAST_SESSION=""
if [ -f "$BEYIN_MEMORY_DIR/Last-Session.md" ]; then
  BEYIN_LAST_SESSION=$(awk '
    /^## Session:/ { active = 1 }
    active && /^## Previous/ { exit }
    active { print }
  ' "$BEYIN_MEMORY_DIR/Last-Session.md" 2>/dev/null | sed -n '1,50p')
fi

BEYIN_THREADS=""
if [ -f "$BEYIN_MEMORY_DIR/Threads.md" ]; then
  BEYIN_THREADS=$(sed -n '/^## Active/,/^## Closed/p' "$BEYIN_MEMORY_DIR/Threads.md" 2>/dev/null \
    | grep -E '^### |^\*\*Status:\*\*' 2>/dev/null \
    | sed -n '1,12p')
fi

BEYIN_RULES=""
if [ -f "$BEYIN_MEMORY_DIR/Kurallar.md" ]; then
  BEYIN_RULES=$(sed -n '1,60p' "$BEYIN_MEMORY_DIR/Kurallar.md" 2>/dev/null)
fi

BEYIN_JOURNAL=""
if [ -f "$BEYIN_MEMORY_DIR/Journal.md" ]; then
  BEYIN_JOURNAL_LINE=$(grep -n '^## ' "$BEYIN_MEMORY_DIR/Journal.md" 2>/dev/null \
    | tail -n 1 | cut -d: -f1)
  case "$BEYIN_JOURNAL_LINE" in
    ''|*[!0-9]*) ;;
    *)
      BEYIN_JOURNAL_END=$((BEYIN_JOURNAL_LINE + 9))
      BEYIN_JOURNAL=$(sed -n "${BEYIN_JOURNAL_LINE},${BEYIN_JOURNAL_END}p" \
        "$BEYIN_MEMORY_DIR/Journal.md" 2>/dev/null)
      ;;
  esac
fi

BEYIN_INDEX=""
if [ -f "$BEYIN_PROJECT_DIR/knowledge/index.md" ]; then
  BEYIN_INDEX=$(sed -n '1,150p' "$BEYIN_PROJECT_DIR/knowledge/index.md" 2>/dev/null)
fi

BEYIN_DAILY=""
BEYIN_TODAY=$(date '+%Y-%m-%d' 2>/dev/null || :)
BEYIN_DAILY_FILE=""
if [ -n "$BEYIN_TODAY" ] && [ -f "$BEYIN_PROJECT_DIR/daily/$BEYIN_TODAY.md" ]; then
  BEYIN_DAILY_FILE="$BEYIN_PROJECT_DIR/daily/$BEYIN_TODAY.md"
else
  BEYIN_YESTERDAY=$(beyin_yesterday)
  if [ -n "$BEYIN_YESTERDAY" ] && [ -f "$BEYIN_PROJECT_DIR/daily/$BEYIN_YESTERDAY.md" ]; then
    BEYIN_DAILY_FILE="$BEYIN_PROJECT_DIR/daily/$BEYIN_YESTERDAY.md"
  fi
fi
[ -n "$BEYIN_DAILY_FILE" ] && BEYIN_DAILY=$(tail -n 25 "$BEYIN_DAILY_FILE" 2>/dev/null)

BEYIN_NL='
'
BEYIN_REFLECTION=""
for BEYIN_REFLECTION_FILE in \
  "$BEYIN_STATE_DIR/needs_reflection" \
  "$BEYIN_STATE_DIR"/needs_reflection.*
do
  [ -f "$BEYIN_REFLECTION_FILE" ] || continue
  BEYIN_REFLECTION_DETAIL=$(sed -n '1p' "$BEYIN_REFLECTION_FILE" 2>/dev/null || :)
  if [ -n "$BEYIN_REFLECTION_DETAIL" ]; then
    [ -n "$BEYIN_REFLECTION" ] && BEYIN_REFLECTION="${BEYIN_REFLECTION}${BEYIN_NL}"
    BEYIN_REFLECTION="${BEYIN_REFLECTION}⚠️ Önceki oturum hafıza güncellemeden bitti: ${BEYIN_REFLECTION_DETAIL}. Anlamlı bir şey olduysa 🔮 850-Companion dosyalarını güncelle."
  fi
  rm -f "$BEYIN_REFLECTION_FILE" 2>/dev/null || :
done

# Hard section entry caps, including truncation notes: Last Session 4000,
# Threads 2000, Kurallar 4000, Journal 1500, reflection debt 1000 characters.
beyin_cap_section() {
  BEYIN_CAP_VALUE=$1
  BEYIN_CAP_LIMIT=$2
  BEYIN_CAP_NOTE=$3
  if [ "${#BEYIN_CAP_VALUE}" -le "$BEYIN_CAP_LIMIT" ]; then
    printf '%s' "$BEYIN_CAP_VALUE"
    return 0
  fi

  BEYIN_CAP_KEEP=$((BEYIN_CAP_LIMIT - ${#BEYIN_CAP_NOTE} - 1))
  [ "$BEYIN_CAP_KEEP" -gt 0 ] || BEYIN_CAP_KEEP=0
  printf '%s\n%s' "${BEYIN_CAP_VALUE:0:$BEYIN_CAP_KEEP}" "$BEYIN_CAP_NOTE"
}

BEYIN_LAST_SESSION=$(beyin_cap_section "$BEYIN_LAST_SESSION" 4000 \
  '[not: son oturum 4.000 karakterde kırpıldı, beyin-doktor çalıştır]')
BEYIN_THREADS=$(beyin_cap_section "$BEYIN_THREADS" 2000 \
  '[not: aktif konular 2.000 karakterde kırpıldı, beyin-doktor çalıştır]')
BEYIN_RULES=$(beyin_cap_section "$BEYIN_RULES" 4000 \
  '[not: kurallar 4.000 karakterde kırpıldı, beyin-doktor çalıştır]')
BEYIN_JOURNAL=$(beyin_cap_section "$BEYIN_JOURNAL" 1500 \
  '[not: son Journal 1.500 karakterde kırpıldı, beyin-doktor çalıştır]')
BEYIN_REFLECTION=$(beyin_cap_section "$BEYIN_REFLECTION" 1000 \
  '[not: hafıza uyarıları 1.000 karakterde kırpıldı, beyin-doktor çalıştır]')

BEYIN_TRUNCATED=0
BEYIN_CLOSING='[Hafıza] Süreklilik senin sorumluluğun. Bu kullanıcı için kim olduğunu anlamak üzere 🔮 850-Companion/Core.md dosyasını oku.
Hafıza protokolü zorunludur.'
BEYIN_TRUNCATION_NOTE='[not: indeks kırpıldı, beyin-doktor çalıştır]'
BEYIN_CAP_DIAGNOSTIC='Beyin uyarısı: Oturum başlangıç bağlamı 16.000 karakter sınırına sığmadı. Bölüm limitlerini kontrol etmek için beyin-doktor çalıştır.'

beyin_build_context() {
  BEYIN_CONTEXT=""
  [ -n "$BEYIN_REFLECTION" ] && BEYIN_CONTEXT="${BEYIN_CONTEXT}${BEYIN_REFLECTION}${BEYIN_NL}${BEYIN_NL}"
  [ -n "$BEYIN_LAST_SESSION" ] && BEYIN_CONTEXT="${BEYIN_CONTEXT}[Hafıza: Son Oturum]${BEYIN_NL}${BEYIN_LAST_SESSION}${BEYIN_NL}${BEYIN_NL}"
  [ -n "$BEYIN_THREADS" ] && BEYIN_CONTEXT="${BEYIN_CONTEXT}[Hafıza: Aktif Konular]${BEYIN_NL}${BEYIN_THREADS}${BEYIN_NL}${BEYIN_NL}"
  [ -n "$BEYIN_RULES" ] && BEYIN_CONTEXT="${BEYIN_CONTEXT}[Hafıza: Kurallar]${BEYIN_NL}${BEYIN_RULES}${BEYIN_NL}${BEYIN_NL}"
  [ -n "$BEYIN_JOURNAL" ] && BEYIN_CONTEXT="${BEYIN_CONTEXT}[Hafıza: Son Journal]${BEYIN_NL}${BEYIN_JOURNAL}${BEYIN_NL}${BEYIN_NL}"
  [ -n "$BEYIN_INDEX" ] && BEYIN_CONTEXT="${BEYIN_CONTEXT}[Bilgi Tabanı: İndeks]${BEYIN_NL}${BEYIN_INDEX}${BEYIN_NL}${BEYIN_NL}"
  [ -n "$BEYIN_DAILY" ] && BEYIN_CONTEXT="${BEYIN_CONTEXT}[Bugünün Logu]${BEYIN_NL}${BEYIN_DAILY}${BEYIN_NL}${BEYIN_NL}"
  [ "$BEYIN_TRUNCATED" -eq 1 ] && BEYIN_CONTEXT="${BEYIN_CONTEXT}${BEYIN_TRUNCATION_NOTE}${BEYIN_NL}${BEYIN_NL}"
  BEYIN_CONTEXT="${BEYIN_CONTEXT}${BEYIN_CLOSING}"
}

beyin_build_context
if [ "${#BEYIN_CONTEXT}" -gt 16000 ]; then
  BEYIN_TRUNCATED=1
  beyin_build_context

  BEYIN_OVER=$(( ${#BEYIN_CONTEXT} - 16000 ))
  if [ "$BEYIN_OVER" -gt 0 ] && [ -n "$BEYIN_INDEX" ]; then
    if [ "$BEYIN_OVER" -ge "${#BEYIN_INDEX}" ]; then
      BEYIN_INDEX=""
    else
      BEYIN_KEEP=$(( ${#BEYIN_INDEX} - BEYIN_OVER ))
      BEYIN_INDEX=${BEYIN_INDEX:0:$BEYIN_KEEP}
    fi
    beyin_build_context
  fi

  BEYIN_OVER=$(( ${#BEYIN_CONTEXT} - 16000 ))
  if [ "$BEYIN_OVER" -gt 0 ] && [ -n "$BEYIN_DAILY" ]; then
    if [ "$BEYIN_OVER" -ge "${#BEYIN_DAILY}" ]; then
      BEYIN_DAILY=""
    else
      BEYIN_DAILY=${BEYIN_DAILY:$BEYIN_OVER}
    fi
    beyin_build_context
  fi

  # Journal and reflection are the only remaining non-protected sections.
  BEYIN_OVER=$(( ${#BEYIN_CONTEXT} - 16000 ))
  if [ "$BEYIN_OVER" -gt 0 ] && [ -n "$BEYIN_JOURNAL" ]; then
    if [ "$BEYIN_OVER" -ge "${#BEYIN_JOURNAL}" ]; then
      BEYIN_JOURNAL=""
    else
      BEYIN_KEEP=$(( ${#BEYIN_JOURNAL} - BEYIN_OVER ))
      BEYIN_JOURNAL=${BEYIN_JOURNAL:0:$BEYIN_KEEP}
    fi
    beyin_build_context
  fi

  BEYIN_OVER=$(( ${#BEYIN_CONTEXT} - 16000 ))
  if [ "$BEYIN_OVER" -gt 0 ] && [ -n "$BEYIN_REFLECTION" ]; then
    if [ "$BEYIN_OVER" -ge "${#BEYIN_REFLECTION}" ]; then
      BEYIN_REFLECTION=""
    else
      BEYIN_KEEP=$(( ${#BEYIN_REFLECTION} - BEYIN_OVER ))
      BEYIN_REFLECTION=${BEYIN_REFLECTION:0:$BEYIN_KEEP}
    fi
    beyin_build_context
  fi
fi

if [ "${#BEYIN_CONTEXT}" -gt 16000 ]; then
  BEYIN_CONTEXT=$BEYIN_CAP_DIAGNOSTIC
fi

[ -n "$BEYIN_CONTEXT" ] && beyin_emit SessionStart "$BEYIN_CONTEXT"

# The evening compile is triggered from SessionEnd, which means a day whose last
# session closes before 18:00 never reaches it and its log sits uncompiled. Fire
# the catch-up pass here, detached and after the context is already emitted so it
# can neither delay the session nor corrupt the hook's JSON on stdout. flush.py
# decides whether anything is actually due; the call is cheap when it is not.
if command -v python3 >/dev/null 2>&1; then
  nohup python3 "$BEYIN_PROJECT_DIR/.claude/scripts/flush.py" \
    --maybe-compile >/dev/null 2>&1 &
fi

exit 0
