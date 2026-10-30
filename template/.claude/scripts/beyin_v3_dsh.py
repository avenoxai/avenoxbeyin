"""DeepSeek Harness (dsh) hook adapter and read-only profile report.

DSH desktop loads Claude Code command hooks through the ``dsh-hooks-claude-code``
plugin: a profile's ``cordis.patch.yml`` points ``hooks-claude-code`` at a Claude
``hooks.json`` and the plugin runs each command through ``ctx.shell`` — the same
sandboxed executor its own tools use. Two properties of that bridge shape this
module:

* The payload is Claude-shaped and stdout is parsed as a Claude hook result, so
  the shared ``beyin_v3_hook.py`` output passes through unchanged.
* The hook runs with the session's working directory as ``cwd`` but with the
  *deployment* sandbox root and no per-session policy, which is the DSH profile
  directory. Hooks therefore cannot write the Beyin state directory until the
  profile names one (``sandbox_policy_block``). ``in_vault`` closes the other
  half: one profile serves every folder DSH opens, so a hook outside the vault
  answers ``{}``.

V3 deliberately never edits that profile: it is a user-global YAML file, and a
safe structural edit needs a real parser plus a rollback story the installer's
journal cannot cover. ``doctor`` reads it instead and prints the exact entries
to paste when one is missing or stale.

Only the four events DSH bridges are registered: SessionStart, UserPromptSubmit,
PostToolUse and Stop. PreCompact and SessionEnd are not part of the bridge's
event list, so queueing them would be dead configuration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
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
SANDBOX_ID = "sandbox-policy"
SANDBOX_NAME = "@deepseek-ai/dsh-sandbox-policy"
HOOK_ID = "hooks-claude-code"
HOOK_NAME = "@deepseek-ai/dsh-hooks-claude-code"
PERMISSION_ID = "permission"
PERMISSION_NAME = "@deepseek-ai/dsh-permission-presets"
ENV_MODE = "DSH_PERMISSION_MODE"
DEFAULT_MODE = "workspace-write"


class _Unreadable(ValueError):
    """The profile text uses YAML this reader deliberately does not model."""


def canonical(path):
    """Comparable form of a path: resolved, case-folded on Windows, forward slashes."""
    if path is None or str(path) == "":
        return ""  # An empty value is not the process working directory.
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


def _json_path(path):
    return json.dumps(str(Path(path).resolve()).replace("\\", "/"), ensure_ascii=False)


def sandbox_policy_block(state):
    """The profile entry a user pastes when session-less hooks cannot write the state root.

    ``mode`` keeps the bundle's own expression: a literal would override the user's
    ``DSH_PERMISSION_MODE`` and, when the profile names no ``defaultPreset``, DSH 0.2.1-rc.3
    refuses to start because the patched mode and approval policy match no preset (#7567).
    """
    return "\n".join([
        "- id: sandbox-policy",
        '  name: "@deepseek-ai/dsh-sandbox-policy"',
        "  config:",
        f"    mode: !!js process.env.{ENV_MODE} ?? '{DEFAULT_MODE}'",
        "    workspaceRoot: " + _json_path(state),
    ]) + "\n"


def hook_entry_block(vault):
    """The profile insert a user pastes to load the vault-owned Claude-shaped hook file."""
    return "\n".join([
        "- insert:",
        f"    - id: {HOOK_ID}",
        f'      name: "{HOOK_NAME}"',
        "      config:",
        "        configPath: " + _json_path(Path(vault) / HOOK_FILE),
    ]) + "\n"


def _strip_comment(line):
    """Drop a trailing YAML comment; a ``#`` inside quotes or a URL stays content."""
    quote, index = None, 0
    while index < len(line):
        char = line[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
        index += 1
    return line


def _clean(line):
    return _strip_comment(line).rstrip()


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


_ITEM = re.compile(r"^(?P<indent> *)-(?P<gap> +)?(?P<inline>.*)$")


def _split_quoted(text):
    """Decoded value of a quoted scalar that starts at ``text[0]``, or ``None`` if unclosed."""
    quote = text[0]
    index = 1
    while index < len(text):
        char = text[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if char == quote:
            raw = text[:index + 1]
            if quote == "'":
                return raw[1:-1].replace("''", "'")
            try:
                value = json.loads(raw)
            except ValueError as exc:
                raise _Unreadable("invalid double-quoted scalar") from exc
            return value
        index += 1
    return None


def _flow_end(text):
    """True when every ``[]``/``{}`` in the value is closed outside quotes."""
    depth, quote, index = 0, None, 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        index += 1
    return depth <= 0


def _scalar(lines, index, rest):
    """Read one scalar; a quoted value folded over several lines is joined with spaces."""
    if rest.startswith("!!js"):
        expression = rest[4:].strip()
        if not expression:
            raise _Unreadable("empty !!js expression")
        if expression[0] in "'\"":
            value = _split_quoted(expression)
            cursor = index
            while value is None:
                cursor += 1
                if cursor >= len(lines):
                    raise _Unreadable("unterminated quoted expression")
                expression += " " + _clean(lines[cursor]).strip()
                value = _split_quoted(expression)
            return "!!js " + str(value), cursor + 1
        return rest, index + 1
    if rest[0] in "'\"":
        value = _split_quoted(rest)
        cursor = index
        while value is None:
            cursor += 1
            if cursor >= len(lines):
                raise _Unreadable("unterminated quoted scalar")
            rest += " " + _clean(lines[cursor]).strip()
            value = _split_quoted(rest)
        return str(value), cursor + 1
    if rest[0] in "[{":
        value = rest
        cursor = index
        while not _flow_end(value):
            cursor += 1
            if cursor >= len(lines):
                raise _Unreadable("unterminated flow collection")
            value += " " + _clean(lines[cursor]).strip()
        return value, cursor + 1
    return rest, index + 1


def _mapping(lines, start, indent):
    """Read the block mapping whose keys sit at ``indent``; return ``(values, next index)``.

    Only the shapes a DSH profile patch actually uses are modelled: nested block mappings,
    block sequences (kept as an empty list marker) and scalar values. Anything else raises
    ``_Unreadable`` so no caller mistakes an unknown entry for an absent one.
    """
    values, index = {}, start
    while index < len(lines):
        cleaned = _clean(lines[index])
        if not cleaned.strip():
            index += 1
            continue
        current = _indent_of(cleaned)
        if current < indent:
            break
        if current > indent:  # content of a value this reader already consumed
            index += 1
            continue
        text = cleaned[indent:]
        if text.startswith("- ") or text == "-":
            break
        key, separator, rest = text.partition(":")
        if not separator or not key.strip() or key != key.strip():
            raise _Unreadable("not a block mapping")
        rest = rest.strip()
        if rest:
            values[key], index = _scalar(lines, index, rest)
            continue
        following = index + 1
        while following < len(lines) and not _clean(lines[following]).strip():
            following += 1
        if following >= len(lines):
            values[key], index = {}, following
            continue
        nested = _clean(lines[following])
        if _indent_of(nested) <= indent:
            values[key], index = {}, following
            continue
        if _ITEM.match(nested):
            values[key] = []
            cursor = following
            while cursor < len(lines):
                candidate = _clean(lines[cursor])
                if candidate.strip() and _indent_of(candidate) <= indent:
                    break
                cursor += 1
            index = cursor
            continue
        values[key], index = _mapping(lines, following, _indent_of(nested))
    return values, index


def _entry_values(lines, index, match):
    """The mapping of one ``- `` sequence entry, including ``- id: x`` and ``-`` + body forms."""
    indent = len(match.group("indent"))
    inline = (match.group("inline") or "").strip()
    gap = match.group("gap") or " "
    if inline.startswith(("{", "[")):
        raise _Unreadable("flow-style sequence entry")
    body, cursor = [], index + 1
    while cursor < len(lines):
        cleaned = _clean(lines[cursor])
        if cleaned.strip() and _indent_of(cleaned) <= indent:
            break
        body.append(lines[cursor])
        cursor += 1
    if inline:
        if ":" not in inline:
            return {}  # a scalar sequence item (``- --flag``), not an entry
        content_indent = indent + 1 + len(gap)
        values, _ = _mapping([" " * content_indent + inline] + body, 0, content_indent)
        return values
    first = None
    for offset, line in enumerate(body):
        if _clean(line).strip():
            first = offset
            break
    if first is None or _ITEM.match(_clean(body[first])):
        return {}
    values, _ = _mapping(body, first, _indent_of(_clean(body[first])))
    return values


def _entries(lines):
    """Every readable sequence entry, in file order, plus a count of entries we could not read."""
    entries, unreadable = [], 0
    for index, line in enumerate(lines):
        match = _ITEM.match(_clean(line))
        if not match:
            continue
        try:
            entries.append(_entry_values(lines, index, match))
        except _Unreadable:
            unreadable += 1
    return entries, unreadable


def _block_sequence(lines):
    """True for an empty profile (``[]``, comments only) or a top-level block sequence."""
    for line in lines:
        cleaned = _clean(line).strip()
        if not cleaned:
            continue
        return cleaned == "[]" or _ITEM.match(cleaned) is not None
    return True


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _find(entries, entry_id, name):
    return [entry for entry in entries
            if _text(entry.get("id")) == entry_id or _text(entry.get("name")) == name]


def _covers(root, state):
    """True when the sandbox root is the state directory or one of its ancestors."""
    root, wanted = canonical(root), canonical(state)
    return bool(root) and bool(wanted) and (wanted == root or wanted.startswith(root + "/"))


def profile_status(patch, state, vault, env_mode=None):
    """Read one profile patch: the Beyin hook entry and the session-less sandbox entry."""
    path = Path(patch).expanduser()
    report = {"path": str(path), "exists": path.is_file()}
    if not report["exists"]:
        report.update(status="missing", hook={"status": "absent"}, sandbox={"status": "absent"})
        return report
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        report.update(status="unreadable", error=type(exc).__name__,
                      hook={"status": "absent"}, sandbox={"status": "absent"})
        return report
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    entries, unreadable = _entries(lines)
    if not _block_sequence(lines):
        unreadable += 1  # flow root, a mapping, or a scalar: not a shape we can trust
    warnings = []
    hooks = _find(entries, HOOK_ID, HOOK_NAME)
    hook = {"status": "absent"}
    if hooks:
        config = hooks[-1].get("config")
        target = _text(config.get("configPath")) if isinstance(config, dict) else ""
        if not target:
            hook = {"status": "unreadable"}
            warnings.append("The hooks-claude-code entry has no readable configPath.")
        else:
            hook = {"status": "present", "configPath": target,
                    "current": canonical(target) == canonical(Path(vault) / HOOK_FILE)}
            if not hook["current"]:
                hook["status"] = "outdated"
                warnings.append("hooks-claude-code.configPath must name " + str(Path(vault) / HOOK_FILE) + ".")
        if len(hooks) > 1:
            warnings.append("hooks-claude-code appears %d times; DSH keeps the last entry." % len(hooks))
    policies = _find(entries, SANDBOX_ID, SANDBOX_NAME)
    sandbox = {"status": "absent"}
    boot_risk = False
    if policies:
        config = policies[-1].get("config")
        mode = _text(config.get("mode")) if isinstance(config, dict) else ""
        root = _text(config.get("workspaceRoot")) if isinstance(config, dict) else ""
        if not mode:
            sandbox = {"status": "unreadable"}
            warnings.append("The sandbox-policy entry has no readable mode.")
        else:
            from_env = ENV_MODE in mode
            sandbox = {"status": "present", "mode": mode, "env_mode": from_env,
                       "workspaceRoot": root or None, "covers_state": _covers(root, state)}
            if not root:
                warnings.append("sandbox-policy.workspaceRoot is missing; DSH would fall back to its own "
                                "working directory, not the state directory.")
            elif not sandbox["covers_state"]:
                warnings.append("sandbox-policy.workspaceRoot does not contain the state directory " +
                                str(Path(state)) + ".")
            if "read-only" in mode:
                warnings.append("sandbox-policy.mode is read-only, so hooks cannot write the state directory.")
            elif from_env and (env_mode or DEFAULT_MODE) == "read-only":
                warnings.append(f"{ENV_MODE} is read-only, so the session-less hook runs read-only.")
        if len(policies) > 1:
            warnings.append("sandbox-policy appears %d times; DSH keeps the last entry." % len(policies))
    permissions = _find(entries, PERMISSION_ID, PERMISSION_NAME)
    preset = ""
    if permissions:
        config = permissions[-1].get("config")
        preset = _text(config.get("defaultPreset")) if isinstance(config, dict) else ""
    if not preset and env_mode and env_mode != DEFAULT_MODE:
        boot_risk = True
        warnings.append(f"permission.defaultPreset is not set and {ENV_MODE}={env_mode}: the patched "
                        "sandbox mode and the approval policy match no preset, so DSH 0.2.1-rc.3 "
                        "refuses to start (#7567). Set permission.defaultPreset explicitly.")
    hook_ready = hook.get("status") == "present" and bool(hook.get("current"))
    sandbox_ready = (sandbox.get("status") == "present" and bool(sandbox.get("covers_state"))
                     and "read-only" not in str(sandbox.get("mode") or "")
                     and not (sandbox.get("env_mode") and (env_mode or DEFAULT_MODE) == "read-only"))
    if unreadable:
        # An entry we cannot model may override ours: never report this profile as ready.
        report["status"] = "unreadable"
        report["entries_unreadable"] = unreadable
        warnings.append("%d profile entr%s could not be read; inspect the file by hand."
                        % (unreadable, "y" if unreadable == 1 else "ies"))
    elif hook_ready and sandbox_ready and not boot_risk:
        report["status"] = "ok"
    elif hook["status"] == "absent" and sandbox["status"] == "absent":
        report["status"] = "not_configured"
    else:
        report["status"] = "needs_attention"
    report["hook"], report["sandbox"] = hook, sandbox
    report["permission_default_preset"] = preset or None
    report["boot_risk"] = boot_risk
    report["warnings"] = warnings
    return report


def discover_profiles(home=None):
    """Every DSH profile patch under ``~/.dsh/profiles``; read-only and never guessed."""
    base = Path(home or Path.home()) / ".dsh" / "profiles"
    try:
        return sorted(path / PROFILE_FILE for path in base.iterdir() if (path / PROFILE_FILE).is_file())
    except OSError:
        return []


def _hook_config_report(vault):
    """Read the managed hook JSON: events, pinned adapter and vault, PostToolUse matcher."""
    hook = Path(vault) / HOOK_FILE
    report = {"path": HOOK_FILE, "present": hook.is_file(), "events": [], "adapter_pinned": None,
              "matcher": None}
    if not hook.is_file():
        return report
    try:
        data = json.loads(hook.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        report["adapter_pinned"] = False
        return report
    hooks = data.get("hooks") if isinstance(data, dict) else None
    report["events"] = sorted(hooks) if isinstance(hooks, dict) else []
    root, pinned = canonical(vault), set(report["events"]) == set(EVENTS)
    matcher_ok = True
    for event, groups in (hooks or {}).items():
        for group in groups if isinstance(groups, list) else ():
            if not isinstance(group, dict):
                continue
            if event == "PostToolUse" and group.get("matcher") != "|".join(WRITE_TOOLS):
                matcher_ok = False
            for handler in group.get("hooks", []) if isinstance(group.get("hooks"), list) else ():
                args = _command_arguments(str(handler.get("command", "")))
                paths = [argument.replace("\\", "/") for argument in args]
                if not any(path.endswith("/beyin_v3_dsh.py") for path in paths):
                    pinned = False
                elif "--vault" not in args:
                    pinned = False
                else:
                    index = args.index("--vault")
                    if index + 1 >= len(args) or canonical(args[index + 1]) != root:
                        pinned = False
    report["adapter_pinned"] = pinned
    report["matcher"] = matcher_ok
    return report


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


def _state_report(state, now=None):
    """Observable state signals. ``writable`` is this shell's view, not DSH's sandbox."""
    now = time.time() if now is None else now
    root = Path(state)
    report = {"writable": os.access(root, os.W_OK),
              "last_hook_event": None, "last_hook_event_at": None, "last_hook_age_seconds": None,
              "last_sync_at": None, "last_sync_age_seconds": None, "last_sync_status": None,
              "last_error": None, "last_error_at": None}
    newest = None
    try:
        for path in (root / "hook-done").glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(data, dict) or data.get("harness") != "dsh":
                continue
            at = data.get("at")
            if not isinstance(at, (int, float)):
                try:
                    at = path.stat().st_mtime
                except OSError:
                    continue
            if newest is None or at > newest[0]:
                newest = (at, str(data.get("event") or "unknown"))
    except OSError:
        pass
    if newest is not None:
        report["last_hook_event"], report["last_hook_event_at"] = newest[1], newest[0]
        report["last_hook_age_seconds"] = max(0, int(now - newest[0]))
    try:
        health = json.loads((root / "hook-health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        health = None
    if isinstance(health, dict):
        at, sync = health.get("at"), health.get("sync")
        if isinstance(at, (int, float)):
            report["last_sync_at"] = at
            report["last_sync_age_seconds"] = max(0, int(now - at))
        if isinstance(sync, dict):
            report["last_sync_status"] = sync.get("status")
    try:
        error = json.loads((root / "hook-error.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        error = None
    if isinstance(error, dict):
        report["last_error"] = error.get("error")
        report["last_error_at"] = error.get("at")
    return report


def _manual_setup(reports, vault, state, home=None):
    """The exact entries to paste, for the profile that should carry them."""
    setup = {"path": None, "entries": [], "notes": [], "block": ""}
    wired = [item for item in reports if item.get("hook", {}).get("current")]
    target = wired[0] if len(wired) == 1 else (reports[0] if len(reports) == 1 else None)
    if target is None and reports:
        setup["notes"].append("Several DSH profiles exist; add the entries below to the profile DSH "
                              "desktop loads: " + ", ".join(item["path"] for item in reports))
        setup["entries"] = [sandbox_policy_block(state), hook_entry_block(vault)]
    if target is not None:
        setup["path"] = target["path"]
        if target.get("status") == "unreadable":
            setup["notes"].append("The profile patch contains entries this reader cannot model; "
                                  "inspect the file by hand instead of adding a second entry.")
            return setup
        hook, sandbox = target.get("hook", {}), target.get("sandbox", {})
        if target.get("boot_risk"):
            setup["notes"].append("Set permission.defaultPreset explicitly: with the current " + ENV_MODE +
                                  " the patched sandbox mode matches no preset and DSH will not start (#7567).")
        if hook.get("status") == "absent":
            setup["entries"].append(hook_entry_block(vault))
        elif hook.get("status") == "outdated":
            setup["notes"].append('Set hooks-claude-code.configPath to ' +
                                  _json_path(Path(vault) / HOOK_FILE) + " (do not add a second entry).")
        elif hook.get("status") == "unreadable":
            setup["notes"].append("The hooks-claude-code entry is unreadable; inspect it by hand.")
        if sandbox.get("status") == "absent":
            setup["entries"].append(sandbox_policy_block(state))
        elif sandbox.get("status") == "present":
            if not sandbox.get("covers_state"):
                setup["notes"].append("Set sandbox-policy.workspaceRoot to " + _json_path(state) +
                                      " (do not add a second entry).")
            mode = str(sandbox.get("mode") or "")
            if not mode or mode == "read-only":
                setup["notes"].append("Set sandbox-policy.mode to " +
                                      sandbox_policy_block(state).splitlines()[3].strip())
        elif sandbox.get("status") == "unreadable":
            setup["notes"].append("The sandbox-policy entry is unreadable; inspect it by hand.")
    if not reports:
        setup["path"] = str(Path(home or Path.home()) / ".dsh" / "profiles" / "<profile>" / PROFILE_FILE)
        setup["entries"] = [sandbox_policy_block(state), hook_entry_block(vault)]
        setup["notes"].append("Start DSH once so it creates the profile patch, then add these entries.")
    setup["block"] = "".join(setup["entries"])
    return setup


def doctor(vault, state, home=None, environ=None):
    """Read-only DSH report: hook config, state signals, and every profile patch."""
    environ = os.environ if environ is None else environ
    env_mode = environ.get(ENV_MODE) or None
    hook_config = _hook_config_report(vault)
    profiles = discover_profiles(home)
    reports = [profile_status(path, state, vault, env_mode) for path in profiles]
    report = {"hook_config": hook_config, "state": _state_report(state),
              "profiles": reports, "env_mode": env_mode}
    setup = _manual_setup(reports, vault, state, home)
    hook_ok = bool(hook_config["present"] and hook_config["adapter_pinned"] and hook_config["matcher"])
    if not hook_config["present"]:
        report["status"] = "hook_config_missing"
    elif any(item["status"] == "ok" for item in reports) and hook_ok:
        report["status"] = "ok"
    elif not reports or setup["block"] or setup["notes"]:
        report["status"] = "manual_setup"
    else:
        report["status"] = "needs_attention"
    if report["status"] != "ok":
        report["manual_setup"] = setup
    if hook_config["present"] and (not hook_config["adapter_pinned"] or not hook_config["matcher"]):
        report["warnings"] = ["The managed hook file does not pin the DSH adapter, this vault and the "
                              "write-tool matcher; reinstall or run beyin.py hook-reconcile."]
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
