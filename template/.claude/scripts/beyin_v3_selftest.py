"""Vault-local read-only retrieval self-test command (#675)."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import sys

_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

import beyin_v3 as runtime
from beyin_v3_notes import vault_locked
from beyin_v3_sync import atomic


def _safe_vault_path(vault_root, path_arg, must_exist=True):
    vault = Path(vault_root).expanduser().resolve()
    raw = Path(path_arg)
    resolved = raw if raw.is_absolute() else vault / raw
    if not resolved.is_relative_to(vault) or ".." in resolved.parts:
        raise ValueError("path traversal or outside vault rejected")
    cursor = resolved
    while cursor != vault:
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0):
                raise ValueError("symlink path rejected")
        cursor = cursor.parent

    if must_exist and not resolved.is_file():
        raise ValueError(f"file not found: {path_arg}")
    return resolved


def _normal(text):
    return " ".join(str(text).split()).strip()


def add_question(vault, questions_path, query, source, sentence=None):
    vault = Path(vault).expanduser().resolve()
    resolved = _safe_vault_path(vault, questions_path or "knowledge/v3/selftest.json", False)
    # Validate source safely
    src_resolved = _safe_vault_path(vault, source, must_exist=True)
    rel_source = src_resolved.relative_to(vault).as_posix()

    with vault_locked(vault):
        data = {"questions": [], "controls": []}
        if resolved.is_file():
            data = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
                raise ValueError("invalid questions file")

        q_item = {"query": str(query), "source": rel_source}
        if sentence:
            q_item["sentence"] = str(sentence)
        data["questions"].append(q_item)

        atomic(resolved, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {"status": "ok", "questions_path": resolved.relative_to(vault).as_posix(), "added": q_item}


def run_selftest(vault, state, questions_path=None, limit=5, audience="internal", *, mode="candidates", budget=8000):
    vault = Path(vault).expanduser().resolve()
    state = Path(state).expanduser().resolve()

    try:
        resolved_q = _safe_vault_path(vault, questions_path or "knowledge/v3/selftest.json", False)
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

    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return {"status": "error", "message": "invalid questions file"}
    for question in data["questions"]:
        if not isinstance(question, dict) or not isinstance(question.get("query"), str):
            return {"status": "error", "message": "invalid question"}
        source = question.get("source")
        alternatives = question.get("sources", [])
        if (not isinstance(alternatives, list)
                or not all(isinstance(item, str) and item for item in alternatives)
                or (source is not None and not isinstance(source, str))
                or not (source or alternatives)):
            return {"status": "error", "message": "invalid question source"}
    questions = data.get("questions", [])
    controls = data.get("controls", [])

    try:
        store = runtime.MemoryStore(state, vault, read_only=True)
    except Exception as exc:
        return {"status": "error", "message": f"failed to open read-only memory store: {exc}"}

    total = len(questions)
    passed = 0
    missed = []

    def retrieve(query):
        if mode == "context":
            context = store.context_for("codex", query, project=runtime.context_project(vault, query),
                                        audience=audience, limit=limit, budget_chars=budget, explicit=True)
            return context["records"], context.sources
        return store.candidates(query, audience=audience, limit=limit), ()

    truncated = 0
    for q in questions:
        query = q["query"]
        expected = set(q.get("sources", []))
        if q.get("source"):
            expected.add(q["source"])
        sentence = _normal(q.get("sentence") or "")

        try:
            candidates, originals = retrieve(query)
        except Exception as exc:
            return {"status": "error", "message": f"retrieval failed: {exc}"}

        def matches(record):
            return record.get("source") in expected and sentence in _normal(record.get("text", ""))

        if any(matches(record) for record in candidates):
            passed += 1
        else:
            miss = {
                "query": query,
                "expected_source": q.get("source") or q["sources"][0],
                "got_sources": [record.get("source") for record in candidates]
            }
            if q.get("sources"):
                miss["expected_sources"] = sorted(expected)
            if sentence and any(matches(record) for record in originals):
                miss["reason"] = "truncated"
                truncated += 1
            missed.append(miss)

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
            candidates, _ = retrieve(ctrl_query)
        except Exception as exc:
            return {"status": "error", "message": f"retrieval failed: {exc}"}
        if not candidates:
            ctrl_passed += 1
        else:
            ctrl_missed.append({
                "query": ctrl_query,
                "got_sources": [c.get("source") for c in candidates]
            })

    summary = {
        "status": "ok", "questions_path": resolved_q.relative_to(vault).as_posix(),
        "total": total, "passed": passed, "missed": missed,
        "controls_total": ctrl_total, "controls_passed": ctrl_passed, "controls_missed": ctrl_missed,
    }
    if mode == "context":
        return dict(summary, mode=mode, budget=budget, truncated=truncated)

    # Compare with previous run stored in state / selftest-last.json
    last_file = state / "selftest-last.json"
    previous_data = None
    if last_file.is_file() and not last_file.is_symlink():
        try:
            previous_data = json.loads(last_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    previous_passed_queries = set()
    if isinstance(previous_data, dict) and isinstance(previous_data.get("passed_queries"), list):
        previous_passed_queries = {query for query in previous_data["passed_queries"] if isinstance(query, str)}

    passed_query_set = {q.get("query") for q in questions if q.get("query") not in [m["query"] for m in missed]}
    regressed = [m for m in missed if m["query"] in previous_passed_queries]

    current_state_payload = {
        "timestamp": runtime._utc_now() if hasattr(runtime, "_utc_now") else "",
        "passed_queries": sorted(passed_query_set),
        "passed": passed,
        "total": total
    }

    try:
        atomic(last_file, json.dumps(current_state_payload, ensure_ascii=False, indent=2))
    except OSError:
        return {"status": "error", "message": "failed to save selftest result"}

    return dict(summary, previous=previous_data, regressed=regressed)
