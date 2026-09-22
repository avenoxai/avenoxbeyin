#!/usr/bin/env bash
# v1 -> v2 yükseltmesinin işlem sınırları ve sürüm damgası regresyon testleri.
set -euo pipefail

# Force UTF-8 mode for every python3 subprocess this test spawns. Without it,
# on a non-English Windows install (e.g. Turkish cp1254), Python's stdin/
# stdout default to the OS locale codepage instead of UTF-8 and corrupt (or
# crash on) the Turkish text these tests write and compare.
export PYTHONUTF8=1

TEST_ROOT=$(CDPATH= cd "$(dirname "$0")/.." 2>/dev/null && pwd)
UPGRADE="$TEST_ROOT/scripts/upgrade.sh"
FIXTURE="$TEST_ROOT/tests/fixtures/v1_vault.sh"
BASH_BIN=$(command -v bash)
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/beyin-upgrade.XXXXXX")
trap 'chmod -R u+w "$TEST_TMP" 2>/dev/null || :; rm -rf "$TEST_TMP"' EXIT HUP INT TERM

# shellcheck source=fixtures/v1_vault.sh
source "$FIXTURE"

TEST_COUNT=0
FAIL_COUNT=0
RUN_STATUS=0

run_case() {
  local description="$1"
  local function_name="$2"
  TEST_COUNT=$((TEST_COUNT + 1))
  if "$function_name"; then
    printf 'ok %s - %s\n' "$TEST_COUNT" "$description"
  else
    printf 'not ok %s - %s\n' "$TEST_COUNT" "$description"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

diag() {
  printf '# %s\n' "$*" >&2
}

assert_eq() {
  local expected="$1"
  local actual="$2"
  local message="$3"
  if [ "$expected" != "$actual" ]; then
    diag "$message (beklenen: '$expected', bulunan: '$actual')"
    return 1
  fi
}

assert_file() {
  [ -f "$1" ] || { diag "dosya yok: $1"; return 1; }
}

assert_no_file() {
  [ ! -e "$1" ] || { diag "dosya oluşmamalıydı: $1"; return 1; }
}

assert_dir() {
  [ -d "$1" ] || { diag "klasör yok: $1"; return 1; }
}

new_case() {
  mktemp -d "$TEST_TMP/vaka.XXXXXX"
}

# İçerik, dosya türü ve izin bitlerini özetler; .git ağacını bilerek dışarıda bırakır.
tree_digest() {
  python3 - "$1" <<'PY'
import hashlib
import os
import stat
import sys

root = os.path.abspath(sys.argv[1])
h = hashlib.sha256()

def add(value):
    if isinstance(value, str):
        value = value.encode("utf-8", "surrogateescape")
    h.update(value)
    h.update(b"\0")

for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
    dirs[:] = sorted(name for name in dirs if name != ".git")
    files.sort()
    rel_current = os.path.relpath(current, root)
    if rel_current == ".":
        rel_current = ""
    for name in dirs + files:
        path = os.path.join(current, name)
        rel = os.path.join(rel_current, name)
        info = os.lstat(path)
        add(rel)
        add(oct(stat.S_IMODE(info.st_mode)))
        add(stat.S_IFMT(info.st_mode).to_bytes(8, "big"))
        if stat.S_ISLNK(info.st_mode):
            add(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    h.update(chunk)
            h.update(b"\0")

print(h.hexdigest())
PY
}

file_digest() {
  python3 - "$1" <<'PY'
import hashlib
import sys

with open(sys.argv[1], "rb") as handle:
    print(hashlib.sha256(handle.read()).hexdigest())
PY
}

# / ağacını dolaşmadan kökün girişlerini ve tarihsel kaçak hedefleri özetler.
root_guard_digest() {
  python3 - <<'PY'
import hashlib
import os
import stat

h = hashlib.sha256()

def add(value):
    if isinstance(value, str):
        value = value.encode("utf-8", "surrogateescape")
    h.update(value)
    h.update(b"\0")

for name in sorted(os.listdir("/")):
    path = os.path.join("/", name)
    info = os.lstat(path)
    add(name)
    add(oct(stat.S_IMODE(info.st_mode)))
    add(stat.S_IFMT(info.st_mode).to_bytes(8, "big"))
    if stat.S_ISLNK(info.st_mode):
        add(os.readlink(path))

for path in ("/daily", "/.beyin-version", "/template"):
    add(path)
    if not os.path.lexists(path):
        add("YOK")
        continue
    info = os.lstat(path)
    add(oct(stat.S_IMODE(info.st_mode)))
    if stat.S_ISREG(info.st_mode):
        with open(path, "rb") as handle:
            h.update(handle.read())
    elif stat.S_ISDIR(info.st_mode):
        for current, dirs, files in os.walk(path, topdown=True, followlinks=False):
            dirs.sort()
            files.sort()
            for name in dirs + files:
                child = os.path.join(current, name)
                child_info = os.lstat(child)
                add(os.path.relpath(child, path))
                add(oct(stat.S_IMODE(child_info.st_mode)))
                if stat.S_ISREG(child_info.st_mode):
                    with open(child, "rb") as handle:
                        h.update(handle.read())

print(h.hexdigest())
PY
}

prepare_case_dirs() {
  mkdir -p "$1/home" "$1/backup" "$1/tmp"
}

run_upgrade() {
  local case_dir="$1"
  local output="$2"
  shift 2
  prepare_case_dirs "$case_dir"
  set +e
  env HOME="$case_dir/home" \
      TMPDIR="$case_dir/tmp" \
      BEYIN_BACKUP_ROOT="$case_dir/backup" \
      "$BASH_BIN" "$UPGRADE" "$@" >"$output" 2>&1
  RUN_STATUS=$?
  set -e
}

# Ortamı sıfırlayarak yalnız güvenlik için gereken geçici HOME/yedek/TMP ve PATH'i verir.
run_upgrade_fresh() {
  local case_dir="$1"
  local output="$2"
  shift 2
  prepare_case_dirs "$case_dir"
  set +e
  env -i PATH="$PATH" \
      HOME="$case_dir/home" \
      TMPDIR="$case_dir/tmp" \
      BEYIN_BACKUP_ROOT="$case_dir/backup" \
      "$BASH_BIN" "$UPGRADE" "$@" >"$output" 2>&1
  RUN_STATUS=$?
  set -e
}

prepare_finalizable_vault() {
  mkdir -p "$1/🔮 850-Companion"
  printf '# Kurallar\n\nKullanıcının gerçek kuralları.\n' > "$1/🔮 850-Companion/Kurallar.md"
}

test_missing_vault() {
  local case_dir before after
  case_dir=$(new_case)
  prepare_case_dirs "$case_dir"
  before=$(tree_digest "$case_dir/backup")
  run_upgrade "$case_dir" "$case_dir/output" --stage apply
  assert_eq 2 "$RUN_STATUS" "--vault eksikken kullanım çıkışı" || return 1
  after=$(tree_digest "$case_dir/backup")
  assert_eq "$before" "$after" "eksik --vault çağrısı yazma yaptı" || return 1
}

test_relative_vault() {
  local case_dir
  case_dir=$(new_case)
  run_upgrade "$case_dir" "$case_dir/output" --vault ./v --stage apply
  assert_eq 2 "$RUN_STATUS" "göreli vault kullanım çıkışı" || return 1
  [ -z "$(find "$case_dir/backup" -mindepth 1 -print -quit)" ] \
    || { diag "göreli vault çağrısı yedek alanına yazdı"; return 1; }
}

test_root_vault() {
  local case_dir before after
  case_dir=$(new_case)
  before=$(root_guard_digest)
  run_upgrade "$case_dir" "$case_dir/output" --vault / --stage apply
  [ "$RUN_STATUS" -ne 0 ] || { diag "kök dizin vault olarak kabul edildi"; return 1; }
  after=$(root_guard_digest)
  assert_eq "$before" "$after" "kök dizinde mutasyon saptandı" || return 1
}

test_repo_vault() {
  local case_dir before after status_before status_after head_before head_after
  case_dir=$(new_case)
  before=$(tree_digest "$TEST_ROOT")
  status_before=$(git -C "$TEST_ROOT" status --short --untracked-files=all)
  head_before=$(git -C "$TEST_ROOT" rev-parse HEAD)
  run_upgrade "$case_dir" "$case_dir/output" --vault "$TEST_ROOT" --stage apply
  [ "$RUN_STATUS" -ne 0 ] || { diag "repo vault olarak kabul edildi"; return 1; }
  after=$(tree_digest "$TEST_ROOT")
  status_after=$(git -C "$TEST_ROOT" status --short --untracked-files=all)
  head_after=$(git -C "$TEST_ROOT" rev-parse HEAD)
  assert_eq "$before" "$after" "repo dosya ağacı değişti" || return 1
  assert_eq "$status_before" "$status_after" "repo git durumu değişti" || return 1
  assert_eq "$head_before" "$head_after" "repo HEAD değişti" || return 1
}

test_unmarked_empty_vault() {
  local case_dir vault before after
  case_dir=$(new_case)
  vault="$case_dir/vault"
  mkdir -p "$vault"
  before=$(tree_digest "$vault")
  run_upgrade "$case_dir" "$case_dir/output" --vault "$vault" --stage apply
  [ "$RUN_STATUS" -ne 0 ] || { diag "işaretsiz boş dizin vault olarak kabul edildi"; return 1; }
  after=$(tree_digest "$vault")
  assert_eq "$before" "$after" "işaretsiz dizin reddedilirken değişti" || return 1
}

test_check_read_only() {
  local case_dir vault before after
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault"
  before=$(tree_digest "$vault")
  run_upgrade "$case_dir" "$case_dir/output" --vault "$vault" --stage check
  assert_eq 0 "$RUN_STATUS" "check başarısız" || return 1
  after=$(tree_digest "$vault")
  assert_eq "$before" "$after" "check içerik veya izin değiştirdi" || return 1
}

test_stamp_only_finalize() {
  local case_dir vault stamp
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault"
  prepare_finalizable_vault "$vault"
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply --confirm-local-hooks
  assert_eq 0 "$RUN_STATUS" "apply başarısız" || return 1
  assert_no_file "$vault/.beyin-version" || return 1
  run_upgrade "$case_dir" "$case_dir/finalize.out" --vault "$vault" --stage finalize
  assert_eq 0 "$RUN_STATUS" "finalize başarısız" || return 1
  assert_file "$vault/.beyin-version" || return 1
  stamp=$(sed -n '1p' "$vault/.beyin-version")
  assert_eq 2.3.0 "$stamp" "sürüm damgası yanlış" || return 1
}

test_fresh_shell_chain() {
  local case_dir vault hook script
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --clean-local
  prepare_finalizable_vault "$vault"

  run_upgrade_fresh "$case_dir" "$case_dir/check.out" --vault "$vault" --stage check
  assert_eq 0 "$RUN_STATUS" "taze shell check başarısız" || return 1
  run_upgrade_fresh "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 0 "$RUN_STATUS" "taze shell apply başarısız" || return 1
  run_upgrade_fresh "$case_dir" "$case_dir/finalize.out" --vault "$vault" --stage finalize
  assert_eq 0 "$RUN_STATUS" "taze shell finalize başarısız" || return 1

  for hook in lib.sh session-start.sh prompt-counter.sh session-end.sh pre-compact.sh; do
    assert_file "$vault/.claude/hooks/$hook" || return 1
    [ -x "$vault/.claude/hooks/$hook" ] \
      || { diag "v2 kancası çalıştırılabilir değil: $hook"; return 1; }
    cmp -s "$TEST_ROOT/template/.claude/hooks/$hook" "$vault/.claude/hooks/$hook" \
      || { diag "v2 kancası kaynakla aynı değil: $hook"; return 1; }
  done
  for script in flush.py compile.py _portalock.py render_codex_hooks.py render_antigravity_hooks.py antigravity_hooks.py graf_kontrol.py; do
    assert_file "$vault/.claude/scripts/$script" || return 1
    cmp -s "$TEST_ROOT/template/.claude/scripts/$script" "$vault/.claude/scripts/$script" \
      || { diag "v2 scripti kaynakla aynı değil: $script"; return 1; }
  done
  [ -L "$vault/AGENTS.md" ] && [ "$(readlink "$vault/AGENTS.md")" = "CLAUDE.md" ] \
    || { diag "AGENTS.md ortak router değil"; return 1; }
  [ -L "$vault/.agents/skills" ] && [ "$(readlink "$vault/.agents/skills")" = "../.claude/skills" ] \
    || { diag ".agents/skills ortak store değil"; return 1; }
  [ -L "$vault/.codex/hooks" ] && [ "$(readlink "$vault/.codex/hooks")" = "../.claude/hooks" ] \
    || { diag ".codex/hooks ortak store değil"; return 1; }
  python3 -m json.tool "$vault/.codex/hooks.json" >/dev/null \
    || { diag ".codex/hooks.json geçerli değil"; return 1; }
  python3 -m json.tool "$vault/.agents/hooks.json" >/dev/null \
    || { diag ".agents/hooks.json geçerli değil"; return 1; }
  assert_eq 2.3.0 "$(sed -n '1p' "$vault/.beyin-version")" "taze shell zinciri damgası" || return 1
}

test_apply_failure_no_stamp() {
  local case_dir vault probe_dir
  case_dir=$(new_case)
  vault="$case_dir/vault"

  # NTFS (via Git Bash chmod) does not reliably enforce directory-level
  # write denial the way POSIX does: `chmod a-w` on a directory is often
  # cosmetic there, so a write into it can still succeed. Probe that
  # directly before relying on it to make apply fail; if this platform
  # can't produce a real unwritable directory, this specific failure mode
  # can't be exercised here even though scripts/upgrade.sh's own error
  # handling for a failed copy is unchanged and still real.
  probe_dir="$case_dir/write-probe"
  mkdir -p "$probe_dir"
  chmod a-w "$probe_dir"
  if : > "$probe_dir/canary" 2>/dev/null; then
    chmod u+w "$probe_dir"
    diag "bu dosya sisteminde chmod a-w bir dizini gerçekten yazılamaz yapmıyor (Windows/NTFS), bu vaka atlanıyor"
    return 0
  fi
  chmod u+w "$probe_dir"

  make_v1_vault "$vault" --clean-local
  chmod a-w "$vault/.claude/hooks"
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  chmod u+w "$vault/.claude/hooks"
  [ "$RUN_STATUS" -ne 0 ] || { diag "yazılamaz kanca hedefinde apply başarılı göründü"; return 1; }
  assert_no_file "$vault/.beyin-version" || return 1
}

test_finalize_failure_no_stamp() {
  local case_dir vault
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --clean-local
  prepare_finalizable_vault "$vault"
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 0 "$RUN_STATUS" "finalize hata enjeksiyonu için apply başarısız" || return 1
  rm -f "$vault/.claude/hooks/lib.sh"
  run_upgrade "$case_dir" "$case_dir/finalize.out" --vault "$vault" --stage finalize
  [ "$RUN_STATUS" -ne 0 ] || { diag "eksik lib.sh ile finalize başarılı göründü"; return 1; }
  assert_no_file "$vault/.beyin-version" || return 1
}

test_already_current() {
  local case_dir vault before after
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --clean-local
  printf '2.3.0\n' > "$vault/.beyin-version"
  before=$(tree_digest "$vault")
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 3 "$RUN_STATUS" "zaten v2.3 vault apply çıkışı" || return 1
  after=$(tree_digest "$vault")
  assert_eq "$before" "$after" "zaten v2.3 vault apply ile değişti" || return 1
}

test_v2_0_is_upgradeable() {
  local case_dir vault
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --clean-local
  printf '2.0.0\n' > "$vault/.beyin-version"
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 0 "$RUN_STATUS" "v2.0 -> v2.3 apply başarısız" || return 1
  assert_file "$vault/.codex/hooks.json" || return 1
  assert_no_file "$vault/.beyin-version.tmp" || return 1
}

test_v2_1_is_upgradeable() {
  local case_dir vault
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --clean-local
  prepare_finalizable_vault "$vault"
  printf '2.1.0\n' > "$vault/.beyin-version"
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 0 "$RUN_STATUS" "v2.1 -> v2.3 apply başarısız" || return 1
  run_upgrade "$case_dir" "$case_dir/finalize.out" --vault "$vault" --stage finalize
  assert_eq 0 "$RUN_STATUS" "v2.1 -> v2.3 finalize başarısız" || return 1
  assert_eq 2.3.0 "$(sed -n '1p' "$vault/.beyin-version")" "v2.1 yükseltme damgası" || return 1
  assert_file "$vault/.agents/hooks.json" || return 1
  assert_file "$vault/.claude/scripts/antigravity_hooks.py" || return 1
}

test_v2_2_is_upgradeable() {
  local case_dir vault
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --clean-local
  prepare_finalizable_vault "$vault"
  printf '2.2.0\n' > "$vault/.beyin-version"
  rm -f "$vault/.claude/scripts/graf_kontrol.py"
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 0 "$RUN_STATUS" "v2.2 -> v2.3 apply başarısız" || return 1
  assert_file "$vault/.claude/scripts/graf_kontrol.py" || return 1
  run_upgrade "$case_dir" "$case_dir/finalize.out" --vault "$vault" --stage finalize
  assert_eq 0 "$RUN_STATUS" "v2.2 -> v2.3 finalize başarısız" || return 1
  assert_eq 2.3.0 "$(sed -n '1p' "$vault/.beyin-version")" "v2.2 yükseltme damgası" || return 1
}

test_no_git_verified_snapshot() {
  # Sözleşme: git deposu olmayan bir vault'ta apply, ÜSTÜNE YAZMADAN ÖNCE doğrulanmış bir
  # anlık görüntü bırakmak zorunda. Kabul edilen iki yol var: (a) vault'ta git deposu kurup
  # snapshot commit'i atmak, (b) BEYIN_BACKUP_ROOT altına doğrulanmış harici kopya almak.
  # Hangi dalın seçildiği scriptin kararı; olmaması gereken şey İKİSİNİN DE olmaması.
  local case_dir vault snapshot_ok backup_dirs
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --no-git --clean-local

  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 0 "$RUN_STATUS" "--no-git vault apply başarısız" || return 1

  snapshot_ok=0
  if [ -d "$vault/.git" ] && git -C "$vault" rev-parse HEAD >/dev/null 2>&1; then
    snapshot_ok=1
  fi
  backup_dirs=$(find "$case_dir/backup" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
  [ "$backup_dirs" = "0" ] || snapshot_ok=1

  if [ "$snapshot_ok" != "1" ]; then
    diag "git deposu olmayan vault'ta ne snapshot commit'i ne de harici yedek oluştu"
    return 1
  fi
}

test_no_git_binary_uses_external_backup() {
  # git ikilisi hiç yokken harici kopya dalı devreye girmek ZORUNDA. Git kurulu her makinede
  # bu dal ölü kod olduğu için bugüne kadar hiç çalışmamıştı; burada PATH'ten git'i çıkarıp
  # gerçekten koşturuyoruz.
  local case_dir vault fake_path source_count dirs_file dir_count backup_dir backup_count
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --no-git --clean-local
  source_count=$(find "$vault" -mindepth 1 | wc -l | tr -d ' ')

  # git DIŞINDA her şeyin bulunduğu bir PATH kur.
  fake_path="$case_dir/bin"
  mkdir -p "$fake_path"
  local tool tool_path
  for tool in bash sh python3 cp mv rm ln find sed awk grep printf date mkdir chmod wc tr head tail sort stat basename dirname cat mktemp; do
    tool_path=$(command -v "$tool" 2>/dev/null) || continue
    # A couple of tools (notably a Microsoft Store "app execution alias"
    # python3, which is what a fresh Windows install resolves python3 to)
    # live behind a reparse point that ln -s is not allowed to target;
    # fall back to a real copy so this test's PATH still has a working
    # python3, not just a working "not git".
    ln -sf "$tool_path" "$fake_path/$tool" 2>/dev/null \
      || cp "$tool_path" "$fake_path/$tool" 2>/dev/null || :
  done
  # On MSYS2/Git-Bash (Windows), each of the coreutils above is a small PE
  # binary dynamically linked against msys-2.0.dll and friends. Windows
  # resolves those DLLs relative to the launching executable's own
  # directory, not the symlink target's directory, so a PATH containing only
  # the tool symlinks makes every one of them fail to load ("error while
  # loading shared libraries"). Bring the runtime DLLs along too. This is a
  # no-op (glob matches nothing) on real POSIX systems.
  for dll in "$(dirname "$BASH_BIN")"/*.dll; do
    [ -e "$dll" ] || continue
    ln -sf "$dll" "$fake_path/$(basename "$dll")" 2>/dev/null || :
  done
  if PATH="$fake_path" command -v git >/dev/null 2>&1; then
    diag "sahte PATH hâlâ git görüyor, vaka kurulamadı"
    return 1
  fi
  # A Windows PE binary fully isolated from the system's normal search
  # locations (System32, WindowsApps) can fail to load even with its own
  # DLLs alongside it: some depend on Universal-CRT "API set" forwarder
  # DLLs that Windows normally resolves virtually, which MSYS2's exec path
  # does not replicate for a bare, trimmed PATH. If this platform can't run
  # even a trivial python3 call under the isolated PATH this test builds,
  # the "no git" scenario can't be exercised here either.
  if ! PATH="$fake_path" "$fake_path/python3" -c "1" >/dev/null 2>&1; then
    diag "bu ortamda izole PATH içinde python3 çalıştırılamıyor (Windows/MSYS2 CRT sınırı), bu vaka atlanıyor"
    return 0
  fi

  prepare_case_dirs "$case_dir"
  set +e
  env -i PATH="$fake_path" \
      HOME="$case_dir/home" \
      TMPDIR="$case_dir/tmp" \
      BEYIN_BACKUP_ROOT="$case_dir/backup" \
      "$BASH_BIN" "$UPGRADE" --vault "$vault" --stage apply >"$case_dir/apply.out" 2>&1
  RUN_STATUS=$?
  set -e
  assert_eq 0 "$RUN_STATUS" "git'siz ortamda apply başarısız" || {
    sed 's/^/# /' "$case_dir/apply.out" >&2 || :
    return 1
  }

  dirs_file="$case_dir/backup-dirs"
  find "$case_dir/backup" -mindepth 1 -maxdepth 1 -type d -print > "$dirs_file"
  dir_count=$(wc -l < "$dirs_file" | tr -d ' ')
  assert_eq 1 "$dir_count" "git yokken harici doğrulanmış yedek sayısı" || return 1
  backup_dir=$(sed -n '1p' "$dirs_file")
  assert_dir "$backup_dir" || return 1
  assert_file "$backup_dir/CLAUDE.md" || return 1
  backup_count=$(find "$backup_dir" -mindepth 1 | wc -l | tr -d ' ')
  [ "$backup_count" -ge "$source_count" ] || {
    diag "yedek eksik kopyalanmış (kaynak: $source_count, yedek: $backup_count)"
    return 1
  }
  assert_no_file "$vault/.beyin-version" || return 1
}

test_rename_confirmation_is_atomic() {
  local case_dir vault before after
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --memory-dir "🔮 850-Echo" --clean-local
  before=$(tree_digest "$vault")
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 10 "$RUN_STATUS" "yeniden adlandırma onayı çıkışı" || return 1
  after=$(tree_digest "$vault")
  assert_eq "$before" "$after" "onaysız yeniden adlandırma vakası mutasyon yaptı" || return 1
}

test_rename_preserves_core() {
  local case_dir vault before
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --memory-dir "🔮 850-Echo" --clean-local
  before=$(file_digest "$vault/🔮 850-Echo/Core.md")
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply --confirm-rename
  assert_eq 0 "$RUN_STATUS" "onaylı yeniden adlandırma apply başarısız" || return 1
  assert_no_file "$vault/🔮 850-Echo/Core.md" || return 1
  assert_file "$vault/🔮 850-Companion/Core.md" || return 1
  assert_eq "$before" "$(file_digest "$vault/🔮 850-Companion/Core.md")" \
    "Core.md içeriği yeniden adlandırmada değişti" || return 1
}

test_user_hook_preserved() {
  local case_dir vault before
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --clean-local
  before=$(file_digest "$vault/.claude/hooks/kullanici-kendi.sh")
  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 0 "$RUN_STATUS" "kullanıcı kancası koruma apply başarısız" || return 1
  assert_file "$vault/.claude/hooks/kullanici-kendi.sh" || return 1
  assert_eq "$before" "$(file_digest "$vault/.claude/hooks/kullanici-kendi.sh")" \
    "kullanıcının kendi kancası değişti" || return 1
}

bash -n "$UPGRADE"
bash -n "$FIXTURE"
bash -n "$0"

test_untracks_tracked_pycache() {
  # A vault upgraded before .gitignore knew about __pycache__ carries compiled bytecode in the
  # index. Adding the ignore rule alone does not fix that, so the upgrade must also untrack it,
  # and it must leave the files on disk: python regenerates them, they are not the user's data.
  local case_dir vault pyc rel still_tracked
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --clean-local
  prepare_finalizable_vault "$vault"

  rel=".claude/scripts/__pycache__/flush.cpython-312.pyc"
  pyc="$vault/$rel"
  mkdir -p "$(dirname "$pyc")"
  printf 'not real bytecode, only a tracked placeholder\n' > "$pyc"
  git -C "$vault" add -f -- "$rel" >/dev/null 2>&1 || { diag "fikstür: .pyc eklenemedi"; return 1; }
  git -C "$vault" -c user.name=fixture -c user.email=fixture@example.invalid \
    commit -q -m "v1: derlenmis bytecode izleniyor" || { diag "fikstür: commit basarisiz"; return 1; }
  git -C "$vault" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1 \
    || { diag "fikstür kurulamadi: .pyc izlenmiyor"; return 1; }

  run_upgrade "$case_dir" "$case_dir/apply.out" --vault "$vault" --stage apply
  assert_eq 0 "$RUN_STATUS" "apply basarisiz" || { diag "$(cat "$case_dir/apply.out")"; return 1; }
  run_upgrade "$case_dir" "$case_dir/finalize.out" --vault "$vault" --stage finalize
  assert_eq 0 "$RUN_STATUS" "finalize basarisiz" || { diag "$(cat "$case_dir/finalize.out")"; return 1; }

  assert_file "$pyc" || { diag ".pyc diskten silinmis olmamali"; return 1; }

  still_tracked=$(git -C "$vault" ls-files -- '*.pyc' '*/__pycache__/*' | tr '\n' ' ')
  if [ -n "$still_tracked" ]; then
    diag "yukseltmeden sonra hala izlenen bytecode var: $still_tracked"
    return 1
  fi
  grep -qxF '__pycache__/' "$vault/.gitignore" || { diag ".gitignore __pycache__/ icermiyor"; return 1; }
  grep -qxF '*.pyc' "$vault/.gitignore" || { diag ".gitignore *.pyc icermiyor"; return 1; }
}

test_pycache_untrack_failure_is_fatal() {
  local case_dir vault pyc rel fake_path real_git
  case_dir=$(new_case)
  vault="$case_dir/vault"
  make_v1_vault "$vault" --clean-local
  prepare_finalizable_vault "$vault"

  rel=".claude/scripts/__pycache__/flush.cpython-312.pyc"
  pyc="$vault/$rel"
  mkdir -p "$(dirname "$pyc")"
  printf 'not real bytecode, only a tracked placeholder\n' > "$pyc"
  git -C "$vault" add -f -- "$rel" >/dev/null 2>&1 || return 1
  git -C "$vault" -c user.name=fixture -c user.email=fixture@example.invalid \
    commit -q -m "v1: derlenmis bytecode izleniyor" || return 1

  real_git=$(command -v git)
  fake_path="$case_dir/bin"
  mkdir -p "$fake_path"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'if [ "${1:-}" = "-C" ] && [ "${3:-}" = "rm" ]; then exit 42; fi' \
    'exec "$REAL_GIT" "$@"' > "$fake_path/git"
  chmod +x "$fake_path/git"

  prepare_case_dirs "$case_dir"
  set +e
  env HOME="$case_dir/home" \
      TMPDIR="$case_dir/tmp" \
      BEYIN_BACKUP_ROOT="$case_dir/backup" \
      REAL_GIT="$real_git" \
      PATH="$fake_path:$PATH" \
      "$BASH_BIN" "$UPGRADE" --vault "$vault" --stage apply >"$case_dir/apply.out" 2>&1
  RUN_STATUS=$?
  set -e

  [ "$RUN_STATUS" -ne 0 ] || { diag "git rm hatasi yutuldu"; return 1; }
  grep -q "izlemeden çıkarılamadı" "$case_dir/apply.out" \
    || { diag "git rm hatasi acik raporlanmadi"; return 1; }
  git -C "$vault" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1 \
    || { diag "basarisiz git rm vakasinda index beklenmedik bicimde degisti"; return 1; }
  assert_file "$pyc" || return 1
}

run_case "--vault olmadan apply kullanım hatası verir ve yazmaz" test_missing_vault
run_case "göreli --vault kullanım hatasıyla reddedilir" test_relative_vault
run_case "kök dizin vault olarak reddedilir ve değişmez" test_root_vault
run_case "repo vault olarak reddedilir ve repo değişmez" test_repo_vault
run_case "v1 işareti olmayan boş dizin reddedilir" test_unmarked_empty_vault
run_case "check ağacı içerik ve izin düzeyinde değiştirmez" test_check_read_only
run_case "sürüm damgasını apply değil yalnız finalize yazar" test_stamp_only_finalize
run_case "check apply finalize ayrı taze shell süreçlerinde tamamlanır" test_fresh_shell_chain
run_case "apply kopyalama hatasında başarısız olur ve damga yazmaz" test_apply_failure_no_stamp
run_case "finalize kapısı bozulunca başarısız olur ve damga yazmaz" test_finalize_failure_no_stamp
run_case "zaten 2.3.0 damgalı vault apply için 3 döndürür" test_already_current
run_case "2.0.0 damgalı vault v2.3'e yükseltilebilir" test_v2_0_is_upgradeable
run_case "2.1.0 damgalı vault Antigravity destekli v2.3'e yükseltilebilir" test_v2_1_is_upgradeable
run_case "2.2.0 damgalı vault graf tarayıcılı v2.3'e yükseltilebilir" test_v2_2_is_upgradeable
run_case "git olmayan vault doğrulanmış anlık görüntü bırakmadan ilerlemiyor" test_no_git_verified_snapshot
run_case "git ikilisi yokken harici doğrulanmış yedek dalı gerçekten çalışıyor" test_no_git_binary_uses_external_backup
run_case "yeniden adlandırma onayı yoksa apply atomik olarak 10 döndürür" test_rename_confirmation_is_atomic
run_case "yeniden adlandırma Core.md içeriğini aynen korur" test_rename_preserves_core
run_case "kullanıcının kendi kancası yükseltmede aynen korunur" test_user_hook_preserved
run_case "izlenen __pycache__ artıkları izden çıkarılır ama diskte kalır" test_untracks_tracked_pycache
run_case "__pycache__ izlemeden çıkarılamazsa yükseltme açıkça durur" test_pycache_untrack_failure_is_fatal

printf '1..%s\n' "$TEST_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
