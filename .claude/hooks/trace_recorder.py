"""
Skill execution trace recorder — Claude Code Hook script.

Records structured execution traces for /require and /design skills
via UserPromptExpansion / Stop / UserPromptSubmit / PostToolUse events.

Usage: python trace_recorder.py <expansion|stop|submit|post_tool>

State file: .claude/traces/.active
Trace dir:  .claude/traces/{skill_name}/YYYYMMDD_HHmmss_{skill_name}.json
"""

import json
import sys
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────

_project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
_traces_dir = _project_dir / ".claude" / "traces"
_active_file = _traces_dir / ".active"

SKILLS = {"require", "design"}


# ── Helpers ────────────────────────────────────────────────────────

def _read_stdin() -> dict:
    """Read hook input JSON from stdin (bytes mode, Windows-safe)."""
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return {}
    try:
        text = raw.decode("utf-8", errors="replace")
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _read_active() -> dict | None:
    """Read the .active state file, return None if missing/invalid."""
    if not _active_file.exists():
        return None
    try:
        raw = _active_file.read_bytes()
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None


def _atomic_write(path: Path, content: str) -> None:
    """Atomic write via temp file + rename (Windows-safe, handles surrogates)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_bytes = content.encode("utf-8", errors="surrogatepass")
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_active(data: dict) -> None:
    """Write the .active state file."""
    _atomic_write(
        _active_file,
        json.dumps(data, ensure_ascii=False, indent=2),
    )


def _remove_active() -> None:
    """Remove the .active state file."""
    try:
        _active_file.unlink()
    except OSError:
        pass


def _read_trace(path: str) -> dict:
    """Read a trace JSON file."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = p.read_bytes()
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_trace(path: str, data: dict) -> None:
    """Write a trace JSON file (atomic)."""
    _atomic_write(
        Path(path),
        json.dumps(data, ensure_ascii=False, indent=2),
    )


def _now_iso() -> str:
    """Current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _finalize_trace(active: dict) -> None:
    """Close out an active trace: set finished_at, extract metadata, remove .active."""
    trace_path = active.get("trace_file", "")
    trace = _read_trace(trace_path)
    if not trace:
        _remove_active()
        return

    trace["finished_at"] = _now_iso()

    # Extract final_doc_path from Write/Edit calls to .claude/md/
    doc_paths = set()
    related = set()
    for rnd in trace.get("rounds", []):
        for dp in rnd.get("doc_update_paths", []):
            if dp.endswith(".md"):
                doc_paths.add(dp)
        for tc in rnd.get("tools_called", []):
            inp = tc.get("tool_input", {})
            fp = inp.get("file_path", "")
            if fp and ".claude/md/" not in fp:
                related.add(fp)

    # The most recently created .md under .claude/md/ is likely the final doc
    if doc_paths:
        trace["final_doc_path"] = sorted(doc_paths)[-1]
    if related:
        trace["related_files"] = sorted(related)

    _write_trace(trace_path, trace)
    _remove_active()


def _get_or_create_current_round(trace: dict) -> dict:
    """Get the last round, or create a new one if the last is closed."""
    rounds = trace.get("rounds", [])
    if rounds and not rounds[-1].get("_closed"):
        return rounds[-1]

    new_round = {
        "round_number": len(rounds),
        "started_at": _now_iso(),
        "skill_output_raw": "",
        "user_input_raw": "",
        "tools_called": [],
        "doc_updated": False,
        "doc_update_paths": [],
    }
    rounds.append(new_round)
    trace["rounds"] = rounds
    return new_round


# ── Event handlers ─────────────────────────────────────────────────

def _detect_skill(data: dict) -> str | None:
    """Extract skill name from hook data.

    UserPromptExpansion provides 'prompt' (expanded text) but not 'command_name'.
    We detect the skill by checking which known skill name appears in the prompt.
    """
    # Try explicit fields first (in case future Claude versions provide them)
    for field in ("command_name", "skill_name"):
        val = data.get(field, "")
        if val in SKILLS:
            return val

    # Fallback: scan the prompt text for skill markers
    prompt = data.get("prompt", "")
    for skill in SKILLS:
        # Match /require or /design as a word boundary
        if f"/{skill}" in prompt or f" {skill} " in prompt:
            return skill

    return None


def handle_expansion(data: dict) -> None:
    """UserPromptExpansion — skill triggered, create trace."""
    skill_name = _detect_skill(data)
    if not skill_name:
        return

    session_id = data.get("session_id", "unknown")

    # Finalize any lingering active trace
    active = _read_active()
    if active:
        _finalize_trace(active)

    # Create new trace
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    trace_filename = f"{timestamp}_{skill_name}.json"
    trace_dir = _traces_dir / skill_name
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = str(trace_dir / trace_filename)

    trace = {
        "skill_name": skill_name,
        "session_id": session_id,
        "started_at": now.isoformat(),
        "finished_at": None,
        "final_doc_path": None,
        "related_files": [],
        "rounds": [],
    }

    # Pre-create round 0
    trace["rounds"].append({
        "round_number": 0,
        "started_at": now.isoformat(),
        "skill_output_raw": "",
        "user_input_raw": "",
        "tools_called": [],
        "doc_updated": False,
        "doc_update_paths": [],
    })

    _write_trace(trace_path, trace)
    _write_active({
        "skill": skill_name,
        "started_at": now.isoformat(),
        "trace_file": trace_path,
    })


def handle_stop(data: dict) -> None:
    """Stop — Claude finished a reply, capture skill output."""
    active = _read_active()
    if not active:
        return

    trace_path = active.get("trace_file", "")
    trace = _read_trace(trace_path)
    if not trace:
        return

    msg = data.get("last_assistant_message", "")
    rnd = _get_or_create_current_round(trace)

    # Append to skill_output_raw (a single Stop closes the round)
    if rnd.get("skill_output_raw"):
        rnd["skill_output_raw"] += "\n\n" + msg
    else:
        rnd["skill_output_raw"] = msg

    # Mark round as closed
    rnd["_closed"] = True

    _write_trace(trace_path, trace)


def handle_submit(data: dict) -> None:
    """UserPromptSubmit — user sent a message, capture input."""
    active = _read_active()
    if not active:
        return

    trace_path = active.get("trace_file", "")
    trace = _read_trace(trace_path)
    if not trace:
        return

    prompt = data.get("prompt", "")

    # Get current round — if last round is closed, create a new one
    rnd = _get_or_create_current_round(trace)

    if rnd.get("user_input_raw"):
        rnd["user_input_raw"] += "\n\n" + prompt
    else:
        rnd["user_input_raw"] = prompt

    _write_trace(trace_path, trace)


def handle_post_tool(data: dict) -> None:
    """PostToolUse — tool call completed, record it."""
    active = _read_active()
    if not active:
        return

    trace_path = active.get("trace_file", "")
    trace = _read_trace(trace_path)
    if not trace:
        return

    rnd = _get_or_create_current_round(trace)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_response = data.get("tool_response", "")

    tool_record = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": _truncate_response(tool_response),
        "called_at": _now_iso(),
    }
    rnd["tools_called"].append(tool_record)

    # Detect doc updates (Write/Edit to .claude/md/)
    if tool_name in ("Write", "Edit"):
        fp = tool_input.get("file_path", "")
        if ".claude/md/" in fp or fp.endswith(".md"):
            rnd["doc_updated"] = True
            if fp not in rnd.get("doc_update_paths", []):
                rnd.setdefault("doc_update_paths", []).append(fp)

    _write_trace(trace_path, trace)


def _truncate_response(resp, max_len: int = 5000) -> str:
    """Truncate tool response to avoid bloating trace files."""
    if isinstance(resp, dict):
        resp = json.dumps(resp, ensure_ascii=False)
    s = str(resp)
    if len(s) > max_len:
        return s[:max_len] + "... [truncated]"
    return s


# ── Main ───────────────────────────────────────────────────────────

HANDLERS = {
    "expansion": handle_expansion,
    "stop": handle_stop,
    "submit": handle_submit,
    "post_tool": handle_post_tool,
}


def _debug_dump(event_type: str, data: dict) -> None:
    """Append raw hook data to debug log for diagnostics."""
    debug_dir = _project_dir / ".claude" / "hooks"
    debug_file = debug_dir / "debug.log"
    debug_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    entry = json.dumps({
        "ts": ts,
        "event": event_type,
        "keys": list(data.keys()),
        "data": {k: (v if not isinstance(v, str) or len(v) < 200 else v[:200] + "...") for k, v in data.items()},
    }, ensure_ascii=False)
    with open(debug_file, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    event_type = sys.argv[1]
    handler = HANDLERS.get(event_type)
    if not handler:
        sys.exit(0)

    data = _read_stdin()

    # Always dump raw data for diagnostics
    _debug_dump(event_type, data if data else {})

    if not data:
        sys.exit(0)

    try:
        handler(data)
    except Exception as e:
        # Hooks must not crash the session — log error to stderr
        print(f"[trace_recorder] {event_type}: {e}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
