"""Antigravity CLI (agy) 数据源：brain/<conversation_id>/.system_generated/logs/transcript.jsonl → UsageEntry。

布局：
`<antigravity_home>/brain/<conversation_id>/.system_generated/logs/transcript.jsonl`
`<antigravity_home>/conversation_summaries.db`（记录 conversation_id 与 workspace_uris 映射）
"""

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from .types import AgentInfo, RateLimits, UsageEntry, normalize_pct
from .util import (
    antigravity_home,
    file_may_have_events_since,
    iter_jsonl_dicts,
    project_from_cwd,
)

ANTIGRAVITY_DIR = antigravity_home()
BRAIN_DIR = os.path.join(ANTIGRAVITY_DIR, "brain")
CONVERSATION_SUMMARIES_DB = os.path.join(ANTIGRAVITY_DIR, "conversation_summaries.db")

_DIR_ARG_KEYS = frozenset({"cwd", "workdir", "directory", "dir", "workspace", "root"})


def detect() -> AgentInfo | None:
    try:
        if Path(ANTIGRAVITY_DIR).is_dir():
            return AgentInfo(id="antigravity", name="Antigravity")
    except OSError:
        return None
    return None


def load_entries(hours_back: int = 0) -> list[UsageEntry]:
    cutoff = None
    if hours_back > 0:
        cutoff = datetime.now(UTC) - timedelta(hours=hours_back)
    return _load_entries(cutoff, cutoff)


def load_recent_entries(cutoff: datetime) -> list[UsageEntry]:
    """读取 cutoff 后仍有写入的完整会话，供 `tt sessions` 渐进查找最近 N 条。"""
    return _load_entries(cutoff, None)


def _load_entries(file_cutoff: datetime | None, entry_cutoff: datetime | None) -> list[UsageEntry]:
    entries: list[UsageEntry] = []
    seen: set[str] = set()

    brain_path = Path(BRAIN_DIR)
    try:
        if not brain_path.is_dir():
            return entries
    except OSError:
        return entries

    workspaces = load_conversation_workspaces(CONVERSATION_SUMMARIES_DB)

    try:
        paths = list(brain_path.glob("*/.system_generated/logs/transcript.jsonl"))
    except OSError:
        return entries

    for jsonl_path in paths:
        if not file_may_have_events_since(jsonl_path, file_cutoff):
            continue
        _parse_jsonl(jsonl_path, workspaces, entries, seen, entry_cutoff)

    entries.sort(key=lambda e: e.timestamp)
    return entries


def _workspace_uri_to_path(uri: str) -> str:
    """将 file:// URI 或本地路径还原为文件系统路径（含百分号解码与 Windows 盘符修正）。"""
    if not uri.startswith("file://"):
        return uri
    parsed = urlparse(uri)
    raw_path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        raw_path = f"//{parsed.netloc}{raw_path}"
    elif len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    return raw_path


def load_conversation_workspaces(db_path: Path | str) -> dict[str, str]:
    """从 conversation_summaries.db 读取会话的 workspace_uris 映射到项目名。"""
    path = Path(db_path)
    conn: sqlite3.Connection | None = None
    try:
        if not path.is_file():
            return {}
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute("SELECT conversation_id, workspace_uris FROM conversation_summaries").fetchall()
    except (sqlite3.Error, OSError):
        return {}
    finally:
        if conn is not None:
            conn.close()

    mapping: dict[str, str] = {}
    for cid, uris_raw in rows:
        if not isinstance(cid, str) or not isinstance(uris_raw, str) or not uris_raw:
            continue
        try:
            uris = json.loads(uris_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(uris, list) and uris and isinstance(uris[0], str):
            target = _workspace_uri_to_path(uris[0])
            if target:
                mapping[cid] = project_from_cwd(target)
    return mapping


def _unquote_arg(val: str) -> str:
    """Antigravity transcript 的 tool_calls.args 常见 JSON 二次引号包裹（如 '"\\"/Users/...\\""'），先解壳再清洗。"""
    s = val.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            decoded = json.loads(s)
            if isinstance(decoded, str):
                s = decoded.strip()
        except json.JSONDecodeError:
            s = s[1:-1].strip()
    return s.strip("\"'")


def _is_abs_path(s: str) -> bool:
    return s.startswith("/") or (len(s) > 2 and s[1] == ":" and s[2] in ("/", "\\"))


def _is_internal_agent_path(path_str: str) -> bool:
    norm = path_str.replace("\\", "/")
    return "/.gemini/" in norm or norm.endswith("/.gemini")


def _path_to_project_dir(path_str: str, key: str) -> str:
    """将工具参数里的路径收敛到目录再交给 project_from_cwd，避免非 git 目录下把文件名当项目名。"""
    if key.lower() in _DIR_ARG_KEYS:
        return path_str
    try:
        if os.path.isdir(path_str):
            return path_str
    except OSError:
        pass
    # 文件路径（或带扩展名的不存在文件路径）回退到父目录
    base = os.path.basename(path_str.rstrip("/\\"))
    if "." in base and not base.startswith("."):
        parent = os.path.dirname(path_str.rstrip("/\\"))
        if parent:
            return parent
    return path_str


def extract_project_from_tool_calls(tool_calls: object) -> str | None:
    """从单条 MODEL 消息的 tool_calls 参数中推断项目名；跳过 ~/.gemini 内部路径，避免 skill/日志劫持项目名。"""
    if not isinstance(tool_calls, list):
        return None
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        args = call.get("args")
        if not isinstance(args, dict):
            continue
        for key, val in args.items():
            if not isinstance(val, str):
                continue
            clean = _unquote_arg(val)
            if not _is_abs_path(clean) or _is_internal_agent_path(clean):
                continue
            candidate = _path_to_project_dir(clean, str(key))
            proj = project_from_cwd(candidate)
            if proj != "unknown":
                return proj
    return None


def _parse_iso_utc(ts_str: object) -> datetime | None:
    if not isinstance(ts_str, str) or not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _parse_jsonl(
    path: Path,
    workspaces: dict[str, str],
    entries: list[UsageEntry],
    seen: set[str],
    cutoff: datetime | None,
) -> None:
    # 路径形如：brain/<conversation_id>/.system_generated/logs/transcript.jsonl
    session_id = path.parents[2].name if len(path.parents) >= 3 else path.parent.name
    project = workspaces.get(session_id)
    session_entries: list[UsageEntry] = []

    for data in iter_jsonl_dicts(path):
        if data.get("source") != "MODEL":
            continue

        if not project:
            project = extract_project_from_tool_calls(data.get("tool_calls"))

        input_tokens = _int_or_zero(data.get("input_tokens"))
        output_tokens = _int_or_zero(data.get("output_tokens"))
        cache_read = _int_or_zero(data.get("cache_read_tokens"))
        cache_creation = _int_or_zero(data.get("cache_creation_tokens"))

        if not (input_tokens or output_tokens or cache_read or cache_creation):
            continue

        ts = _parse_iso_utc(data.get("created_at"))
        if ts is None:
            continue

        if cutoff and ts < cutoff:
            continue

        step_idx = data.get("step_index", 0)
        message_id = f"{session_id}:{step_idx}"
        model = data.get("model")

        entry = UsageEntry(
            timestamp=ts,
            session_id=session_id,
            message_id=message_id,
            request_id="",
            model=model if isinstance(model, str) and model else "unknown",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
            cost_usd=None,
            project=project or "unknown",
            agent_id="antigravity",
            message_count=1,
        )

        if entry.dedup_key in seen:
            continue
        seen.add(entry.dedup_key)
        session_entries.append(entry)

    resolved_project = project or "unknown"
    if resolved_project != "unknown":
        for entry in session_entries:
            if entry.project == "unknown":
                entry.project = resolved_project
    entries.extend(session_entries)


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _reset_epoch(entry: dict, received_ts: float | None) -> int | None:
    raw = entry.get("reset_time") or entry.get("resets_at")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, str) and raw:
        ts = _parse_iso_utc(raw)
        if ts is not None:
            return int(ts.timestamp())
    sec = entry.get("reset_in_seconds")
    if isinstance(sec, (int, float)) and not isinstance(sec, bool) and received_ts is not None:
        return int(received_ts + sec)
    return None


def _used_pct(entry: dict) -> float | None:
    rem = entry.get("remaining_fraction")
    if isinstance(rem, (int, float)) and not isinstance(rem, bool):
        return round(max(0.0, min(100.0, (1.0 - float(rem)) * 100.0)), 1)
    pct = entry.get("used_percentage")
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        return round(max(0.0, min(100.0, float(pct))), 1)
    return None


def load_rate_limits() -> RateLimits | None:
    """从 Antigravity statusLine 落盘的 tt-antigravity-status.json 提取 5h / 7d 配额与模型名。"""
    from .. import config

    path = config.ANTIGRAVITY_STATUS_FILE
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    received_dt = _parse_iso_utc(data.get("_received_at"))
    received_ts = received_dt.timestamp() if received_dt is not None else None
    now_ts = datetime.now(UTC).timestamp()

    model_info = data.get("model")
    if isinstance(model_info, dict):
        model_name = model_info.get("display_name") or model_info.get("id") or model_info.get("name") or ""
        model_id = model_info.get("id") or model_info.get("name") or ""
    elif isinstance(model_info, str):
        model_name = model_id = model_info
    else:
        model_name = model_id = ""

    five_pct = seven_pct = None
    five_reset = seven_reset = None

    quota = data.get("quota")
    if isinstance(quota, dict) and quota:
        prefer_3p = str(model_id).lower().startswith(("claude", "gpt", "o1", "o3", "o4"))

        def _score(name: str) -> tuple[int, str]:
            nl = name.lower()
            is_3p = "3p" in nl or "claude" in nl or "gpt" in nl
            return (0 if is_3p == prefer_3p else 1, nl)

        for bucket_name in sorted(quota, key=_score):
            entry = quota.get(bucket_name)
            if not isinstance(entry, dict) or entry.get("disabled"):
                continue
            pct = _used_pct(entry)
            if pct is None:
                continue
            nl = bucket_name.lower()
            if "5h" in nl and five_pct is None:
                five_reset = _reset_epoch(entry, received_ts)
                five_pct = normalize_pct(pct, five_reset, now_ts)
            elif ("week" in nl or "7d" in nl) and seven_pct is None:
                seven_reset = _reset_epoch(entry, received_ts)
                seven_pct = normalize_pct(pct, seven_reset, now_ts)
    else:
        rl = data.get("rate_limits")
        if isinstance(rl, dict):
            five = rl.get("five_hour")
            if isinstance(five, dict):
                five_reset = _reset_epoch(five, received_ts)
                five_pct = normalize_pct(_used_pct(five), five_reset, now_ts)
            seven = rl.get("seven_day")
            if isinstance(seven, dict):
                seven_reset = _reset_epoch(seven, received_ts)
                seven_pct = normalize_pct(_used_pct(seven), seven_reset, now_ts)

    if five_pct is None and seven_pct is None:
        return None
    return RateLimits(
        five_hour_pct=five_pct,
        five_hour_resets_at=five_reset,
        seven_day_pct=seven_pct,
        seven_day_resets_at=seven_reset,
        model=str(model_name),
    )
