import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
TOKEN_LOG_FILE = BASE_DIR / ".token_usage.jsonl"
TOKEN_META_FILE = BASE_DIR / ".token_stats_meta.json"
AGY_DATA_DIR = Path(os.environ.get("AGY_DATA_DIR") or str(Path.home() / ".gemini" / "antigravity-cli"))
BRAIN_DIR = AGY_DATA_DIR / "brain"

_lock = threading.Lock()


def _empty_bucket() -> Dict[str, int]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "thinking_tokens": 0,
        "total_tokens": 0,
    }


def _add_to_bucket(bucket: Dict[str, int], rec: Dict[str, Any]) -> None:
    bucket["calls"] += 1
    bucket["input_tokens"] += int(rec.get("input_tokens") or 0)
    bucket["output_tokens"] += int(rec.get("output_tokens") or 0)
    bucket["cache_read_tokens"] += int(rec.get("cache_read_tokens") or 0)
    bucket["thinking_tokens"] += int(rec.get("thinking_tokens") or 0)
    bucket["total_tokens"] += int(rec.get("total_tokens") or 0)


def _get_or_init_started_at() -> float:
    if TOKEN_META_FILE.exists():
        try:
            data = json.loads(TOKEN_META_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("started_at"), (int, float)):
                return float(data["started_at"])
        except Exception:
            pass
    now = time.time()
    TOKEN_META_FILE.write_text(
        json.dumps({"started_at": now}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return now


def _parse_iso_ts(iso_str: str) -> float:
    if not iso_str:
        return 0.0
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _load_transcript_turns(started_at: float) -> List[Dict[str, Any]]:
    """Scan local agy transcript.jsonl files for turns after started_at."""
    if not BRAIN_DIR.exists():
        return []
    turns: List[Dict[str, Any]] = []
    for tf in BRAIN_DIR.glob("*/.system_generated/logs/transcript.jsonl"):
        try:
            if tf.stat().st_mtime < started_at:
                continue
        except Exception:
            continue
        conv_id = tf.parent.parent.parent.name
        cur: Optional[Dict[str, Any]] = None

        def flush_cur() -> None:
            nonlocal cur
            if cur is None:
                return
            tot = cur["input_tokens"] + cur["output_tokens"]
            if (tot > 0 or cur["cache_read_tokens"] > 0) and cur["ts"] >= started_at:
                dt = datetime.fromtimestamp(cur["ts"])
                cur["total_tokens"] = tot
                cur["date"] = dt.strftime("%Y-%m-%d")
                cur["time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                turns.append(cur)
            cur = None

        try:
            lines = tf.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            mtype = d.get("type")
            if mtype == "USER_INPUT":
                flush_cur()
            elif mtype == "PLANNER_RESPONSE":
                ts = _parse_iso_ts(str(d.get("created_at") or ""))
                if ts <= 0:
                    continue
                if cur is None:
                    cur = {
                        "ts": ts,
                        "source": "remote / cli",
                        "conversation_id": conv_id,
                        "model": str(d.get("model") or ""),
                        "cwd": "",
                        "duration_seconds": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        "thinking_tokens": 0,
                        "total_tokens": 0,
                    }
                cur["ts"] = max(cur["ts"], ts)
                if d.get("model"):
                    cur["model"] = str(d["model"])
                cur["input_tokens"] += int(d.get("input_tokens") or 0)
                cur["output_tokens"] += int(d.get("output_tokens") or 0)
                cur["cache_read_tokens"] += int(d.get("cache_read_tokens") or 0)
                cur["thinking_tokens"] += int(d.get("thinking_tokens") or 0)
                if (d.get("content") or "").strip():
                    flush_cur()
        flush_cur()
    return turns


def normalize_usage(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    if not isinstance(usage, dict):
        return None
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cache = int(usage.get("cache_read_tokens") or 0)
    think = int(usage.get("thinking_tokens") or 0)
    tot = int(usage.get("total_tokens") or 0) or (inp + out)
    if inp == 0 and out == 0 and cache == 0 and think == 0 and tot == 0:
        return None
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache,
        "thinking_tokens": think,
        "total_tokens": tot,
    }


def record_token_usage(
    usage: Optional[Dict[str, Any]],
    source: str = "web",
    conversation_id: str = "",
    model: str = "",
    cwd: str = "",
    duration_seconds: float = 0.0,
) -> Optional[Dict[str, Any]]:
    norm = normalize_usage(usage)
    if not norm:
        return None
    now = time.time()
    dt = datetime.fromtimestamp(now)
    rec: Dict[str, Any] = {
        "ts": round(now, 3),
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source or "web",
        "conversation_id": conversation_id or "",
        "model": model or "",
        "cwd": cwd or "",
        "duration_seconds": round(float(duration_seconds or 0.0), 2),
        **norm,
    }
    with _lock:
        _get_or_init_started_at()
        with TOKEN_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def get_token_stats(days: int = 7) -> Dict[str, Any]:
    with _lock:
        started_at = _get_or_init_started_at()
        local_records: List[Dict[str, Any]] = []
        if TOKEN_LOG_FILE.exists():
            for line in TOKEN_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if float(d.get("ts") or 0) >= started_at:
                        local_records.append(d)
                except Exception:
                    continue

    # Merge local RemoteAgy/Bot records with brain transcript.jsonl turns (e.g. remote-control / CLI)
    transcript_turns = _load_transcript_turns(started_at)
    merged: List[Dict[str, Any]] = list(local_records)
    for t in transcript_turns:
        t_cid = t.get("conversation_id") or ""
        t_ts = float(t.get("ts") or 0)
        is_dup = any(
            (r.get("conversation_id") or "") == t_cid
            and abs(float(r.get("ts") or 0) - t_ts) <= 30.0
            for r in local_records
        )
        if not is_dup:
            merged.append(t)

    merged.sort(key=lambda x: float(x.get("ts") or 0))

    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")
    last_n_dates = [
        (now_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(days - 1, -1, -1)
    ]
    last_n_set = set(last_n_dates)

    today_bucket = _empty_bucket()
    last_7d_bucket = _empty_bucket()
    total_bucket = _empty_bucket()
    by_date: Dict[str, Dict[str, int]] = {}

    for rec in merged:
        d_str = str(rec.get("date") or "")
        if not d_str and rec.get("ts"):
            d_str = datetime.fromtimestamp(float(rec["ts"])).strftime("%Y-%m-%d")
        _add_to_bucket(total_bucket, rec)
        if d_str == today_str:
            _add_to_bucket(today_bucket, rec)
        if d_str in last_n_set:
            _add_to_bucket(last_7d_bucket, rec)
        if d_str:
            bucket = by_date.setdefault(d_str, _empty_bucket())
            _add_to_bucket(bucket, rec)

    daily_7d = [
        {"date": d_str, **(by_date.get(d_str) or _empty_bucket())}
        for d_str in reversed(last_n_dates)
    ]
    daily_all = [
        {"date": d_str, **by_date[d_str]}
        for d_str in sorted(by_date.keys(), reverse=True)
    ]

    recent_calls = list(reversed(merged[-20:]))

    return {
        "ok": True,
        "started_at": int(started_at),
        "started_at_str": datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S"),
        "today_date": today_str,
        "today": today_bucket,
        "last_7_days": last_7d_bucket,
        "total": total_bucket,
        "daily_7d": daily_7d,
        "daily_all": daily_all,
        "recent_calls": recent_calls,
    }


def reset_token_stats() -> Dict[str, Any]:
    with _lock:
        now = time.time()
        TOKEN_META_FILE.write_text(
            json.dumps({"started_at": now}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return get_token_stats()
