"""DeepSeek Harness (dsh) hook adapter and profile patch helper.

DSH desktop loads Claude Code command hooks through the ``dsh-hooks-claude-code``
plugin: the profile's ``cordis.patch.yml`` points ``configPath`` at a Claude
``hooks.json`` and the plugin runs each command through ``ctx.shell`` — the same
sandboxed executor its own tools use. Two properties of that bridge shape this
module:

* The payload is Claude-shaped and stdout is parsed as a Claude hook result, so
  the shared ``beyin_v3_hook.py`` output passes through unchanged.
* The hook runs with the session's working directory as ``cwd`` but with the
  *deployment* sandbox root and no per-session policy, which is the DSH profile
  directory. Hooks therefore cannot write
  the Beyin state directory until the profile patch names it (see
  ``sandbox_policy_block``). ``in_vault`` closes the other half: one profile
  serves every folder DSH opens, so a hook outside the vault answers ``{}``.

Only the four events DSH bridges are registered: SessionStart, UserPromptSubmit,
PostToolUse and Stop. PreCompact and SessionEnd are not part of the bridge's
event list, so queueing them would be dead configuration.

``dsh-profile apply`` writes two marked entries into the user's profile patch:
the ``sandbox-policy`` override that lets session-less hooks write the state
directory, and the ``hooks-claude-code`` insert that points DSH at the
vault-owned hook file. Both are opt-in, backed up, and skipped when an entry we
do not own already exists.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")
# DSH 0.2.1-rc.3 model-facing tool names (dsh-tool-fs, dsh-tool-str-replace-editor).
# The bridge treats a word-and-pipe matcher as an exact pipe-separated list, so these
# must match ``exec.name`` exactly; Claude's ``Edit|Write`` never selects DSH's ``edit``.
WRITE_TOOLS = ("edit", "write", "str_replace_editor")
HOOK_FILE = ".claude/dsh/beyin-v3-hooks.json"
PROFILE_FILE = "cordis.patch.yml"
RECORD_FILE = "dsh-profile.json"

# The profile patch layer replaces a targeted entry's config wholesale and merges nothing
# (dsh-app-boot), so ``mode`` is written next to ``workspaceRoot`` even though the bundle
# default is already ``workspace-write``. 0.2.1-rc.3 accepts only these two keys; a newer
# ``additionalWritableRoots`` is deliberately not guessed here.
SANDBOX_ID = "sandbox-policy"
SANDBOX_NAME = "@deepseek-ai/dsh-sandbox-policy"
BEGIN = "# beyin-v3:begin sandbox-policy (managed block; remove with beyin.py dsh-profile remove)"
END = "# beyin-v3:end sandbox-policy"
HOOK_ID = "hooks-claude-code"
HOOK_NAME = "@deepseek-ai/dsh-hooks-claude-code"
HOOK_BEGIN = "# beyin-v3:begin hooks-claude-code (managed block; remove with beyin.py dsh-profile remove)"
HOOK_END = "# beyin-v3:end hooks-claude-code"


class ProfilePatchError(ValueError):
    """The profile patch cannot be edited safely; nothing was written."""


def canonical(path):
    """Comparable form of a path: resolved, case-folded on Windows, forward slashes."""
    try:
        text = str(Path(path).expanduser().resolve())
    except (OSError, ValueError, RuntimeError):
        text = str(path or "")
    text = text.replace("\\", "/").rstrip("/")
    return text.lower() if os.name == "nt" else text


def in_vault(vault, cwd):
    """True when the hook runs inside the vault; DSH starts one profile for any folder."""
    root = canonical(vault)
    here = canonical(cwd or os.getcwd())
    return bool(root) and (here == root or here.startswith(root + "/"))


def context(vault, state, event, payload, cwd=None):
    """Return the shared handler's Claude-shaped answer, or ``{}`` outside the vault.

    Every transport problem is a quiet ``{}``: a DSH hook must never block a turn or
    spill runtime text into the transcript. The handler records its own errors in
    ``hook-error.json``, which doctor reads.
    """
    if event not in EVENTS:
        return {}
    try:
        here = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    except OSError:
        return {}
    if not in_vault(vault, here):
        return {}
    payload = dict(payload) if isinstance(payload, dict) else {}
    payload["hook_event_name"] = event
    payload.setdefault("cwd", here)
    hook = Path(__file__).with_name("beyin_v3_hook.py")
    command = [sys.executable, str(hook), "--vault", str(vault), "--state", str(state),
               "--harness", "dsh", "--event", event]
    try:
        result = subprocess.run(command, input=json.dumps(payload), text=True, encoding="utf-8",
                                capture_output=True, timeout=18 if os.name == "nt" else 7)
        if result.returncode:
            return {}
        output = json.loads(result.stdout or "{}")
        return output if isinstance(output, dict) else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def plan_plugin(vault, state, command):
    """Return the vault-owned hook config; ``command`` builds one shell command from argv.

    The installer supplies its cross-platform builder, so the same bytes are planned for
    POSIX shells and for PowerShell's encoded form. Vault, state and interpreter are
    pinned like every other adapter's; the file is installer-owned, so uninstall and
    rollback remove it with the rest of ``.claude/dsh/``.
    """
    resolved_vault = str(Path(vault).resolve())
    resolved_state = str(Path(state).resolve()) if state is not None else ""
    for value in (resolved_vault, resolved_state, sys.executable):
        if any(char in str(value) for char in "\r\n\x00"):
            raise ValueError("Newlines or NUL in hook paths are unsupported")
    adapter = str(Path(vault).resolve() / ".claude/scripts/beyin_v3_dsh.py")
    hooks = {}
    for event in EVENTS:
        posix, windows = command([sys.executable, adapter, "--vault", resolved_vault,
                                  "--state", resolved_state, "--event", event])
        group = {"hooks": [{"type": "command", "command": windows if os.name == "nt" else posix,
                            "timeout": 25 if os.name == "nt" else 10}]}
        if event == "PostToolUse":
            group["matcher"] = "|".join(WRITE_TOOLS)
        hooks[event] = [group]
    return {HOOK_FILE: (json.dumps({"hooks": hooks}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}


def sandbox_policy_block(state):
    """The marked profile entry that points DSH's session-less sandbox at the state root."""
    root = str(Path(state).resolve()).replace("\\", "/")
    lines = [
        BEGIN,
        "# Hooks run without a session, so dsh-sandbox-policy falls back to the deployment",
        "# mode and root. Without this entry that root is the DSH profile folder and every",
        "# Beyin state write is denied. Only ``mode`` and ``workspaceRoot`` exist in",
        "# 0.2.1-rc.3, and a patched entry replaces its config completely.",
        f"- id: {SANDBOX_ID}",
        f'  name: "{SANDBOX_NAME}"',
        "  config:",
        "    mode: workspace-write",
        "    workspaceRoot: " + json.dumps(root, ensure_ascii=False),
        END,
    ]
    return "\n".join(lines) + "\n"


def hook_entry_block(vault):
    """The marked profile insert that loads the vault-owned Claude-shaped hook file."""
    target = str(Path(vault).resolve() / HOOK_FILE).replace("\\", "/")
    lines = [
        HOOK_BEGIN,
        "# DSH has no project-local hook file: this insert points its Claude-hook bridge at",
        "# the managed JSON above. Restart DSH after changing the profile patch.",
        "- insert:",
        f"    - id: {HOOK_ID}",
        f'      name: "{HOOK_NAME}"',
        "      config:",
        "        configPath: " + json.dumps(target, ensure_ascii=False),
        HOOK_END,
    ]
    return "\n".join(lines) + "\n"


def _block_span(lines, begin=BEGIN, end=END, label=SANDBOX_ID):
    """Line indexes ``(begin, end)`` of one marked block, or ``None`` when it is absent."""
    begins = [index for index, line in enumerate(lines) if line.strip() == begin]
    ends = [index for index, line in enumerate(lines) if line.strip() == end]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        raise ProfilePatchError(f"The managed {label} block is damaged; fix the file by hand")
    return begins[0], ends[0]


def _is_sequence(lines):
    """Cheap, deliberately shallow check: a top-level YAML sequence of entries.

    Anything else (flow style ``[...]``, a top-level mapping, a bare ``!!js`` scalar) is
    refused rather than guessed at. Indented lines are content of the preceding entry.
    """
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        if line[0] in " \t":
            continue
        if line == "-" or line.startswith("- "):
            continue
        return False
    return True


def _yaml_scalar(raw):
    """Read one scalar written as JSON, single-quoted YAML, or a plain value."""
    value = raw.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            return value.strip('"')
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value.split(" #", 1)[0].strip()


def _entry_lines(lines, entry_id):
    """Lines of the first top-level entry with this id, or ``None``."""
    pattern = re.compile(rf'^- id:\s*["\']?{re.escape(entry_id)}["\']?\s*$')
    start = None
    for index, line in enumerate(lines):
        if line.startswith("- ") or line == "-":
            if start is not None:
                return lines[start:index]
            if pattern.match(line):
                start = index
    return lines[start:] if start is not None else None


def _field(lines, name):
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(name + ":"):
            return _yaml_scalar(stripped.split(":", 1)[1])
    return ""


def _policy_root(block_lines):
    return canonical(_field(block_lines, "workspaceRoot"))


def _covers(root, state):
    """True when the sandbox root is the state directory or one of its ancestors."""
    root, wanted = canonical(root), canonical(state)
    return bool(root) and bool(wanted) and (wanted == root or wanted.startswith(root + "/"))


def _foreign_entry(lines, entry_id, name):
    """True when an entry we do not own already declares this id or plugin name."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("- id:", "id:")) and _yaml_scalar(stripped.split(":", 1)[1]) == entry_id:
            return True
        if stripped.startswith(("- name:", "name:")) and _yaml_scalar(stripped.split(":", 1)[1]) == name:
            return True
    return False


def read_profile_status(patch, state=None, vault=None):
    """Read-only view of one profile patch: our two entries, and whether they are current."""
    path = Path(patch).expanduser()
    report = {"path": str(path), "exists": path.is_file()}
    if not report["exists"]:
        report["status"] = "missing"
        report["hook_entry"] = {"status": "missing"}
        return report
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        report["status"] = "unreadable"
        report["error"] = type(exc).__name__
        report["hook_entry"] = {"status": "unreadable"}
        return report
    lines = text.splitlines()
    try:
        span = _block_span(lines)
        hook_span = _block_span(lines, HOOK_BEGIN, HOOK_END, HOOK_ID)
    except ProfilePatchError as exc:
        report["status"] = "damaged"
        report["error"] = str(exc)
        report["hook_entry"] = {"status": "damaged", "error": str(exc)}
        return report
    foreign = span is None and _foreign_entry(lines, SANDBOX_ID, SANDBOX_NAME)
    report["foreign_entry"] = foreign
    if span is None:
        report["status"] = "foreign" if foreign else "absent"
        if foreign and state is not None:
            entry = _entry_lines(lines, SANDBOX_ID) or []
            root = _policy_root(entry)
            report["workspaceRoot"] = root
            report["current"] = _covers(root, state)
    else:
        report["status"] = "present"
        report["workspaceRoot"] = _policy_root(lines[span[0]:span[1] + 1])
        if state is not None:
            report["current"] = report["workspaceRoot"] == canonical(state)
            if not report["current"]:
                report["status"] = "outdated"
    hook = {"status": "absent"}
    if hook_span is not None:
        block = lines[hook_span[0]:hook_span[1] + 1]
        hook["status"] = "present"
        hook["configPath"] = _field(block, "configPath")
        if vault is not None:
            hook["current"] = canonical(hook["configPath"]) == canonical(Path(vault) / HOOK_FILE)
            if not hook["current"]:
                hook["status"] = "outdated"
    elif _foreign_entry(lines, HOOK_ID, HOOK_NAME):
        hook["status"] = "foreign"
    report["hook_entry"] = hook
    return report


def _backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(path.name + ".bak-beyin-" + stamp)
    suffix = 1
    while target.exists():
        target = path.with_name(path.name + ".bak-beyin-" + stamp + "-" + str(suffix))
        suffix += 1
    shutil.copy2(path, target)
    return target


def _write(path, text):
    temporary = path.with_name(path.name + ".beyin-tmp")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def apply_profile(patch, state, vault, replace=False):
    """Add both marked profile entries once; never touch an entry we do not own."""
    path = Path(patch).expanduser()
    status = read_profile_status(path, state, vault)
    if status["status"] == "missing":
        return dict(status, applied=False,
                    message="The DSH profile patch does not exist yet; start DSH once, then retry")
    if status["status"] in ("unreadable", "damaged"):
        return dict(status, applied=False)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not _is_sequence(lines):
        return dict(status, applied=False,
                    warning="The profile patch is not a top-level YAML sequence; edit it by hand")
    if status["status"] == "outdated" and not replace:
        return dict(status, applied=False,
                    warning="Our sandbox-policy entry names another workspaceRoot; pass --replace to rewrite it")
    changes, append, warnings = [], [], []
    sandbox_foreign = status["status"] == "foreign"
    if sandbox_foreign:
        warnings.append("An unmanaged sandbox-policy entry already exists; left it unchanged. "
                        "Point its workspaceRoot at the Beyin state directory yourself.")
    elif status["status"] == "outdated":
        changes.append((*_block_span(lines), sandbox_policy_block(state).splitlines()))
    elif status["status"] != "present":
        append.append(sandbox_policy_block(state))
    if status["hook_entry"]["status"] == "present":
        if not status["hook_entry"].get("current", False):
            changes.append((*_block_span(lines, HOOK_BEGIN, HOOK_END, HOOK_ID),
                            hook_entry_block(vault).splitlines()))
    elif status["hook_entry"]["status"] == "foreign":
        warnings.append("An unmanaged hooks-claude-code entry already exists; left it unchanged. "
                        "Point its configPath at " + str(Path(vault) / HOOK_FILE) + " yourself.")
    else:
        append.append(hook_entry_block(vault))
    if not changes and not append:
        result = dict(status, applied=False, unchanged=True)
        if warnings:
            result["warnings"] = warnings
        return result
    backup = _backup(path)
    for begin, end, replacement in sorted(changes, reverse=True):
        lines = lines[:begin] + replacement + lines[end + 1:]
    for block in append:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += [""] + block.splitlines()
    _write(path, "\n".join(lines) + "\n")
    report = dict(read_profile_status(path, state, vault), status="applied", applied=True,
                  replaced=status["status"] == "outdated", backup=str(backup))
    if not sandbox_foreign:
        report["workspaceRoot"] = canonical(state)
    if warnings:
        report["warnings"] = warnings
        report["warning"] = warnings[0]
    return report


def remove_profile(patch):
    """Delete only our marked blocks; a foreign entry stays untouched."""
    path = Path(patch).expanduser()
    status = read_profile_status(path)
    if status["status"] in ("missing", "unreadable", "damaged"):
        return dict(status, removed=False)
    lines = path.read_text(encoding="utf-8").splitlines()
    spans = []
    for begin_marker, end_marker, label in ((BEGIN, END, SANDBOX_ID),
                                            (HOOK_BEGIN, HOOK_END, HOOK_ID)):
        try:
            span = _block_span(lines, begin_marker, end_marker, label)
        except ProfilePatchError as exc:
            return dict(status, removed=False, error=str(exc))
        if span is not None:
            spans.append((span[0], span[1], label))
    if not spans:
        return dict(status, removed=False)
    backup = _backup(path)
    removed = []
    for begin, end, label in sorted(spans, reverse=True):
        while begin > 0 and not lines[begin - 1].strip():
            begin -= 1
        del lines[begin:end + 1]
        removed.append(label)
    while lines and not lines[-1].strip():
        lines.pop()
    _write(path, ("\n".join(lines) + "\n") if lines else "")
    return {"path": str(path), "status": "removed", "removed": True, "backup": str(backup),
            "removed_blocks": sorted(removed)}


def record_path(state):
    return Path(state) / RECORD_FILE


def read_record(state):
    """The profile patch this install owns, if the user pointed one out."""
    path = record_path(state)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("patch") if isinstance(data, dict) else None
    return Path(value) if isinstance(value, str) and Path(value).is_absolute() else None


def save_record(state, patch):
    path = record_path(state)
    document = {"schema": 1, "patch": str(Path(patch).expanduser().resolve())}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".beyin-tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return document


def clear_record(state):
    record_path(state).unlink(missing_ok=True)


def profile_file(path):
    """Accept either ``cordis.patch.yml`` or the profile directory that holds it."""
    candidate = Path(path).expanduser()
    return candidate / PROFILE_FILE if candidate.is_dir() else candidate


def discover_profiles(home=None):
    """Every DSH profile patch under ``~/.dsh/profiles``; used only when no path is given."""
    base = Path(home or Path.home()) / ".dsh" / "profiles"
    try:
        return sorted(path / PROFILE_FILE for path in base.iterdir() if (path / PROFILE_FILE).is_file())
    except OSError:
        return []


def references_hook_config(patch, vault):
    """True when the profile text names our hook config; DSH has no project-local hooks."""
    try:
        text = Path(patch).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return str(Path(vault) / HOOK_FILE) in text or HOOK_FILE in text


def _command_arguments(command):
    """Arguments of one generated hook command, including PowerShell's encoded form."""
    import base64
    import shlex
    match = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", command, re.I)
    if match:
        try:
            script = base64.b64decode(match.group(1), validate=True).decode("utf-16le")
        except (ValueError, UnicodeError):
            return []
        return [item.replace("''", "'") for item in re.findall(r"'((?:[^']|'')*)'", script)]
    try:
        return shlex.split(command)
    except ValueError:
        return []


def doctor(vault, state):
    """Read-only DSH report: our hook config, the recorded profile, and the sandbox root."""
    report = {}
    hook = Path(vault) / HOOK_FILE
    events, adapter_ok = [], False
    if hook.is_file():
        try:
            data = json.loads(hook.read_text(encoding="utf-8"))
            hooks = data.get("hooks") if isinstance(data, dict) else None
            events = sorted(hooks) if isinstance(hooks, dict) else []
            # The adapter pins --harness dsh and the vault for the shared handler; the command
            # itself names the adapter, so that is what a stale or foreign config loses first.
            root, adapter_ok = canonical(vault), set(events) == set(EVENTS)
            for groups in (hooks or {}).values():
                for group in groups if isinstance(groups, list) else ():
                    for handler in group.get("hooks", []) if isinstance(group, dict) else ():
                        args = _command_arguments(str(handler.get("command", "")))
                        paths = [argument.replace("\\", "/") for argument in args]
                        if not any(path.endswith("/beyin_v3_dsh.py") for path in paths):
                            adapter_ok = False
                        elif "--vault" not in args:
                            adapter_ok = False
                        else:
                            index = args.index("--vault")
                            if index + 1 >= len(args) or canonical(args[index + 1]) != root:
                                adapter_ok = False
        except (OSError, ValueError, UnicodeError):
            events, adapter_ok = [], False
    report["hook_config"] = {"path": HOOK_FILE, "present": hook.is_file(), "events": events,
                             "adapter_pinned": adapter_ok if hook.is_file() else None}
    report["state_writable"] = os.access(state, os.W_OK)
    record = read_record(state)
    if record is None:
        report["profile"] = {"status": "not_configured",
                             "hint": "python3 beyin.py dsh-profile apply --profile <profile>/cordis.patch.yml"}
        report["status"] = "not_configured" if hook.is_file() else "not_installed"
        return report
    status = read_profile_status(record, state, vault)
    status["hook_config_referenced"] = references_hook_config(record, vault)
    report["profile"] = status
    warnings = []
    if status["status"] == "foreign":
        warnings.append("The sandbox-policy entry is unmanaged; verify its workspaceRoot covers the state directory.")
    if status["hook_entry"]["status"] == "foreign":
        warnings.append("The hooks-claude-code entry is unmanaged; verify its configPath names the managed hook file.")
    if warnings:
        report["warnings"] = warnings
    if not hook.is_file():
        report["status"] = "hook_config_missing"
    elif status["status"] in ("present", "foreign") and status.get("current", False) and status["hook_config_referenced"]:
        report["status"] = "ok"
    else:
        report["status"] = "needs_attention"
    return report


def main(argv=None):
    """Run one hook event: read the Claude payload on stdin, print a Claude result."""
    import argparse
    parser = argparse.ArgumentParser(description="DeepSeek Harness hook adapter for Beyin V3")
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--event", required=True, choices=EVENTS)
    args = parser.parse_args(argv)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig", errors="replace")
    try:
        payload = json.loads(sys.stdin.read(1_000_000).lstrip("\ufeff").strip() or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    output = context(args.vault, args.state, args.event, payload, cwd=payload.get("cwd"))
    print(json.dumps(output, ensure_ascii=False) if output else "{}")


if __name__ == "__main__":
    main()
