#!/bin/bash
# Self-contained integration tests for the portable v2 hook set.
set -eu

# Force UTF-8 mode for every python3 subprocess this test script spawns.
# Without it, on a non-English Windows install (e.g. Turkish cp1254),
# Python's stdin/stdout default to the OS locale codepage instead of UTF-8
# and silently corrupt (or crash on) the Turkish text these tests compare.
export PYTHONUTF8=1

TEST_ROOT=$(CDPATH= cd "$(dirname "$0")/.." 2>/dev/null && pwd)
SOURCE_HOOKS="$TEST_ROOT/template/.claude/hooks"
SOURCE_SETTINGS="$TEST_ROOT/template/.claude/settings.json"
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/beyin-hooks.XXXXXX")
trap 'rm -rf "$TEST_TMP"' EXIT HUP INT TERM

PASS_COUNT=0

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  printf 'ok %s - %s\n' "$PASS_COUNT" "$1"
}

fail() {
  printf 'not ok - %s\n' "$1" >&2
  exit 1
}

assert_file() {
  [ -f "$1" ] || fail "dosya yok: $1"
}

assert_contains() {
  case "$1" in
    *"$2"*) ;;
    *) fail "beklenen metin yok: $2" ;;
  esac
}

assert_not_contains() {
  case "$1" in
    *"$2"*) fail "beklenmeyen metin var: $2" ;;
    *) ;;
  esac
}

session_key() {
  python3 - "$1" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest())
PY
}

wait_for_file() {
  python3 - "$1" <<'PY'
import os
import sys
import time

path = sys.argv[1]
for _ in range(100):
    if os.path.isfile(path):
        raise SystemExit(0)
    time.sleep(0.02)
raise SystemExit(1)
PY
}

json_context() {
  python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    lines = [line for line in handle if line.strip()]
if len(lines) != 1:
    raise SystemExit("expected exactly one hook JSON object")
payload = json.loads(lines[0])
print(payload["hookSpecificOutput"]["additionalContext"], end="")
PY
}

assert_json_or_empty() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

path, expected_event = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    for line in handle:
        if not line.strip():
            continue
        payload = json.loads(line)
        output = payload["hookSpecificOutput"]
        assert output["hookEventName"] == expected_event
        assert isinstance(output["additionalContext"], str)
PY
}

assert_exact_start_headers() {
  python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    context = json.load(handle)["hookSpecificOutput"]["additionalContext"]
headers = [
    line for line in context.splitlines()
    if line.startswith("[") and line.endswith("]")
]
assert headers == [
    "[Hafıza: Son Oturum]",
    "[Hafıza: Aktif Konular]",
    "[Hafıza: Kurallar]",
    "[Hafıza: Son Journal]",
    "[Bilgi Tabanı: İndeks]",
    "[Bugünün Logu]",
], headers
assert "\N{EM DASH}" not in context
assert "\N{EN DASH}" not in context
PY
}

HOOK_NAMES="lib.sh session-start.sh prompt-counter.sh session-end.sh pre-compact.sh"
for hook_name in $HOOK_NAMES; do
  assert_file "$SOURCE_HOOKS/$hook_name"
  bash -n "$SOURCE_HOOKS/$hook_name" || fail "bash -n: $hook_name"
done
bash -n "$0" || fail "bash -n: tests/hooks_test.sh"
pass "tüm shell dosyaları bash -n kontrolünden geçti"

python3 - "$SOURCE_HOOKS" <<'PY'
from pathlib import Path
import sys

for path in Path(sys.argv[1]).glob("*.sh"):
    source = path.read_text(encoding="utf-8")
    assert "\N{EM DASH}" not in source, path
    assert "\N{EN DASH}" not in source, path
PY
pass "hook kaynaklarında em dash ve en dash bulunmuyor"

for hook_name in lib.sh session-start.sh prompt-counter.sh session-end.sh pre-compact.sh; do
  guard_line=$(sed -n '2p' "$SOURCE_HOOKS/$hook_name")
  [ "$guard_line" = '[ -n "${BEYIN_INVOKED_BY:-}" ] && exit 0' ] \
    || fail "recursion guard konumu: $hook_name"
done
pass "recursion guard tüm hook betiklerinde erken konumda"

python3 - "$SOURCE_SETTINGS" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    hooks = json.load(handle)["hooks"]
expected = {
    "SessionStart": ("session-start.sh", 15),
    "UserPromptSubmit": ("prompt-counter.sh", 5),
    "SessionEnd": ("session-end.sh", 10),
    "PreCompact": ("pre-compact.sh", 10),
}
assert set(hooks) == set(expected)
for event, (script, timeout) in expected.items():
    entries = hooks[event]
    assert len(entries) == 1
    commands = entries[0]["hooks"]
    assert len(commands) == 1
    command = commands[0]
    assert command == {
        "type": "command",
        "command": f'"$CLAUDE_PROJECT_DIR/.claude/hooks/{script}"',
        "timeout": timeout,
    }
PY
pass "settings.json dört olayı doğru timeout ve proje yollarıyla bağlıyor"

VAULT="$TEST_TMP/vault"
HOOKS="$VAULT/.claude/hooks"
STATE="$VAULT/.claude/scripts/.state"
MEMORY="$VAULT/🔮 850-Companion"
mkdir -p "$HOOKS" "$STATE" "$MEMORY" "$VAULT/knowledge" "$VAULT/daily"
cp "$SOURCE_HOOKS"/*.sh "$HOOKS/"
chmod +x "$HOOKS"/*.sh

cat > "$MEMORY/Last-Session.md" <<'EOF'
# Last Session
## Session: Current
EOF
i=1
while [ "$i" -le 55 ]; do
  printf 'current-%02d\n' "$i" >> "$MEMORY/Last-Session.md"
  i=$((i + 1))
done
cat >> "$MEMORY/Last-Session.md" <<'EOF'
## Previous
previous-secret
EOF

cat > "$MEMORY/Threads.md" <<'EOF'
# Threads
## Active
EOF
i=1
while [ "$i" -le 7 ]; do
  printf '### Active-%02d\n**Status:** açık-%02d\n' "$i" "$i" >> "$MEMORY/Threads.md"
  i=$((i + 1))
done
cat >> "$MEMORY/Threads.md" <<'EOF'
## Closed
### Closed-01
**Status:** kapalı
EOF

: > "$MEMORY/Kurallar.md"
i=1
while [ "$i" -le 61 ]; do
  printf 'rule-%02d\n' "$i" >> "$MEMORY/Kurallar.md"
  i=$((i + 1))
done

cat > "$MEMORY/Journal.md" <<'EOF'
# Journal
## Old Entry
old-journal-secret
## Latest Entry
EOF
i=1
while [ "$i" -le 12 ]; do
  printf 'journal-latest-%02d\n' "$i" >> "$MEMORY/Journal.md"
  i=$((i + 1))
done

: > "$VAULT/knowledge/index.md"
i=1
while [ "$i" -le 151 ]; do
  printf 'index-%03d\n' "$i" >> "$VAULT/knowledge/index.md"
  i=$((i + 1))
done

TODAY=$(date '+%Y-%m-%d')
: > "$VAULT/daily/$TODAY.md"
i=1
while [ "$i" -le 30 ]; do
  printf 'daily-%03d\n' "$i" >> "$VAULT/daily/$TODAY.md"
  i=$((i + 1))
done
printf '%s\n' 'önceki borç' > "$STATE/needs_reflection"

START_OUT="$TEST_TMP/session-start.json"
START_KEY=$(session_key s-start)
printf '%s\n' '{"session_id":"s-start","transcript_path":"/tmp/transcript.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/session-start.sh" > "$START_OUT"
assert_json_or_empty "$START_OUT" SessionStart
assert_exact_start_headers "$START_OUT"
CONTEXT=$(json_context "$START_OUT")
assert_contains "$CONTEXT" '⚠️ Önceki oturum hafıza güncellemeden bitti:'
assert_contains "$CONTEXT" '[Hafıza: Son Oturum]'
assert_contains "$CONTEXT" 'current-49'
assert_not_contains "$CONTEXT" 'current-50'
assert_not_contains "$CONTEXT" 'previous-secret'
assert_contains "$CONTEXT" '[Hafıza: Aktif Konular]'
assert_contains "$CONTEXT" 'Active-06'
assert_not_contains "$CONTEXT" 'Active-07'
assert_not_contains "$CONTEXT" 'Closed-01'
assert_contains "$CONTEXT" '[Hafıza: Kurallar]'
assert_contains "$CONTEXT" 'rule-60'
assert_not_contains "$CONTEXT" 'rule-61'
assert_contains "$CONTEXT" '[Hafıza: Son Journal]'
assert_not_contains "$CONTEXT" 'old-journal-secret'
assert_contains "$CONTEXT" 'journal-latest-09'
assert_not_contains "$CONTEXT" 'journal-latest-10'
assert_contains "$CONTEXT" '[Bilgi Tabanı: İndeks]'
assert_contains "$CONTEXT" 'index-150'
assert_not_contains "$CONTEXT" 'index-151'
assert_contains "$CONTEXT" '[Bugünün Logu]'
assert_contains "$CONTEXT" 'daily-006'
assert_not_contains "$CONTEXT" 'daily-005'
assert_contains "$CONTEXT" 'daily-030'
assert_contains "$CONTEXT" 'Hafıza protokolü zorunludur.'
[ ! -e "$STATE/needs_reflection" ] || fail "reflection işareti yüzeye çıktıktan sonra temizlenmedi"
[ "$(sed -n '1p' "$STATE/prompt_count.$START_KEY")" = 0 ] || fail "prompt sayacı sıfırlanmadı"
case "$(sed -n '1p' "$STATE/session_start_time.$START_KEY")" in
  ''|*[!0-9]*) fail "session_start_time epoch değil" ;;
esac
pass "SessionStart tüm bölüm başlıklarını tam kolon biçimiyle ve doğru sınırlarda enjekte ediyor"

python3 - "$VAULT/knowledge/index.md" <<'PY'
import sys

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    for number in range(1, 151):
        handle.write(f"index-huge-{number:03d}-{'ç' * 200}\n")
PY
CAP_OUT="$TEST_TMP/session-start-cap.json"
printf '%s\n' '{"session_id":"s-cap","transcript_path":"/tmp/transcript.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/session-start.sh" > "$CAP_OUT"
CAP_CONTEXT=$(json_context "$CAP_OUT")
python3 - "$CAP_OUT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    context = json.load(handle)["hookSpecificOutput"]["additionalContext"]
assert len(context) <= 16000, len(context)
PY
assert_contains "$CAP_CONTEXT" '[not: indeks kırpıldı, beyin-doktor çalıştır]'
assert_contains "$CAP_CONTEXT" 'current-49'
assert_contains "$CAP_CONTEXT" 'Active-06'
assert_contains "$CAP_CONTEXT" 'rule-60'
assert_contains "$CAP_CONTEXT" 'daily-006'
assert_contains "$CAP_CONTEXT" 'daily-030'
pass "16.000 karakter bütçesi indeksi önce kırpıp korunan bölümleri ve günlük kuyruğunu saklıyor"

cp "$MEMORY/Last-Session.md" "$TEST_TMP/Last-Session.saved"
cp "$MEMORY/Threads.md" "$TEST_TMP/Threads.saved"
cp "$MEMORY/Kurallar.md" "$TEST_TMP/Kurallar.saved"
python3 - "$MEMORY" <<'PY'
from pathlib import Path
import sys

memory = Path(sys.argv[1])
(memory / "Last-Session.md").write_text(
    "# Last Session\n## Session: Huge\nlast-" + "L" * 20_000
    + "-LAST_TAIL\n## Previous\nsecret\n",
    encoding="utf-8",
)
(memory / "Threads.md").write_text(
    "# Threads\n## Active\n### thread-" + "T" * 10_000
    + "-THREAD_TAIL\n**Status:** " + "S" * 10_000
    + "\n## Closed\n",
    encoding="utf-8",
)
(memory / "Kurallar.md").write_text(
    "rule-" + "K" * 20_000 + "-RULE_TAIL\n",
    encoding="utf-8",
)
PY
PROTECTED_OUT="$TEST_TMP/session-start-protected-cap.json"
printf '%s\n' '{"session_id":"s-protected","transcript_path":"/tmp/transcript.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/session-start.sh" > "$PROTECTED_OUT"
assert_json_or_empty "$PROTECTED_OUT" SessionStart
PROTECTED_CONTEXT=$(json_context "$PROTECTED_OUT")
python3 - "$PROTECTED_OUT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    context = json.load(handle)["hookSpecificOutput"]["additionalContext"]
assert len(context) <= 16_000, len(context)
PY
assert_contains "$PROTECTED_CONTEXT" '[not: son oturum 4.000 karakterde kırpıldı, beyin-doktor çalıştır]'
assert_contains "$PROTECTED_CONTEXT" '[not: aktif konular 2.000 karakterde kırpıldı, beyin-doktor çalıştır]'
assert_contains "$PROTECTED_CONTEXT" '[not: kurallar 4.000 karakterde kırpıldı, beyin-doktor çalıştır]'
assert_not_contains "$PROTECTED_CONTEXT" 'LAST_TAIL'
assert_not_contains "$PROTECTED_CONTEXT" 'THREAD_TAIL'
assert_not_contains "$PROTECTED_CONTEXT" 'RULE_TAIL'
pass "korunan üç bölüm kendi sert karakter limitlerinde kalıyor ve toplam bağlam sınırı aşılmıyor"
mv "$TEST_TMP/Last-Session.saved" "$MEMORY/Last-Session.md"
mv "$TEST_TMP/Threads.saved" "$MEMORY/Threads.md"
mv "$TEST_TMP/Kurallar.saved" "$MEMORY/Kurallar.md"

OLD_KEY=$(session_key old-session)
RECENT_KEY=$(session_key recent-session)
printf '%s\n' 1 > "$STATE/session_start_time.$OLD_KEY"
printf '%s\n' 2 > "$STATE/prompt_count.$OLD_KEY"
printf '%s\n' old > "$STATE/needs_reflection.$OLD_KEY"
printf '%s\n' 3 > "$STATE/prompt_count.$RECENT_KEY"
python3 - "$STATE" "$OLD_KEY" <<'PY'
import os
from pathlib import Path
import sys
import time

state = Path(sys.argv[1])
key = sys.argv[2]
old = time.time() - 9 * 24 * 60 * 60
for name in (
    f"session_start_time.{key}",
    f"prompt_count.{key}",
    f"needs_reflection.{key}",
):
    os.utime(state / name, (old, old))
PY
CLEANUP_OUT="$TEST_TMP/session-start-cleanup.json"
printf '%s\n' '{"session_id":"s-cleanup","transcript_path":"/tmp/transcript.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/session-start.sh" > "$CLEANUP_OUT"
[ ! -e "$STATE/session_start_time.$OLD_KEY" ] || fail "eski session_start_time temizlenmedi"
[ ! -e "$STATE/prompt_count.$OLD_KEY" ] || fail "eski prompt_count temizlenmedi"
[ ! -e "$STATE/needs_reflection.$OLD_KEY" ] || fail "eski needs_reflection temizlenmedi"
[ "$(sed -n '1p' "$STATE/prompt_count.$RECENT_KEY")" = 3 ] || fail "yeni oturum durumu temizlendi"
pass "SessionStart yedi günden eski oturum durumunu temizleyip yeni durumu koruyor"

rm -f "$VAULT/daily/$TODAY.md"
YESTERDAY=$(CLAUDE_PROJECT_DIR="$VAULT" /bin/bash -c '. "$1"; beyin_yesterday' _ "$HOOKS/lib.sh")
case "$YESTERDAY" in
  ????-??-??) ;;
  *) fail "dünün tarihi taşınabilir biçimde hesaplanamadı" ;;
esac
: > "$VAULT/daily/$YESTERDAY.md"
i=1
while [ "$i" -le 30 ]; do
  printf 'yesterday-%03d\n' "$i" >> "$VAULT/daily/$YESTERDAY.md"
  i=$((i + 1))
done
YESTERDAY_OUT="$TEST_TMP/session-start-yesterday.json"
printf '%s\n' '{"session_id":"s-yesterday","transcript_path":"/tmp/transcript.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/session-start.sh" > "$YESTERDAY_OUT"
YESTERDAY_CONTEXT=$(json_context "$YESTERDAY_OUT")
assert_contains "$YESTERDAY_CONTEXT" '[Bugünün Logu]'
assert_contains "$YESTERDAY_CONTEXT" 'yesterday-006'
assert_not_contains "$YESTERDAY_CONTEXT" 'yesterday-005'
assert_contains "$YESTERDAY_CONTEXT" 'yesterday-030'
pass "bugünün logu yoksa BSD/GNU uyumlu dün hesabıyla son 25 satır enjekte ediliyor"

MTIME=$(CLAUDE_PROJECT_DIR="$VAULT" /bin/bash -c '. "$1"; beyin_mtime "$2"' \
  _ "$HOOKS/lib.sh" "$MEMORY/Last-Session.md")
case "$MTIME" in
  ''|*[!0-9]*) fail "beyin_mtime sayısal sonuç üretmedi" ;;
esac
[ "$MTIME" -gt 0 ] || fail "beyin_mtime sıfır döndürdü"
pass "beyin_mtime BSD/GNU sonucu doğrulayıp sayısal epoch üretiyor"

SERIAL_SESSION=s-counter-serial
SERIAL_KEY=$(session_key "$SERIAL_SESSION")
printf '%s\n' '{"session_id":"s-counter-serial","transcript_path":"/tmp/transcript.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/session-start.sh" >/dev/null
NUDGES="$TEST_TMP/nudges.jsonl"
: > "$NUDGES"
i=1
while [ "$i" -le 30 ]; do
  PROMPT_OUT="$TEST_TMP/prompt-$i.out"
  printf '%s\n' '{"session_id":"s-counter-serial","prompt":"deneme"}' \
    | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/prompt-counter.sh" > "$PROMPT_OUT"
  assert_json_or_empty "$PROMPT_OUT" UserPromptSubmit
  [ ! -s "$PROMPT_OUT" ] || cat "$PROMPT_OUT" >> "$NUDGES"
  i=$((i + 1))
done
python3 - "$NUDGES" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    messages = [
        json.loads(line)["hookSpecificOutput"]["additionalContext"]
        for line in handle
        if line.strip()
    ]
assert messages == [
    "[Hafıza] 15. mesaj. Oturum sonunda 🔮 850-Companion/Last-Session.md ve Threads.md güncellemeyi unutma.",
    "[Hafıza] 30. mesaj. Oturum sonunda 🔮 850-Companion/Last-Session.md ve Threads.md güncellemeyi unutma.",
]
PY
[ "$(sed -n '1p' "$STATE/prompt_count.$SERIAL_KEY")" = 30 ] || fail "prompt sayacı 30 değil"
pass "UserPromptSubmit yalnızca her 15. mesajda tam Türkçe metni yayıyor"

CONCURRENT_SESSION=s-counter-concurrent
CONCURRENT_KEY=$(session_key "$CONCURRENT_SESSION")
printf '%s\n' '{"session_id":"s-counter-concurrent","transcript_path":"/tmp/transcript.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/session-start.sh" >/dev/null
CONCURRENT_PIDS=""
i=1
while [ "$i" -le 100 ]; do
  CONCURRENT_OUT="$TEST_TMP/concurrent-$i.out"
  printf '%s\n' '{"session_id":"s-counter-concurrent","prompt":"eşzamanlı"}' \
    | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/prompt-counter.sh" > "$CONCURRENT_OUT" &
  CONCURRENT_PIDS="$CONCURRENT_PIDS $!"
  i=$((i + 1))
done
for CONCURRENT_PID in $CONCURRENT_PIDS; do
  wait "$CONCURRENT_PID" || fail "eşzamanlı prompt-counter başarısız: $CONCURRENT_PID"
done
i=1
while [ "$i" -le 100 ]; do
  assert_json_or_empty "$TEST_TMP/concurrent-$i.out" UserPromptSubmit
  i=$((i + 1))
done
[ "$(sed -n '1p' "$STATE/prompt_count.$CONCURRENT_KEY")" = 100 ] \
  || fail "100 eşzamanlı çağrı sonrası sayaç 100 değil"
pass "100 gerçek paralel prompt-counter çağrısı atomik kilitle tam olarak 100 sayılıyor"

cat > "$VAULT/.claude/scripts/flush.py" <<'PY'
#!/usr/bin/env python3
import argparse
import json
import os
import time

parser = argparse.ArgumentParser()
parser.add_argument("--hook-input", required=True)
parser.add_argument("--reason", default="sessionend")
args = parser.parse_args()
with open(args.hook_input, encoding="utf-8") as handle:
    hook_input = json.load(handle)
state_dir = os.path.join(os.path.dirname(__file__), ".state")
with open(os.path.join(state_dir, "flush-" + args.reason + ".json"), "w", encoding="utf-8") as handle:
    json.dump(
        {"hook_input": hook_input, "hook_input_path": args.hook_input, "reason": args.reason},
        handle,
    )
time.sleep(2)
PY
chmod +x "$VAULT/.claude/scripts/flush.py"

END_SESSION=s-end
END_KEY=$(session_key "$END_SESSION")
LIVE_SESSION=s-live
LIVE_KEY=$(session_key "$LIVE_SESSION")
printf '%s\n' '{"session_id":"s-end","transcript_path":"/tmp/end.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/session-start.sh" >/dev/null
i=1
while [ "$i" -le 4 ]; do
  printf '%s\n' '{"session_id":"s-end","prompt":"deneme"}' \
    | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/prompt-counter.sh" >/dev/null
  i=$((i + 1))
done
printf '%s\n' '{"session_id":"s-live","transcript_path":"/tmp/live.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/session-start.sh" >/dev/null
i=1
while [ "$i" -le 3 ]; do
  printf '%s\n' '{"session_id":"s-live","prompt":"deneme"}' \
    | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/prompt-counter.sh" >/dev/null
  i=$((i + 1))
done
printf '%s\n' '{"session_id":"s-end","prompt":"deneme"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/prompt-counter.sh" >/dev/null
[ "$(sed -n '1p' "$STATE/prompt_count.$END_KEY")" = 5 ] || fail "s-end sayacı 5 değil"
[ "$(sed -n '1p' "$STATE/prompt_count.$LIVE_KEY")" = 3 ] || fail "s-live sayacı 3 değil"
printf '%s\n' 9999999999 > "$STATE/session_start_time.$END_KEY"
rm -f "$STATE/needs_reflection.$END_KEY" "$STATE/flush-sessionend.json"
SESSION_END_OUT="$TEST_TMP/session-end.out"
python3 - "$HOOKS/session-end.sh" "$VAULT" "$SESSION_END_OUT" <<'PY'
import json
import os
import subprocess
import sys
import time

hook, vault, output_path = sys.argv[1:]
payload = json.dumps({"session_id": "s-end", "transcript_path": "/tmp/end.jsonl"}).encode()
env = os.environ.copy()
env["CLAUDE_PROJECT_DIR"] = vault
started = time.monotonic()
# subprocess.run has no shebang-line awareness (unlike bash's own exec, which
# is why every other direct "$HOOKS/…" call in this script works under Git
# Bash on Windows). Route explicitly through bash here so this one Python-
# side invocation doesn't hit WinError 193 ("%1 is not a valid Win32
# application") trying to launch a #!/bin/bash script as a native binary.
argv = ["bash", hook] if sys.platform == "win32" else [hook]
result = subprocess.run(argv, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
elapsed = time.monotonic() - started
assert result.returncode == 0, result.stderr.decode()
assert elapsed < 1.0, elapsed
with open(output_path, "wb") as handle:
    handle.write(result.stdout)
PY
assert_json_or_empty "$SESSION_END_OUT" SessionEnd
wait_for_file "$STATE/flush-sessionend.json" || fail "SessionEnd flush stub çağrılmadı"
python3 - "$STATE/flush-sessionend.json" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    call = json.load(handle)
assert call["reason"] == "sessionend"
assert call["hook_input"] == {
    "session_id": "s-end",
    "transcript_path": "/tmp/end.jsonl",
}
assert os.path.basename(call["hook_input_path"]).startswith("hookin-")
PY
assert_file "$STATE/needs_reflection.$END_KEY"
assert_contains "$(sed -n '1p' "$STATE/needs_reflection.$END_KEY")" 'Prompt: 5.'
[ ! -e "$STATE/session_start_time.$END_KEY" ] || fail "SessionEnd kendi session_start_time dosyasını temizlemedi"
[ ! -e "$STATE/prompt_count.$END_KEY" ] || fail "SessionEnd kendi prompt_count dosyasını temizlemedi"
case "$(sed -n '1p' "$STATE/session_start_time.$LIVE_KEY")" in
  ''|*[!0-9]*) fail "SessionEnd canlı oturumun başlangıç durumunu bozdu" ;;
esac
[ "$(sed -n '1p' "$STATE/prompt_count.$LIVE_KEY")" = 3 ] \
  || fail "SessionEnd canlı oturumun sayacını bozdu"
pass "iki oturum iç içe ilerlerken SessionEnd yalnızca kendi durumunu ve reflection borcunu değiştiriyor"

PRE_KEY=$(session_key s-pre)
printf '%s\n' 123 > "$STATE/session_start_time.$PRE_KEY"
printf '%s\n' 9 > "$STATE/prompt_count.$PRE_KEY"
rm -f "$STATE/flush-precompact.json"
PRECOMPACT_OUT="$TEST_TMP/precompact.out"
printf '%s\n' '{"session_id":"s-pre","transcript_path":"/tmp/pre.jsonl"}' \
  | CLAUDE_PROJECT_DIR="$VAULT" "$HOOKS/pre-compact.sh" > "$PRECOMPACT_OUT"
assert_json_or_empty "$PRECOMPACT_OUT" PreCompact
wait_for_file "$STATE/flush-precompact.json" || fail "PreCompact flush stub çağrılmadı"
python3 - "$STATE/flush-precompact.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    call = json.load(handle)
assert call["reason"] == "precompact"
assert call["hook_input"] == {
    "session_id": "s-pre",
    "transcript_path": "/tmp/pre.jsonl",
}
PY
[ "$(sed -n '1p' "$STATE/session_start_time.$PRE_KEY")" = 123 ] || fail "PreCompact session state değiştirdi"
[ "$(sed -n '1p' "$STATE/prompt_count.$PRE_KEY")" = 9 ] || fail "PreCompact prompt state değiştirdi"
pass "PreCompact reason bayrağıyla flush başlatır ve canlı oturum durumunu korur"

GUARD_VAULT="$TEST_TMP/guard-vault"
GUARD_HOOKS="$GUARD_VAULT/.claude/hooks"
mkdir -p "$GUARD_HOOKS"
cp "$SOURCE_HOOKS"/*.sh "$GUARD_HOOKS/"
chmod +x "$GUARD_HOOKS"/*.sh
for hook_name in session-start.sh prompt-counter.sh session-end.sh pre-compact.sh; do
  GUARD_OUT="$TEST_TMP/guard-$hook_name.out"
  printf '%s\n' '{"session_id":"guard","transcript_path":"/tmp/guard.jsonl"}' \
    | BEYIN_INVOKED_BY=beyin-scripts CLAUDE_PROJECT_DIR="$GUARD_VAULT" \
      "$GUARD_HOOKS/$hook_name" > "$GUARD_OUT"
  [ ! -s "$GUARD_OUT" ] || fail "guard çıktıyı kesmedi: $hook_name"
done
[ ! -e "$GUARD_VAULT/.claude/scripts/.state" ] || fail "guard state yan etkisini engellemedi"
pass "BEYIN_INVOKED_BY dört hook'u tüm yan etkilerden önce durduruyor"

NO_PY_BIN="$TEST_TMP/no-python-bin"
mkdir -p "$NO_PY_BIN"
ln -s "$(command -v mkdir)" "$NO_PY_BIN/mkdir"
ln -s "$(command -v sed)" "$NO_PY_BIN/sed"
# On MSYS2/Git-Bash (Windows), mkdir/sed are PE binaries dynamically linked
# against msys-2.0.dll and friends, resolved relative to the launching
# executable's own directory. A PATH containing only their symlinks makes
# them fail to load ("error while loading shared libraries"). Bring the
# runtime DLLs along too; this is a no-op (glob matches nothing) on POSIX.
for dll in "$(dirname "$(command -v bash)")"/*.dll; do
  [ -e "$dll" ] || continue
  ln -sf "$dll" "$NO_PY_BIN/$(basename "$dll")" 2>/dev/null || :
done
NO_PY_OUT="$TEST_TMP/no-python.out"
CLAUDE_PROJECT_DIR="$VAULT" PATH="$NO_PY_BIN" /bin/bash -c \
  '. "$1"; beyin_emit SessionStart "ignored unicode: ç"' _ "$HOOKS/lib.sh" > "$NO_PY_OUT"
/usr/bin/env python3 - "$NO_PY_OUT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
output = payload["hookSpecificOutput"]
assert output["hookEventName"] == "SessionStart"
message = output["additionalContext"]
assert message.isascii()
assert "python3 bulunamadi" in message
PY
assert_file "$STATE/python3-missing"
pass "python3 yokluğunda marker ve geçerli ASCII fallback JSON üretiliyor"

printf '1..%s\n' "$PASS_COUNT"
