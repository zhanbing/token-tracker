"""Antigravity CLI (agy) adapter：brain/<cid>/.system_generated/logs/transcript.jsonl → UsageEntry。"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from token_tracker.adapters import antigravity
from token_tracker.adapters.util import antigravity_home


def _iso(seconds_ago: float = 60) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _model_step(
    step_index: int = 1,
    seconds_ago: float = 60,
    model: str = "gemini-3.6-flash",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    tool_calls: list[dict] | None = None,
    step_type: str = "PLANNER_RESPONSE",
) -> dict:
    row: dict = {
        "step_index": step_index,
        "source": "MODEL",
        "type": step_type,
        "status": "DONE",
        "created_at": _iso(seconds_ago),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
    }
    if tool_calls is not None:
        row["tool_calls"] = tool_calls
    return row


def _write_transcript(brain_dir: Path, cid: str, rows: list[dict], filename: str = "transcript.jsonl") -> Path:
    path = brain_dir / cid / ".system_generated" / "logs" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _write_summaries_db(db_path: Path, mapping: dict[str, list[str]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, workspace_uris TEXT)")
        for cid, uris in mapping.items():
            conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, workspace_uris) VALUES (?, ?)",
                (cid, json.dumps(uris)),
            )
        conn.commit()
    finally:
        conn.close()


def _load(brain_dir: Path, db_path: Path, monkeypatch, hours_back: int = 0) -> list:
    monkeypatch.setattr(antigravity, "BRAIN_DIR", str(brain_dir))
    monkeypatch.setattr(antigravity, "CONVERSATION_SUMMARIES_DB", str(db_path))
    return antigravity.load_entries(hours_back)


def test_antigravity_home_env_precedence(monkeypatch):
    for var in ("ANTIGRAVITY_APP_DATA_DIR", "ANTIGRAVITY_HOME", "AGY_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert antigravity_home().endswith(".gemini/antigravity-cli") or antigravity_home().endswith(
        ".gemini\\antigravity-cli"
    )
    monkeypatch.setenv("AGY_HOME", "/tmp/agy-home")
    assert antigravity_home() == "/tmp/agy-home"
    monkeypatch.setenv("ANTIGRAVITY_HOME", "/tmp/antigravity-home")
    assert antigravity_home() == "/tmp/antigravity-home"
    monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", "/tmp/app-data-dir")
    assert antigravity_home() == "/tmp/app-data-dir"


def test_detect_uses_install_root_without_requiring_brain(tmp_path, monkeypatch):
    agy_dir = tmp_path / "antigravity-cli"
    monkeypatch.setattr(antigravity, "ANTIGRAVITY_DIR", str(agy_dir))
    assert antigravity.detect() is None
    agy_dir.mkdir()
    info = antigravity.detect()
    assert info is not None and info.id == "antigravity" and info.name == "Antigravity"


def test_model_steps_become_entries_and_use_db_workspace(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    db = tmp_path / "conversation_summaries.db"
    proj = tmp_path / "my project"
    (proj / ".git").mkdir(parents=True)
    uri = f"file://{str(proj).replace(' ', '%20')}"
    _write_summaries_db(db, {"conv-1": [uri]})

    _write_transcript(brain, "conv-1", [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": _iso(130), "content": "hi"},
        _model_step(1, 120, "gemini-3.6-flash", input_tokens=10602, output_tokens=312, cache_read_tokens=0),
        {"step_index": 2, "source": "MODEL", "type": "GENERIC", "created_at": _iso(110), "content": "tool out"},
        _model_step(
            3, 60, "gemini-3.6-flash",
            input_tokens=500, output_tokens=120, cache_read_tokens=10603, cache_creation_tokens=40,
        ),
    ])
    # transcript_full.jsonl 不应被重复扫入
    _write_transcript(brain, "conv-1", [
        _model_step(1, 120, "gemini-3.6-flash", input_tokens=99999, output_tokens=999),
    ], filename="transcript_full.jsonl")

    entries = _load(brain, db, monkeypatch)
    assert len(entries) == 2
    first, second = entries
    assert first.agent_id == "antigravity"
    assert first.session_id == "conv-1"
    assert first.project == "my project"
    assert first.model == "gemini-3.6-flash"
    assert first.input_tokens == 10602
    assert first.output_tokens == 312
    assert first.cache_read_tokens == 0
    assert first.cost_usd is None
    assert second.cache_read_tokens == 10603
    assert second.cache_creation_tokens == 40
    assert first.dedup_key != second.dedup_key


def test_project_fallback_strips_json_quotes_and_skips_gemini_internal_paths(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    db = tmp_path / "missing.db"
    proj_file = tmp_path / "demo-repo" / "src" / "app.py"
    proj_file.parent.mkdir(parents=True)
    proj_file.write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "demo-repo" / ".git").mkdir()

    # 真实 transcript 中 tool_calls.args 常见 JSON 二次引号包裹，且首条可能是 ~/.gemini 技能文件
    _write_transcript(brain, "conv-quoted", [
        _model_step(1, 120, input_tokens=100, output_tokens=20, tool_calls=[
            {"name": "read_file", "args": {"Path": '"/Users/tester/.gemini/antigravity-cli/builtin/SKILL.md"'}},
        ]),
        _model_step(2, 60, input_tokens=200, output_tokens=30, tool_calls=[
            {"name": "read_file", "args": {"Path": json.dumps(str(proj_file))}},
        ]),
    ])

    entries = _load(brain, db, monkeypatch)
    assert len(entries) == 2
    # 单遍解析后回填同会话所有 entry 的 project，避免首条停留在 unknown
    assert [e.project for e in entries] == ["demo-repo", "demo-repo"]


def test_project_fallback_uses_parent_dir_for_non_git_file(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    db = tmp_path / "missing.db"
    loose_file = tmp_path / "loose-proj" / "notes.md"
    loose_file.parent.mkdir(parents=True)
    loose_file.write_text("hello\n", encoding="utf-8")

    _write_transcript(brain, "conv-loose", [
        _model_step(1, 60, input_tokens=10, output_tokens=5, tool_calls=[
            {"name": "read_file", "args": {"Path": json.dumps(str(loose_file))}},
        ]),
    ])

    entries = _load(brain, db, monkeypatch)
    assert entries[0].project == "loose-proj"


def test_zero_bool_malformed_and_duplicate_steps_handled(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    db = tmp_path / "corrupt.db"
    db.write_text("not sqlite", encoding="utf-8")  # 损坏 DB 安全回退

    path = _write_transcript(brain, "conv-edge", [
        _model_step(1, 100, input_tokens=0, output_tokens=0),  # 全 0 跳过
        {"step_index": 2, "source": "MODEL", "created_at": _iso(90), "input_tokens": True, "output_tokens": False},
        {"step_index": 3, "source": "MODEL", "created_at": "bad-ts", "input_tokens": 50, "output_tokens": 10},
        _model_step(4, 80, model="", input_tokens=50, output_tokens=10),
        _model_step(4, 79, model="dup", input_tokens=999, output_tokens=999),  # 同 step_index 去重
        # 无时区时间戳补齐 UTC，排序与 cutoff 比较不抛 TypeError
        {"step_index": 5, "source": "MODEL", "created_at": "2026-11-25T10:00:00", "input_tokens": 10, "output_tokens": 1},
    ])
    with open(path, "a", encoding="utf-8") as f:
        f.write("{broken json\n")

    entries = _load(brain, db, monkeypatch)
    assert len(entries) == 2
    by_id = {e.message_id: e for e in entries}
    assert by_id["conv-edge:4"].input_tokens == 50
    assert by_id["conv-edge:4"].model == "unknown"
    assert by_id["conv-edge:5"].timestamp.tzinfo is not None


def test_hours_back_and_load_recent_entries(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    db = tmp_path / "missing.db"
    _write_transcript(brain, "conv-span", [
        _model_step(1, 3600 * 10, input_tokens=100, output_tokens=1),
        _model_step(2, 60, input_tokens=200, output_tokens=1),
    ])
    monkeypatch.setattr(antigravity, "BRAIN_DIR", str(brain))
    monkeypatch.setattr(antigravity, "CONVERSATION_SUMMARIES_DB", str(db))

    # hours_back 按事件时间过滤
    windowed = antigravity.load_entries(hours_back=1)
    assert [e.input_tokens for e in windowed] == [200]

    # load_recent_entries 保留完整会话供 tt sessions 算真实 start_time
    full = antigravity.load_recent_entries(datetime.now(UTC) - timedelta(hours=1))
    assert [e.input_tokens for e in full] == [100, 200]


def test_workspace_uri_to_path_handles_windows_drive():
    assert antigravity._workspace_uri_to_path("file:///C:/Users/alice/my%20repo") == "C:/Users/alice/my repo"
    assert antigravity._workspace_uri_to_path("file:///Users/alice/repo") == "/Users/alice/repo"
    assert antigravity._workspace_uri_to_path("/plain/path") == "/plain/path"


def test_load_rate_limits_from_antigravity_status_file(tmp_path, monkeypatch):
    from token_tracker import config

    status_file = tmp_path / "tt-antigravity-status.json"
    monkeypatch.setattr(config, "ANTIGRAVITY_STATUS_FILE", str(status_file))
    assert antigravity.load_rate_limits() is None

    future = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    status_file.write_text(json.dumps({
        "_received_at": datetime.now(UTC).isoformat(),
        "model": {"id": "gemini-3.6-flash", "display_name": "Gemini 3.6 Flash"},
        "quota": {
            "gemini-5h": {"remaining_fraction": 0.75, "reset_time": future},
            "gemini-weekly": {"remaining_fraction": 0.40, "reset_time": past},  # 已过重置 → 归零
            "3p-weekly": {"remaining_fraction": 0.10, "reset_time": future},
        },
    }), encoding="utf-8")

    rl = antigravity.load_rate_limits()
    assert rl is not None
    assert rl.model == "Gemini 3.6 Flash"
    assert rl.five_hour_pct == 25.0
    assert rl.seven_day_pct == 0.0  # 过期窗口归零，且优先选了 gemini 桶而非 3p 桶
    assert [g.label for g in rl.groups] == ["Gemini", "Claude/GPT"]
    assert rl.groups[0].five_hour_pct == 25.0 and rl.groups[0].seven_day_pct == 0.0
    assert rl.groups[1].seven_day_pct == 90.0
