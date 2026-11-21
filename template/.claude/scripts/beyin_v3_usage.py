# -*- coding: utf-8 -*-
"""Note usage tracking, citation recording, feedback logging, and feedback hint generation.
Zero network/model calls, stdlib only.
"""

from datetime import datetime, timezone, timedelta
import json
import os
import tempfile
from pathlib import Path

MAX_ENTRIES = 2000

def _is_private_or_sensitive(r: dict) -> bool:
    if not isinstance(r, dict):
        return True
    if r.get("private") or r.get("is_private") or r.get("trusted") is False:
        return True
    vis = str(r.get("visibility", "")).strip().lower()
    if vis in ("private", "untrusted", "secret", "internal"):
        return True
    trust = str(r.get("trust", "")).strip().lower()
    if trust in ("untrusted", "false", "private"):
        return True
    status = str(r.get("status", "")).strip().lower()
    if status in ("untrusted", "private"):
        return True
    kind = str(r.get("kind", "")).strip().lower()
    if kind in ("untrusted", "private"):
        return True

    source = str(r.get("source") or r.get("path") or "")
    if source:
        s_lower = source.lower()
        if any(p in s_lower for p in (".env", "secret", "password", "credential", "private", "token", "key")):
            return True

    return False

def _load_usage_data(state_path: Path) -> dict:
    usage_file = state_path / 'note-usage.json'
    if not usage_file.exists():
        return {"records": {}}
    try:
        with open(usage_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict) and "records" in data:
                return data
    except Exception:
        pass
    return {"records": {}}

def _save_usage_data(state_path: Path, data: dict):
    usage_file = state_path / 'note-usage.json'
    try:
        state_path.mkdir(parents=True, exist_ok=True)
        records = data.get("records", {})
        if len(records) > MAX_ENTRIES:
            sorted_keys = sorted(
                records.keys(),
                key=lambda k: records[k].get("last_injected") or records[k].get("last_cited") or records[k].get("last_feedback_at") or ""
            )
            excess = len(records) - MAX_ENTRIES
            for k in sorted_keys[:excess]:
                records.pop(k, None)

        fd, tmp_path = tempfile.mkstemp(dir=str(state_path), prefix="note-usage-", suffix=".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, usage_file)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
    except Exception:
        pass

def record_injected(state_path: Path, records: list, now=None):
    if not state_path or not records:
        return
    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    data = _load_usage_data(state_path)
    rec_dict = data.setdefault("records", {})
    last_turn = []

    for r in records:
        if not isinstance(r, dict):
            continue
        if _is_private_or_sensitive(r):
            continue

        rec_id = r.get("id") or r.get("record_id") or r.get("source") or r.get("path")
        if not rec_id:
            continue
        rec_id = str(rec_id)

        source = str(r.get("source") or r.get("path") or rec_id)

        entry = rec_dict.setdefault(rec_id, {
            "record_id": rec_id,
            "source": source,
            "injected": 0,
            "cited": 0,
            "feedback": {"ok": 0, "wrong": 0, "stale": 0},
            "last_injected": None,
            "last_cited": None,
            "last_feedback_at": None
        })
        entry["injected"] += 1
        entry["last_injected"] = now

        if len(last_turn) < 10:
            last_turn.append({"id": rec_id, "source": source})

    data["last_turn"] = last_turn
    _save_usage_data(state_path, data)

def record_cited(state_path: Path, refs: list, record_ids=None, now=None):
    if not state_path or (not refs and not record_ids):
        return
    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    data = _load_usage_data(state_path)
    rec_dict = data.setdefault("records", {})

    targets = set()
    if refs:
        for ref in refs:
            if isinstance(ref, str):
                targets.add(ref.strip())
            elif isinstance(ref, dict):
                r_id = ref.get("id") or ref.get("record_id") or ref.get("source") or ref.get("path")
                if r_id:
                    targets.add(str(r_id).strip())
    if record_ids:
        for rid in record_ids:
            if rid:
                targets.add(str(rid).strip())

    matched = False
    for t in targets:
        found_key = None
        for k, v in rec_dict.items():
            if t == k or t == v.get("source") or t.replace("\\", "/") == v.get("source", "").replace("\\", "/"):
                found_key = k
                break
        if found_key:
            entry = rec_dict[found_key]
            entry["cited"] += 1
            entry["last_cited"] = now
            matched = True
        else:
            rec_dict[t] = {
                "record_id": t,
                "source": t,
                "injected": 0,
                "cited": 1,
                "feedback": {"ok": 0, "wrong": 0, "stale": 0},
                "last_injected": None,
                "last_cited": now,
                "last_feedback_at": None
            }
            matched = True

    if matched:
        _save_usage_data(state_path, data)

def record_feedback(state_path: Path, target_id_or_source: str, verdict: str, store=None, now=None):
    verdict = verdict.strip().lower()
    if verdict not in ("ok", "wrong", "stale"):
        return {"success": False, "error": f"Invalid verdict: {verdict}. Must be ok, wrong, or stale."}

    if not state_path or not target_id_or_source:
        return {"success": False, "error": "Missing state path or target."}

    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    data = _load_usage_data(state_path)
    rec_dict = data.setdefault("records", {})

    target = str(target_id_or_source).strip()
    found_key = None

    for k, v in rec_dict.items():
        if target == k or target == v.get("source") or target.replace("\\", "/") == v.get("source", "").replace("\\", "/"):
            found_key = k
            break

    if not found_key and store and hasattr(store, "_records"):
        try:
            store_records = store._records()
            if isinstance(store_records, list):
                for sr_obj in store_records:
                    if isinstance(sr_obj, dict):
                        if _is_private_or_sensitive(sr_obj):
                            continue
                        sr_id = str(sr_obj.get("id") or sr_obj.get("record_id") or "")
                        sr_src = str(sr_obj.get("source") or sr_obj.get("path") or "")
                        if target == sr_id or target == sr_src or target.replace("\\", "/") == sr_src.replace("\\", "/"):
                            found_key = sr_id or sr_src
                            break
        except Exception:
            pass

    if not found_key:
        found_key = target

    entry = rec_dict.setdefault(found_key, {
        "record_id": found_key,
        "source": found_key,
        "injected": 0,
        "cited": 0,
        "feedback": {"ok": 0, "wrong": 0, "stale": 0},
        "last_injected": None,
        "last_cited": None,
        "last_feedback_at": None
    })

    if "feedback" not in entry or not isinstance(entry["feedback"], dict):
        entry["feedback"] = {"ok": 0, "wrong": 0, "stale": 0}

    entry["feedback"][verdict] += 1
    entry["last_feedback_at"] = now

    _save_usage_data(state_path, data)
    return {"success": True, "target": found_key, "verdict": verdict, "feedback": entry["feedback"]}

def summarize_usage(state_path: Path, *, threshold=10, window_days=30, now=None):
    if not state_path:
        return {"tracked": 0, "dead_weight_count": 0, "feedback_totals": {"ok": 0, "wrong": 0, "stale": 0}}

    data = _load_usage_data(state_path)
    rec_dict = data.get("records", {})

    tracked = len(rec_dict)
    dead_weight_count = 0
    feedback_totals = {"ok": 0, "wrong": 0, "stale": 0}

    if now is None:
        try:
            now_dt = datetime.now(timezone.utc)
        except Exception:
            now_dt = datetime.utcnow()
    else:
        if isinstance(now, str):
            try:
                now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
            except Exception:
                now_dt = datetime.now(timezone.utc)
        else:
            now_dt = now

    window_threshold_date = now_dt - timedelta(days=window_days)

    for k, v in rec_dict.items():
        if not isinstance(v, dict):
            continue
        inj = v.get("injected", 0)
        cited = v.get("cited", 0)
        fb = v.get("feedback", {})
        ok_count = fb.get("ok", 0) if isinstance(fb, dict) else 0
        wrong_count = fb.get("wrong", 0) if isinstance(fb, dict) else 0
        stale_count = fb.get("stale", 0) if isinstance(fb, dict) else 0

        feedback_totals["ok"] += ok_count
        feedback_totals["wrong"] += wrong_count
        feedback_totals["stale"] += stale_count

        last_inj_str = v.get("last_injected")
        within_window = True
        if last_inj_str:
            try:
                last_dt = datetime.fromisoformat(last_inj_str.replace("Z", "+00:00"))
                if last_dt < window_threshold_date:
                    within_window = False
            except Exception:
                pass

        if within_window and inj >= threshold and cited == 0 and ok_count == 0:
            dead_weight_count += 1

    return {
        "tracked": tracked,
        "dead_weight_count": dead_weight_count,
        "feedback_totals": feedback_totals
    }

def feedback_hint(state_path: Path, query: str, now=None) -> str:
    if not state_path or not query:
        return ""

    q_str = str(query).strip()
    if not q_str or len(q_str) > 40:
        return ""

    q_clean = q_str.lower()
    cues = {"yanlış", "yanlis", "eski", "eskimis", "dogru", "doğru", "wrong", "stale", "ok"}

    matched_cue = False
    words = q_clean.split()
    for w in words:
        clean_w = w.strip(".,!?;:\'()[]{}<>")
        if clean_w in cues:
            matched_cue = True
            break

    if not matched_cue:
        return ""

    data = _load_usage_data(state_path)
    last_turn = data.get("last_turn", [])
    if not last_turn or not isinstance(last_turn, list):
        return ""

    sources_str = ", ".join(item.get("source") or item.get("id", "") for item in last_turn if isinstance(item, dict))
    if not sources_str:
        return ""

    return f"[Hafıza geri bildirimi] Önceki turda sunulan kaynaklar: {sources_str}. Geri bildirim vermek için: beyin.py feedback <hedef> <ok|wrong|stale>"
