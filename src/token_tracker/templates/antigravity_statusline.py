#!/usr/bin/env python3
"""Antigravity CLI statusLine — 四行主题状态栏 + 状态持久化（tt-antigravity-status.json / tt-terminal-map.json）

布局与 Claude Code statusLine 同源同配色：
- L1: [项目](分支* +A -D ?U) | Total: <会话累计 token> | Cost: $<累计成本>
- L2: Limit: 5h: <bar> (reset) | 7d: <bar> (reset) | <window> Ctx: <bar>
- L3: Tokens: in X, out Y, cache Z
- L4: Model: <display_name>/<effort>/<cycle_mode> | Duration: <时长> | Plan: <plan_tier>

由 `tt setup` 生成并烘焙主题配色，勿手改。
"""
__version__ = "__ANTIGRAVITY_STATUSLINE_HOOK_VERSION__"
import json, os, re, subprocess, sys, tempfile, unicodedata
from datetime import datetime, timezone

STATUS_FILE = os.path.join(os.path.expanduser("~/.config/token-tracker"), "tt-antigravity-status.json")
TERMINAL_MAP_FILE = os.path.join(os.path.expanduser("~/.config/token-tracker"), "tt-terminal-map.json")
MAX_CACHED_SESSIONS = 20
MAX_TERMINAL_MAPPINGS = 20
ANSI_RE = re.compile(r'\033\[[0-9;]*m')

# 配色在 tt setup / update_hook / tt theme set 烘焙时注入（与 CC statusline 同源）：
# truecolor 优先，不支持的终端（如 macOS Terminal.app）自动降级到 256 色近似。
THEME_COLORS = __STATUSLINE_TRUECOLOR__
THEME_COLORS_256 = __STATUSLINE_COLOR256__
# 烘焙内置官方价（Gemini / Claude / GPT），仅在 stdin 未提供 cost 时按 transcript 增量估算会话等效成本。
PRICING = __ANTIGRAVITY_PRICING__
_GEMINI_4_INTRO_AT = datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()
_GEMINI_FLASH_CHANGE_AT = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()


def _supports_truecolor():
    return os.environ.get("COLORTERM", "") in ("truecolor", "24bit")


C = THEME_COLORS if _supports_truecolor() else THEME_COLORS_256

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def vlen(s):
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
               for ch in ANSI_RE.sub("", s))


def get_width(data=None):
    # Antigravity CLI 在 stdin payload 里自带实时 terminal_width，优先使用；
    # 缺失时依次回落 COLUMNS 环境变量、tty 探测和 116 兜底（与 CC statusline 同口径）。
    if isinstance(data, dict):
        tw = data.get("terminal_width")
        if isinstance(tw, int) and not isinstance(tw, bool) and tw > 0:
            return max(1, tw - 4)
    try:
        c = os.environ.get("COLUMNS")
        if c and c.isdigit():
            w = int(c)
            if w > 0:
                return max(1, w - 4)
    except Exception:
        pass
    try:
        return max(1, os.get_terminal_size(2).columns - 4)
    except Exception:
        pass
    if os.name != "nt":
        try:
            import fcntl, struct, termios
            with open('/dev/tty', 'r') as tty:
                res = fcntl.ioctl(tty, termios.TIOCGWINSZ, b'\x00' * 8)
                return max(1, struct.unpack('hh', res[:4])[1] - 4)
        except Exception:
            pass
    return 116


def color_by_pct(pct):
    return C["bar_ok"] if pct < 50 else C["bar_warn"] if pct < 80 else C["bar_danger"]


def fmt_tokens(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.0f}k"
    return str(n)


def progress_bar(value, bar_width=8):
    filled_char, empty_char = "█", "░"
    if value is None:
        return empty_char * bar_width + " n/a"
    pct = max(0.0, min(100.0, float(value)))
    filled = round(pct / 100 * bar_width)
    empty = bar_width - filled
    color = color_by_pct(pct)
    empty_str = f"{color}{empty_char * empty}{C['reset']}" if pct > 0 and empty else empty_char * empty
    return f"{color}{filled_char * filled}{C['reset']}{empty_str} {C['label']}{pct:.0f}%{C['reset']}"


def fmt_duration(seconds):
    if seconds >= 86400:
        d, rem = int(seconds // 86400), int(seconds % 86400)
        return f"{d}d{rem // 3600}h"
    if seconds >= 3600:
        h, m = int(seconds // 3600), int((seconds % 3600) // 60)
        return f"{h}h{m}m"
    if seconds >= 60:
        return f"{int(seconds // 60)}min"
    return f"{int(seconds)}s"


def git_branch(cwd):
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=cwd,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=0.2,
        ).strip()
    except Exception:
        return ""
    if not branch:
        return ""
    try:
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=cwd,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=0.2,
        ).strip()
        if dirty:
            branch += "*"
    except Exception:
        pass
    return branch


def git_diff_stat(cwd):
    """相对 HEAD 的未提交增删行数 + 未跟踪文件数（超时压进 300ms 渲染预算）。"""
    added = deleted = 0
    try:
        out = subprocess.check_output(
            ["git", "diff", "HEAD", "--numstat"], cwd=cwd,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=0.2,
        )
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            a, d = parts[0], parts[1]
            if a.isdigit():
                added += int(a)
            if d.isdigit():
                deleted += int(d)
    except Exception:
        pass
    untracked = 0
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=0.15,
        )
        untracked = sum(1 for ln in out.splitlines() if ln.strip())
    except Exception:
        pass
    return added, deleted, untracked


def _read_prev_cache():
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        return {}
    cache = old.get("_transcript_cache") if isinstance(old, dict) else None
    return cache if isinstance(cache, dict) else {}


def _int_pos(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _resolve_pricing(model):
    if not isinstance(model, str) or not model:
        return None, None
    ml = model.lower()
    if ml in PRICING:
        return ml, PRICING[ml]
    for k, info in PRICING.items():
        if k.lower() == ml:
            return k, info
    matches = [k for k in PRICING if (kl := k.lower()) != ml and ml.startswith(kl) and ml[len(kl)] == "-"]
    if matches:
        best = max(matches, key=len)
        return best, PRICING[best]
    return None, None


def _step_cost(model, inp, out, cr, cc, ts_epoch):
    key, info = _resolve_pricing(model)
    if not info:
        return 0.0
    if key in ("gemini-4-argon", "gemini-4-carbon") and ts_epoch is not None and ts_epoch >= _GEMINI_4_INTRO_AT:
        info = {"input_cost_per_token": 4e-6, "output_cost_per_token": 20e-6, "cache_read_input_token_cost": 0.2e-6}
    elif key in ("gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash") and ts_epoch is not None and ts_epoch >= _GEMINI_FLASH_CHANGE_AT:
        info = {"input_cost_per_token": 1.5e-6, "output_cost_per_token": 7.5e-6, "cache_read_input_token_cost": 0.15e-6}
    prompt_tokens = inp + cr + cc
    suffix = ""
    for threshold in (272_000, 200_000, 100_000):
        cand = f"_above_{threshold // 1000}k_tokens"
        if prompt_tokens > threshold and f"input_cost_per_token{cand}" in info:
            suffix = cand
            break
    ic = info.get(f"input_cost_per_token{suffix}", info.get("input_cost_per_token", 0)) or 0
    oc = info.get(f"output_cost_per_token{suffix}", info.get("output_cost_per_token", 0)) or 0
    crc = info.get(f"cache_read_input_token_cost{suffix}", info.get("cache_read_input_token_cost", ic * 0.1))
    ccc = info.get(f"cache_creation_input_token_cost{suffix}", info.get("cache_creation_input_token_cost", ic * 1.25))
    return inp * ic + out * oc + cr * crc + cc * ccc


def _update_transcript_totals(session_id, path, cache):
    """按 byte offset 增量累计 Antigravity transcript.jsonl 的会话总 token、估算成本与起始时间戳。

    300ms 高频刷新下：文件未增长直接 O(1) 命中缓存，新增时只读尾部增量；文件被截断则重扫。
    返回 (total_tokens, estimated_cost_usd, started_at_ts | None)。
    """
    if not path:
        return 0, 0.0, None
    key = session_id or path
    try:
        st = os.stat(path)
    except OSError:
        return 0, 0.0, None

    entry = cache.get(key)
    if (
        not isinstance(entry, dict)
        or entry.get("path") != path
        or entry.get("offset", 0) > st.st_size
        or "cost" not in entry
    ):
        entry = {"path": path, "offset": 0, "mtime_ns": 0, "total": 0, "cost": 0.0, "seen": [], "started_at": None}

    if entry.get("offset") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns:
        cache.pop(key, None)
        cache[key] = entry
        return entry.get("total", 0), float(entry.get("cost", 0.0)), entry.get("started_at")

    offset = entry.get("offset", 0)
    total = entry.get("total", 0)
    est_cost = float(entry.get("cost", 0.0))
    seen = set(entry.get("seen") or [])
    started_at = entry.get("started_at")
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        last_nl = chunk.rfind(b"\n")
        if last_nl >= 0:
            raw_lines = chunk[:last_nl + 1].decode("utf-8", errors="replace").splitlines()
            offset += last_nl + 1
        elif st.st_size == offset:
            raw_lines = []
        else:
            raw_lines = []
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            ts_epoch = None
            if isinstance(row.get("created_at"), str):
                try:
                    ts = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    ts_epoch = ts.timestamp()
                    if started_at is None:
                        started_at = ts_epoch
                except ValueError:
                    pass
            if row.get("source") != "MODEL":
                continue
            step_idx = row.get("step_index")
            step_key = str(step_idx) if step_idx is not None else ""
            if step_key and step_key in seen:
                continue
            inp = _int_pos(row.get("input_tokens"))
            out = _int_pos(row.get("output_tokens"))
            cr = _int_pos(row.get("cache_read_tokens"))
            cc = _int_pos(row.get("cache_creation_tokens"))
            step_total = inp + out + cr + cc
            if step_total:
                if step_key:
                    seen.add(step_key)
                total += step_total
                est_cost += _step_cost(row.get("model"), inp, out, cr, cc, ts_epoch)
    except OSError:
        return total, est_cost, started_at

    entry = {
        "path": path,
        "offset": offset,
        "mtime_ns": st.st_mtime_ns,
        "total": total,
        "cost": est_cost,
        "seen": list(seen)[-2000:],
        "started_at": started_at,
    }
    cache.pop(key, None)
    cache[key] = entry
    for old_key in list(cache)[:-MAX_CACHED_SESSIONS]:
        del cache[old_key]
    return total, est_cost, started_at


def save_status(data, now, cache):
    out = dict(data)
    out["_received_at"] = now.isoformat()
    out["_transcript_cache"] = cache
    tmp = None
    try:
        parent = os.path.dirname(STATUS_FILE)
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f)
        os.replace(tmp, STATUS_FILE)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def record_terminal_map(session_id):
    """记录当前 Antigravity 会话所在终端窗格（供 `tt sidebar` 点击跳转），与 Codex/Kimi 共用映射文件。"""
    term = {}
    if os.environ.get("ITERM_SESSION_ID"):
        term["iterm"] = os.environ["ITERM_SESSION_ID"]
    if os.environ.get("TMUX_PANE"):
        term["tmux"] = os.environ["TMUX_PANE"]
    if not session_id or not term:
        return
    tmp = None
    lock = None
    try:
        parent = os.path.dirname(TERMINAL_MAP_FILE)
        os.makedirs(parent, exist_ok=True)
        lock = open(TERMINAL_MAP_FILE + ".lock", "a+", encoding="utf-8")
        try:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            with open(TERMINAL_MAP_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        term_map = data.get("_terminal_map") if isinstance(data, dict) else None
        if not isinstance(term_map, dict):
            term_map = {}
        if term_map.get(session_id) == term:
            return
        term_map.pop(session_id, None)
        term_map[session_id] = term
        for key in list(term_map)[:-MAX_TERMINAL_MAPPINGS]:
            del term_map[key]
        fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"_terminal_map": term_map}, f)
        os.replace(tmp, TERMINAL_MAP_FILE)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if lock:
            lock.close()


def _quota_reset_seconds(entry, now):
    sec = entry.get("reset_in_seconds")
    if isinstance(sec, (int, float)) and not isinstance(sec, bool) and sec > 0:
        return int(sec)
    raw = entry.get("reset_time") or entry.get("resets_at")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        rem = int(raw) - int(now.timestamp())
        return rem if rem > 0 else 0
    if isinstance(raw, str) and raw:
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            rem = int(ts.timestamp()) - int(now.timestamp())
            return rem if rem > 0 else 0
        except ValueError:
            pass
    return 0


def _extract_quota_items(data, now):
    """从 Antigravity `quota`（或兼容 `rate_limits`）提取最多 2 个代表性配额项：[(label, used_pct, remain_s)]。"""
    items = []
    quota = data.get("quota")
    if isinstance(quota, dict) and quota:
        model_info = data.get("model")
        model_id = (model_info.get("id") or model_info.get("name") or "") if isinstance(model_info, dict) else str(model_info or "")
        ml = model_id.lower()
        prefer_3p = ml.startswith(("claude", "gpt", "o1", "o3", "o4"))

        def _score(name):
            nl = name.lower()
            is_3p = "3p" in nl or "claude" in nl or "gpt" in nl
            family_match = 0 if (is_3p == prefer_3p) else 1
            window_rank = 0 if "5h" in nl else 1 if ("week" in nl or "7d" in nl) else 2
            return (family_match, window_rank, nl)

        picked_windows = set()
        for bucket_name in sorted(quota, key=_score):
            entry = quota.get(bucket_name)
            if not isinstance(entry, dict) or entry.get("disabled"):
                continue
            rem_frac = entry.get("remaining_fraction")
            used_pct = entry.get("used_percentage")
            if isinstance(rem_frac, (int, float)) and not isinstance(rem_frac, bool):
                pct = max(0.0, min(100.0, (1.0 - float(rem_frac)) * 100.0))
            elif isinstance(used_pct, (int, float)) and not isinstance(used_pct, bool):
                pct = max(0.0, min(100.0, float(used_pct)))
            else:
                continue
            nl = bucket_name.lower()
            if "5h" in nl:
                label = "5h"
            elif "week" in nl or "7d" in nl:
                label = "7d"
            else:
                label = bucket_name[:12]
            if label in picked_windows:
                continue
            picked_windows.add(label)
            items.append((label, pct, _quota_reset_seconds(entry, now)))
            if len(items) >= 2:
                break
        return items

    rl = data.get("rate_limits")
    if isinstance(rl, dict):
        for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
            entry = rl.get(key)
            if not isinstance(entry, dict):
                continue
            pct = entry.get("used_percentage")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                items.append((label, float(pct), _quota_reset_seconds(entry, now)))
    return items


def render(data, now, transcript_total=0, transcript_cost=0.0, started_at=None):
    W = get_width(data)
    ctx = data.get("context_window") or {}
    cost = data.get("cost") or {}
    bar_w = 8 if W >= 100 else 6 if W >= 60 else 4

    # --- Line 1: [项目](分支* +A -D ?U) | Total | Cost ---
    line1 = []
    ws = data.get("workspace") or {}
    project = ws.get("project_dir") or ws.get("current_dir") or data.get("cwd") or ""
    if isinstance(project, str) and project:
        name = os.path.basename(project.rstrip("/\\")) or project
        vcs = data.get("vcs") if isinstance(data.get("vcs"), dict) else {}
        branch = vcs.get("branch") if isinstance(vcs.get("branch"), str) else ""
        dirty_flag = bool(vcs.get("dirty")) if branch else False
        if not branch:
            branch = git_branch(project)
        elif dirty_flag and not branch.endswith("*"):
            branch += "*"
        if branch:
            added, deleted, untracked = git_diff_stat(project)
            if (added or deleted) and not branch.endswith("*"):
                branch += "*"
            inner = f"{C['branch']}{branch}{C['reset']}"
            if added:
                inner += f" {C['added']}+{added}{C['reset']}"
            if deleted:
                inner += f" {C['deleted']}-{deleted}{C['reset']}"
            if untracked:
                inner += f" {C['untracked']}?{untracked}{C['reset']}"
            line1.append(f"\033[1m{C['project']}[{name}]{C['reset']}({inner})")
        else:
            line1.append(f"\033[1m{C['project']}[{name}]{C['reset']}")

    total_tokens = transcript_total or (_int_pos(ctx.get("total_input_tokens")) + _int_pos(ctx.get("total_output_tokens")))
    if total_tokens:
        line1.append(f"{C['total']}Total: {fmt_tokens(total_tokens)}{C['reset']}")

    usd = cost.get("total_cost_usd")
    if usd is None:
        usd = cost.get("total_usd")
    if not (isinstance(usd, (int, float)) and not isinstance(usd, bool)) and transcript_cost > 0:
        usd = transcript_cost
    if isinstance(usd, (int, float)) and not isinstance(usd, bool):
        line1.append(f"{C['total']}Cost: ${usd:.2f}{C['reset']}")

    while len(line1) > 1 and vlen(" | ".join(line1)) > W:
        line1.pop()

    # --- Line 2: Limit: 5h | 7d | Ctx ---
    rl_parts = []
    for label, pct, remain in _extract_quota_items(data, now):
        reset_str = f" \033[2m{C['label']}({fmt_duration(remain)}){C['reset']}" if remain > 0 else ""
        rl_parts.append((
            f"{C['label']}{label}:{C['reset']}{progress_bar(pct, bar_w)}{reset_str}",
            f"{C['label']}{label}:{C['reset']}{progress_bar(pct, bar_w)}",
            f"{C['label']}{label}:{pct:.0f}%{C['reset']}",
        ))

    ctx_parts = []
    used_pct = ctx.get("used_percentage")
    if isinstance(used_pct, (int, float)) and not isinstance(used_pct, bool):
        size = _int_pos(ctx.get("context_window_size")) or _int_pos(ctx.get("total_tokens"))
        prefix = f"{fmt_tokens(size)} Ctx:" if size else "Ctx:"
        ctx_parts = [
            f"{C['label']}{prefix}{C['reset']}{progress_bar(used_pct, bar_w)}",
            f"{C['label']}{prefix}{used_pct:.0f}%{C['reset']}",
        ]

    line2 = []
    if rl_parts or ctx_parts:
        for idx in (0, 1, 2):
            rl_seg = [p[idx] for p in rl_parts]
            ctx_seg = (ctx_parts[:1] if idx < 2 else ctx_parts[1:2]) if ctx_parts else []
            segs = rl_seg + ctx_seg
            if rl_seg:
                segs[0] = f"{C['label']}Limit:{C['reset']} {segs[0]}"
            if idx == 2 or vlen(" | ".join(segs)) <= W:
                line2 = segs
                break

    # --- Line 3: Tokens（上下文窗口 / 本轮 in, out, cache 构成）---
    line3 = []
    cur = ctx.get("current_usage") if isinstance(ctx.get("current_usage"), dict) else {}
    cur_in = _int_pos(ctx.get("total_input_tokens")) or _int_pos(cur.get("input_tokens"))
    cur_out = _int_pos(ctx.get("total_output_tokens")) or _int_pos(cur.get("output_tokens"))
    cur_cache = _int_pos(cur.get("cache_read_input_tokens")) or _int_pos(cur.get("cache_read_tokens"))
    if cur_in or cur_out or cur_cache:
        tok = f"{C['tokens']}Tokens: in {fmt_tokens(cur_in)}, out {fmt_tokens(cur_out)}"
        if cur_cache:
            tok += f", cache {fmt_tokens(cur_cache)}"
        line3.append(tok + C['reset'])

    # --- Line 4: Model | Duration | Plan ---
    line4 = []
    model_obj = data.get("model")
    if isinstance(model_obj, dict):
        model_name = model_obj.get("display_name") or model_obj.get("id") or model_obj.get("name") or ""
        effort = model_obj.get("effort") or ""
    elif isinstance(model_obj, str):
        model_name, effort = model_obj, ""
    else:
        model_name, effort = "", ""
    if model_name:
        if effort:
            model_name = re.sub(r'\s*\(.*?\)', '', model_name).strip()
            model_name += f"/{effort}"
        cycle_mode = data.get("cycle_mode")
        if isinstance(cycle_mode, str) and cycle_mode:
            model_name += f"/{cycle_mode}"
        line4.append(f"{C['model']}Model: {model_name}{C['reset']}")

    duration_ms = cost.get("total_duration_ms")
    duration_s = 0.0
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool) and duration_ms > 0:
        duration_s = duration_ms / 1000.0
    elif isinstance(started_at, (int, float)) and started_at > 0:
        elapsed = now.timestamp() - started_at
        if elapsed >= 1:
            duration_s = elapsed
    if duration_s > 0:
        line4.append(f"{C['duration']}Duration: {fmt_duration(duration_s)}{C['reset']}")

    plan_tier = data.get("plan_tier")
    if isinstance(plan_tier, str) and plan_tier.strip():
        line4.append(f"{C['model']}Plan: {plan_tier.strip()}{C['reset']}")

    while len(line4) > 1 and vlen(" | ".join(line4)) > W:
        line4.pop()

    output = [" | ".join(line) for line in (line1, line2, line3, line4) if line]
    if output:
        print("\n".join(output))
        sys.stdout.flush()


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
    except Exception:
        return

    now = datetime.now(timezone.utc)
    session_id = data.get("conversation_id") or data.get("session_id") or ""
    if not isinstance(session_id, str):
        session_id = ""

    record_terminal_map(session_id)
    cache = _read_prev_cache()
    tpath = data.get("transcript_path")
    transcript_total, transcript_cost, started_at = _update_transcript_totals(
        session_id, tpath if isinstance(tpath, str) else "", cache
    )
    save_status(data, now, cache)
    render(data, now, transcript_total, transcript_cost, started_at)


if __name__ == "__main__":
    main()
