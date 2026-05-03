"""Session persistence tools — save, load, list, delete multi-turn conversations."""

import json
import re

from localforge.paths import sessions_dir
from localforge.tools import tool_handler
from localforge.tools.chat import _conversations


def _sanitize_topic(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip())[:80]


SESSIONS_DIR = sessions_dir()


@tool_handler(
    name="save_session",
    description=("Save a multi-turn conversation to disk so it survives server restarts. Sessions are stored as JSON."),
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session ID to save (must exist in memory)"},
        },
        "required": ["session_id"],
    },
)
async def save_session(args: dict) -> str:
    import time

    session_id = args["session_id"]
    if session_id not in _conversations:
        available = list(_conversations.keys())
        return f"Session '{session_id}' not found in memory. Available: {', '.join(available) or '(none)'}"

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = _sanitize_topic(session_id)
    path = SESSIONS_DIR / f"{safe_id}.json"

    from localforge import config as cfg

    data = {
        "session_id": session_id,
        "messages": _conversations[session_id],
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "turns": len(_conversations[session_id]) // 2,
        "model": cfg.MODEL,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return f"Saved session '{session_id}' ({data['turns']} turns) to {path}"


@tool_handler(
    name="load_session",
    description=(
        "Load a previously saved multi-turn conversation from disk into memory. "
        "After loading, use multi_turn_chat with action='continue' to resume."
    ),
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session ID to load"},
        },
        "required": ["session_id"],
    },
)
async def load_session(args: dict) -> str:
    safe_id = _sanitize_topic(args["session_id"])
    path = SESSIONS_DIR / f"{safe_id}.json"

    if not path.exists():
        available = [f.stem for f in SESSIONS_DIR.glob("*.json")] if SESSIONS_DIR.exists() else []
        return f"Session not found on disk. Available: {', '.join(sorted(available)) or '(none)'}"

    with open(path) as f:
        data = json.load(f)

    session_id = data["session_id"]
    _conversations[session_id] = data["messages"]
    turns = data.get("turns", len(data["messages"]) // 2)
    saved_at = data.get("saved_at", "unknown")
    saved_model = data.get("model", "unknown")

    return (
        f"Loaded session '{session_id}' ({turns} turns, saved {saved_at}, model: {saved_model}). "
        f"Use multi_turn_chat(action='continue', session_id='{session_id}', message='...') to resume."
    )


@tool_handler(
    name="list_sessions",
    description="List all saved multi-turn conversation sessions (on disk and in memory).",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_sessions(args: dict) -> str:
    in_mem = list(_conversations.keys())

    on_disk = []
    if SESSIONS_DIR.exists():
        for f in sorted(SESSIONS_DIR.glob("*.json")):
            try:
                with open(f) as fp:
                    data = json.load(fp)
                sid = data.get("session_id", f.stem)
                turns = data.get("turns", "?")
                saved = data.get("saved_at", "?")
                loaded = " (loaded)" if sid in _conversations else ""
                on_disk.append(f"  {sid}: {turns} turns, saved {saved}{loaded}")
            except Exception:
                on_disk.append(f"  {f.stem}: (corrupt)")

    lines = []
    if on_disk:
        lines.append(f"Saved sessions ({len(on_disk)}):")
        lines.extend(on_disk)
    else:
        lines.append("No saved sessions on disk.")

    mem_only = [s for s in in_mem if not any(s in d for d in on_disk)]
    if mem_only:
        lines.append(f"\nIn-memory only (not saved): {', '.join(mem_only)}")

    return "\n".join(lines)


@tool_handler(
    name="delete_session",
    description="Delete a saved session from disk (and optionally from memory).",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session ID to delete"},
            "from_memory": {
                "type": "boolean",
                "description": "Also remove from in-memory conversations (default: true)",
            },
        },
        "required": ["session_id"],
    },
)
async def delete_session(args: dict) -> str:
    session_id = args["session_id"]
    safe_id = _sanitize_topic(session_id)
    path = SESSIONS_DIR / f"{safe_id}.json"

    deleted = []
    if path.exists():
        path.unlink()
        deleted.append("disk")

    if args.get("from_memory", True) and session_id in _conversations:
        del _conversations[session_id]
        deleted.append("memory")

    if not deleted:
        return f"Session '{session_id}' not found on disk or in memory."
    return f"Deleted session '{session_id}' from {' and '.join(deleted)}."
