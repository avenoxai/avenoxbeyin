"""Vault-local read-only retrieval self-test command (#675)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

import beyin_v3 as runtime


def _safe_vault_path(vault_root, path_arg, must_exist=True):
    vault = Path(vault_root).expanduser().resolve()
    raw = Path(path_arg)
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        resolved = (vault / raw).resolve()
    if not resolved.is_relative_to(vault) or ".." in resolved.parts:
        raise ValueError("path traversal or outside vault rejected")

    cursor = resolved
    boundary = vault.parent
    while cursor != boundary and cursor != cursor.parent:
        try:
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_reparse_tag", 0)):
                raise ValueError("symlink path rejected")
        except OSError:
            pass
        if cursor == vault:
            break
        cursor = cursor.parent

    if must_exist and not resolved.is_file():
        raise ValueError(f"file not found: {path_arg}")
    return resolved


def _normal(text):
    return " ".join(str(text).split()).strip()


def add_question(vault, questions_path, query, source, sentence=None):
    vault = Path(vault).expanduser().resolve()
    if questions_path is None:
        q_path = vault / "knowledge/v3/selftest.json"
    else:
        q_path = Path(questions_path)
        if not q_path.is_absolute():
            q_path = vault / q_path
    resolved = q_path.resolve()
    if not resolved.is_relative_to(vault) or ".." in resolved.parts:
        raise ValueError("questions path traversal rejected")

    cursor = resolved
    boundary = vault.parent
    while cursor != boundary and cursor != cursor.parent:
        try:
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_reparse_tag", 0)):
                raise ValueError("symlink questions path rejected")
        except OSError:
            pass
        if cursor == vault:
            break
        cursor = cursor.parent

    # Validate source safely
    src_resolved = _safe_vault_path(vault, source, must_exist=True)
    rel_source = src_resolved.relative_to(vault).as_posix()

    data = {"questions": [], "controls": []}
    if resolved.is_file():
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {"questions": [], "controls": []}
        except Exception:
            data = {"questions": [], "controls": []}

    if "questions" not in data or not isinstance(data["questions"], list):
        data["questions"] = []

    q_item = {"query": str(query), "source": rel_source}
    if sentence:
        q_item["sentence"] = str(sentence)
    data["questions"].append(q_item)

    resolved.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(dir=str(resolved.parent), prefix="selftest-")
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, resolved)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return {"status": "ok", "questions_path": resolved.relative_to(vault).as_posix(), "added": q_item}


def run_selftest(vault, state, questions_path=None, limit=5, audience="internal"):
    vault = Path(vault).expanduser().resolve()
    state = Path(state).expanduser().resolve()

    if questions_path is None:
        q_path = vault / "knowledge/v3/selftest.json"
    else:
        q_path = Path(questions_path)
        if not q_path.is_absolute():
            q_path = vault / q_path

    try:
        resolved_q = q_path.resolve()
        if not resolved_q.is_relative_to(vault) or ".." in resolved_q.parts:
            return {"status": "error", "message": "invalid questions path traversal"}
        cursor = resolved_q
        boundary = vault.parent
        while cursor != boundary and cursor != cursor.parent:
            try:
                info = os.lstat(cursor)
                if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_reparse_tag", 0)):
                    return {"status": "error", "message": "symlink questions path rejected"}
            except OSError:
                pass
            if cursor == vault:
                break
            cursor = cursor.parent
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    if not resolved_q.is_file():
        return {
            "status": "no_questions",
            "questions_path": resolved_q.relative_to(vault).as_posix(),
            "hint": "Create knowledge/v3/selftest.json with test questions or use --add-question."
        }

    try:
        data = json.loads(resolved_q.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "error", "message": f"failed to parse questions JSON: {exc}"}

    questions = data.get("questions", [])
    controls = data.get("controls", [])

    try:
        store = runtime.MemoryStore(state, vault, read_only=True)
    except Exception as exc:
        return {"status": "error", "message": f"failed to open read-only memory store: {exc}"}

    total = len(questions)
    passed = 0
    missed = []

    for q in questions:
        query = q.get("query", "")
        expected_source = q.get("source", "")
        sentence = q.get("sentence")

        try:
            candidates = store.candidates(query, audience=audience, limit=limit)
        except Exception:
            candidates = []

        got_sources = [c.get("source") for c in candidates]
        found = expected_source in got_sources

        if found and sentence:
            matched_sentence = False
            for c in candidates:
                if c.get("source") == expected_source:
                    if _normal(sentence) in _normal(c.get("text", "")):
                        matched_sentence = True
                        break
            found = matched_sentence

        if found:
            passed += 1
        else:
            missed.append({
                "query": query,
                "expected_source": expected_source,
                "got_sources": got_sources
            })

    # Controls handling
    ctrl_total = 0
    ctrl_passed = 0
    ctrl_missed = []

    control_list = []
    if isinstance(controls, list):
        control_list = controls
    elif isinstance(controls, dict):
        for group_items in controls.values():
            if isinstance(group_items, list):
                control_list.extend(group_items)

    for ctrl_query in control_list:
        ctrl_total += 1
        try:
            candidates = store.candidates(ctrl_query, audience=audience, limit=limit)
        except Exception:
            candidates = []
        if not candidates:
            ctrl_passed += 1
        else:
            ctrl_missed.append({
                "query": ctrl_query,
                "got_sources": [c.get("source") for c in candidates]
            })

    # Compare with previous run stored in state / selftest-last.json
    last_file = state / "selftest-last.json"
    previous_data = None
    if last_file.is_file():
        try:
            previous_data = json.loads(last_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    previous_passed_queries = set()
    if previous_data and isinstance(previous_data.get("passed_queries"), list):
        previous_passed_queries = set(previous_data["passed_queries"])

    passed_query_set = {q.get("query") for q in questions if q.get("query") not in [m["query"] for m in missed]}
    regressed = [m for m in missed if m["query"] in previous_passed_queries]

    current_state_payload = {
        "timestamp": runtime._utc_now() if hasattr(runtime, "_utc_now") else "",
        "passed_queries": list(passed_query_set),
        "passed": passed,
        "total": total
    }

    try:
        state.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=str(state), prefix="selftest-last-")
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(current_state_payload, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, last_file)
    except Exception:
        pass

    return {
        "status": "ok",
        "questions_path": resolved_q.relative_to(vault).as_posix(),
        "total": total,
        "passed": passed,
        "missed": missed,
        "controls_total": ctrl_total,
        "controls_passed": ctrl_passed,
        "controls_missed": ctrl_missed,
        "previous": previous_data,
        "regressed": regressed
    }
