#!/usr/bin/env bash
# BLOCKER 3: v1 settings.local.json, kanca tekilleştirme ve sır güvenliği regresyonları.
set -euo pipefail

# Force UTF-8 mode for every python3 subprocess this test spawns. Without it,
# on a non-English Windows install (e.g. Turkish cp1254), Python's stdin/
# stdout default to the OS locale codepage instead of UTF-8 and corrupt (or
# crash on) the Turkish text these tests write and compare.
export PYTHONUTF8=1

TEST_ROOT=$(CDPATH= cd "$(dirname "$0")/.." 2>/dev/null && pwd)
UPGRADE="$TEST_ROOT/scripts/upgrade.sh"
FIXTURE="$TEST_ROOT/tests/fixtures/v1_vault.sh"

# Fixture ve yükseltme sonucu kullanıcının global ignore/config ayarlarına bağlı kalmasın.
GIT_CONFIG_NOSYSTEM=1
GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM GIT_CONFIG_GLOBAL

# shellcheck source=fixtures/v1_vault.sh
. "$FIXTURE"

TEST_COUNT=0
FAIL_COUNT=0
CASE_ROOT=""
CASE_VAULT=""
CASE_BACKUP=""
RUN_RC=0

cleanup_case() {
  if [ -n "$CASE_ROOT" ] && [ -d "$CASE_ROOT" ]; then
    rm -rf "$CASE_ROOT"
  fi
  CASE_ROOT=""
  CASE_VAULT=""
  CASE_BACKUP=""
}
trap cleanup_case EXIT HUP INT TERM

pass() {
  TEST_COUNT=$((TEST_COUNT + 1))
  printf 'ok %s - %s\n' "$TEST_COUNT" "$1"
}

fail() {
  TEST_COUNT=$((TEST_COUNT + 1))
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf 'not ok %s - %s\n' "$TEST_COUNT" "$1"
}

diag() {
  printf '# %s\n' "$*" >&2
}

new_case() {
  cleanup_case
  CASE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/beyin-upgrade-settings.XXXXXX")
  CASE_VAULT="$CASE_ROOT/vault"
  CASE_BACKUP="$CASE_ROOT/vault-disinda-yedek"
  mkdir -p "$CASE_ROOT/sahte-home"
  make_v1_vault "$CASE_VAULT" "$@"
  if [ -d "$CASE_VAULT/.git" ] && [ -f "$CASE_VAULT/.claude/settings.local.json" ]; then
    git -C "$CASE_VAULT" add -f -- .claude/settings.local.json
    if ! git -C "$CASE_VAULT" diff --cached --quiet; then
      git -C "$CASE_VAULT" -c commit.gpgsign=false commit -q --amend --no-edit
    fi
  fi
}

run_stage() {
  local output="$1" stage="$2"
  shift 2
  if HOME="$CASE_ROOT/sahte-home" BEYIN_BACKUP_ROOT="$CASE_BACKUP" \
      bash "$UPGRADE" --vault "$CASE_VAULT" --stage "$stage" "$@" \
      >"$output" 2>&1; then
    RUN_RC=0
  else
    RUN_RC=$?
  fi
  return 0
}

show_stage_failure() {
  local stage="$1" output="$2"
  diag "$stage aşaması başarısız (çıkış: $RUN_RC)"
  sed 's/^/# /' "$output" >&2 || :
}

resolve_fixture_placeholders() {
  local manifest="$CASE_VAULT/.claude/scripts/.state/upgrade-manifest.txt"
  local relative path temporary
  [ -f "$manifest" ] || return 0
  while IFS= read -r relative; do
    case "$relative" in
      *.md) ;;
      *) continue ;;
    esac
    path="$CASE_VAULT/$relative"
    [ -f "$path" ] || continue
    grep -q '{{' "$path" 2>/dev/null || continue
    temporary="$path.test-tmp"
    sed 's/{{[^}]*}}/TEST-DEGERI/g' "$path" > "$temporary"
    mv -f "$temporary" "$path"
  done < "$manifest"
}

full_chain() {
  local apply_out="$CASE_ROOT/apply.out"
  local finalize_out="$CASE_ROOT/finalize.out"
  run_stage "$apply_out" apply --confirm-local-hooks
  if [ "$RUN_RC" -ne 0 ]; then
    show_stage_failure apply "$apply_out"
    return 1
  fi
  resolve_fixture_placeholders || {
    diag "fixture placeholder'ları doldurulamadı"
    return 1
  }
  run_stage "$finalize_out" finalize
  if [ "$RUN_RC" -ne 0 ]; then
    show_stage_failure finalize "$finalize_out"
    return 1
  fi
  return 0
}

tree_digest() {
  python3 - "$1" <<'PY'
import hashlib
import os
import stat
import sys

root = os.path.realpath(sys.argv[1])
digest = hashlib.sha256()
for current, dirs, files in os.walk(root):
    dirs[:] = sorted(d for d in dirs if d != ".git")
    for name in sorted(dirs + files):
        path = os.path.join(current, name)
        rel = os.path.relpath(path, root)
        info = os.lstat(path)
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(oct(stat.S_IMODE(info.st_mode)).encode("ascii") + b"\0")
        if stat.S_ISLNK(info.st_mode):
            digest.update(os.readlink(path).encode("utf-8") + b"\0")
        elif stat.S_ISREG(info.st_mode):
            with open(path, "rb") as handle:
                digest.update(handle.read())
            digest.update(b"\0")
print(digest.hexdigest())
PY
}

test_confirmation_is_non_mutating() {
  new_case
  local before after before_head after_head before_status after_status
  before=$(tree_digest "$CASE_VAULT")
  before_head=$(git -C "$CASE_VAULT" rev-parse HEAD)
  before_status=$(git -C "$CASE_VAULT" status --porcelain=v1 --untracked-files=all)

  run_stage "$CASE_ROOT/apply.out" apply
  [ "$RUN_RC" -eq 11 ] || {
    diag "--confirm-local-hooks olmadan beklenen 11, gelen $RUN_RC"
    return 1
  }

  after=$(tree_digest "$CASE_VAULT")
  after_head=$(git -C "$CASE_VAULT" rev-parse HEAD)
  after_status=$(git -C "$CASE_VAULT" status --porcelain=v1 --untracked-files=all)
  [ "$before" = "$after" ] && [ "$before_head" = "$after_head" ] \
    && [ "$before_status" = "$after_status" ] || {
      diag "onaysız apply vault'u değiştirdi"
      return 1
    }
}

test_unrelated_hook_preserved() {
  new_case
  full_chain || return 1
  python3 - "$CASE_VAULT/.claude/settings.local.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
assert data["hooks"]["Notification"] == [{
    "matcher": "*",
    "hooks": [{
        "type": "command",
        "command": "/usr/local/bin/bildirim-sesi.sh",
        "timeout": 5,
    }],
}]
PY
}

test_env_and_permissions_preserved() {
  new_case
  full_chain || return 1
  python3 - "$CASE_VAULT/.claude/settings.local.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
assert data["env"] == {"MEM0_API_KEY": "m0-GIZLI-ANAHTAR-ASLA-COMMITLENMEZ"}
assert data["permissions"] == {"allow": ["Bash(git status)"]}
PY
}

test_v1_local_hook_removed() {
  new_case
  full_chain || return 1
  python3 - "$CASE_VAULT/.claude/settings.local.json" <<'PY'
import json
import sys

managed_events = {"SessionStart", "UserPromptSubmit", "SessionEnd", "PreCompact"}
managed_scripts = ("session-start.sh", "prompt-counter.sh", "session-end.sh", "pre-compact.sh")
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
for event, matchers in (data.get("hooks") or {}).items():
    for matcher in matchers or []:
        for hook in matcher.get("hooks") or []:
            command = hook.get("command", "") or ""
            assert not (event in managed_events and any(name in command for name in managed_scripts)), (event, command)
PY
}

test_exactly_one_hook_per_event() {
  new_case
  full_chain || return 1
  python3 - "$CASE_VAULT" <<'PY'
import json
import os
import sys

events = ("SessionStart", "UserPromptSubmit", "SessionEnd", "PreCompact")
scripts = ("session-start.sh", "prompt-counter.sh", "session-end.sh", "pre-compact.sh")
counts = {event: 0 for event in events}
for filename in ("settings.json", "settings.local.json"):
    path = os.path.join(sys.argv[1], ".claude", filename)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        continue
    assert isinstance(data, dict), filename
    for event, matchers in (data.get("hooks") or {}).items():
        if event not in counts:
            continue
        for matcher in matchers or []:
            for hook in matcher.get("hooks") or []:
                command = hook.get("command", "") or ""
                if any(script in command for script in scripts):
                    counts[event] += 1
assert counts == {event: 1 for event in events}, counts
PY
}

test_secret_files_not_staged_or_committed() {
  new_case
  full_chain || return 1
  local committed cached_names cached_patch
  committed=$(git -C "$CASE_VAULT" diff-tree --no-commit-id --name-only -r HEAD)
  if printf '%s\n' "$committed" \
      | grep -E '(^|/)settings\.local\.json$|(^|/)[^/]*\.yedek([^/]*)?$|(^|/)[^/]*\.bak$|(^|/)\.env$' \
      >/dev/null; then
    diag "son commit sır taşıyabilecek dosya içeriyor: $committed"
    return 1
  fi

  cached_names=$(git -C "$CASE_VAULT" diff --cached --name-only)
  cached_patch=$(git -C "$CASE_VAULT" diff --cached)
  if printf '%s\n' "$cached_names" \
      | grep -E '(^|/)settings\.local\.json$|(^|/)[^/]*\.yedek([^/]*)?$|(^|/)[^/]*\.bak$|(^|/)\.env$' \
      >/dev/null; then
    diag "indexte sır taşıyabilecek dosya var: $cached_names"
    return 1
  fi
  if printf '%s' "$cached_patch" | grep -E 'm0-GIZLI-ANAHTAR|sk-GIZLI' >/dev/null; then
    diag "git diff --cached içinde sır bulundu"
    return 1
  fi
}

test_backup_outside_vault_and_mode_0600() {
  new_case
  full_chain || return 1
  python3 - "$CASE_VAULT" "$CASE_BACKUP" <<'PY'
import os
import stat
import sys
from pathlib import Path

vault = Path(sys.argv[1]).resolve()
backup_root = Path(sys.argv[2]).resolve()
secret = b"m0-GIZLI-ANAHTAR"
secret_backups = []
for path in backup_root.rglob("*"):
    if path.is_file() and secret in path.read_bytes():
        secret_backups.append(path.resolve())
assert secret_backups, "sır içeren harici settings.local yedeği bulunamadı"
for path in secret_backups:
    assert vault not in path.parents, path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, (path, oct(stat.S_IMODE(path.stat().st_mode)))
PY

  local inside path
  inside=$(find "$CASE_VAULT" -path "$CASE_VAULT/.git" -prune -o -type f \
    \( -name '*.yedek' -o -name '*.yedek-*' -o -name '*.bak' -o -name 'settings.local.json.*' \) \
    -print)
  if [ -n "$inside" ]; then
    while IFS= read -r path; do
      [ -n "$path" ] || continue
      git -C "$CASE_VAULT" check-ignore -q -- "${path#"$CASE_VAULT"/}" || {
        diag "vault içindeki yedek ignore edilmiyor: $path"
        return 1
      }
    done <<EOF
$inside
EOF
  fi
}

test_no_secret_in_tracked_files() {
  new_case
  full_chain || return 1
  if git -C "$CASE_VAULT" grep -n -E 'm0-GIZLI-ANAHTAR|sk-GIZLI' -- . \
      >"$CASE_ROOT/git-grep.out" 2>&1; then
    diag "izlenen dosyada sır bulundu"
    sed 's/^/# /' "$CASE_ROOT/git-grep.out" >&2 || :
    return 1
  fi
}

test_clean_local_needs_no_confirmation() {
  new_case --clean-local
  run_stage "$CASE_ROOT/apply.out" apply
  [ "$RUN_RC" -eq 0 ] || {
    show_stage_failure apply "$CASE_ROOT/apply.out"
    return 1
  }
  python3 - "$CASE_VAULT/.claude/settings.local.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
assert data["hooks"]["Notification"] == [{
    "matcher": "*",
    "hooks": [{
        "type": "command",
        "command": "/usr/local/bin/bildirim-sesi.sh",
        "timeout": 5,
    }],
}]
PY
}

test_bad_local_fails_cleanly_or_applies_cleanly() {
  new_case --bad-local
  local before after
  before=$(tree_digest "$CASE_VAULT")
  run_stage "$CASE_ROOT/apply.out" apply

  [ ! -e "$CASE_VAULT/.beyin-version" ] || {
    diag "bad-local apply sürüm damgası yazdı"
    return 1
  }
  if grep -Eq 'Traceback \(most recent call last\)|AttributeError|unbound variable' "$CASE_ROOT/apply.out"; then
    diag "Python exception veya ham shell hatası kullanıcıya sızdı"
    return 1
  fi

  if [ "$RUN_RC" -eq 0 ]; then
    [ -f "$CASE_VAULT/.claude/scripts/.state/upgrade-stage" ] || {
      diag "başarılı görünen bad-local apply tamamlanma işareti bırakmadı"
      return 1
    }
    [ -s "$CASE_VAULT/.claude/settings.json" ] \
      && [ -x "$CASE_VAULT/.claude/hooks/pre-compact.sh" ] || {
        diag "başarılı görünen bad-local apply yarım kaldı"
        return 1
      }
  else
    after=$(tree_digest "$CASE_VAULT")
    [ "$before" = "$after" ] || {
      diag "hatalı bad-local apply vault'u yarım değiştirdi"
      return 1
    }
    grep -Eqi 'settings\.local\.json' "$CASE_ROOT/apply.out" \
      && grep -Eqi 'HATA|JSON|nesne' "$CASE_ROOT/apply.out" || {
      diag "bad-local hatası anlaşılır bir kullanıcı mesajı vermedi"
      return 1
    }
  fi
}

test_missing_local_settings_completes() {
  new_case
  git -C "$CASE_VAULT" rm -q -- .claude/settings.local.json
  git -C "$CASE_VAULT" -c commit.gpgsign=false commit -q -m "settings.local yok"
  full_chain || return 1
  [ "$(sed -n '1p' "$CASE_VAULT/.beyin-version")" = "2.3.0" ]
}

test_gitignore_rule_is_effective() {
  new_case
  full_chain || return 1
  grep -qxF '.claude/settings.local.json' "$CASE_VAULT/.gitignore" || {
    diag ".gitignore içinde settings.local.json kuralı yok"
    return 1
  }
  git -C "$CASE_VAULT" check-ignore -q --no-index -- .claude/settings.local.json || {
    diag "git check-ignore settings.local.json kuralını doğrulamadı"
    return 1
  }
}

run_test() {
  local name="$1" function_name="$2"
  if "$function_name"; then
    pass "$name"
  else
    fail "$name"
  fi
  cleanup_case
}

printf '1..12\n'
run_test "onaysız apply 11 ile duruyor ve hiçbir mutasyon yapmıyor" test_confirmation_is_non_mutating
run_test "ilgisiz Notification kancası birebir korunuyor" test_unrelated_hook_preserved
run_test "env.MEM0_API_KEY ve permissions birebir korunuyor" test_env_and_permissions_preserved
run_test "v1 beyin kancası settings.local.json içinden kaldırılıyor" test_v1_local_hook_removed
run_test "iki settings dosyasında olay başına tam bir beyin kancası kalıyor" test_exactly_one_hook_per_event
run_test "sır taşıyabilecek dosyalar son commit veya indexe girmiyor" test_secret_files_not_staged_or_committed
run_test "sır içeren yedek vault dışında ve 0600 izinli" test_backup_outside_vault_and_mode_0600
run_test "git tarafından izlenen dosyalarda fixture sırları bulunmuyor" test_no_secret_in_tracked_files
run_test "temiz local settings onaysız apply edilir ve ilgisiz kanca korunur" test_clean_local_needs_no_confirmation
run_test "nesne olmayan local settings açık ve atomik biçimde ele alınıyor" test_bad_local_fails_cleanly_or_applies_cleanly
run_test "settings.local.json yokken tam zincir tamamlanıyor" test_missing_local_settings_completes
run_test ".gitignore kuralı var ve git check-ignore ile etkili" test_gitignore_rule_is_effective

if [ "$FAIL_COUNT" -ne 0 ]; then
  diag "$FAIL_COUNT / $TEST_COUNT test başarısız"
  exit 1
fi
exit 0
