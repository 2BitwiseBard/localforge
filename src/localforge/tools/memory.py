"""Scratchpad and persistent note tools."""

from localforge import config as cfg
from localforge.paths import notes_dir
from localforge.tools import tool_handler

# In-memory scratchpad (resets on server restart)
_scratchpad: dict[str, str] = {}


@tool_handler(
    name="scratchpad",
    description=(
        "In-memory session scratchpad for stashing intermediate results, plans, or snippets. "
        "Resets on server restart. Actions: write (set key=value), read (get key), list (all keys), clear (wipe all)."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["write", "read", "list", "clear"], "description": "Action to perform"},
            "key": {"type": "string", "description": "Key name (for write/read)"},
            "value": {"type": "string", "description": "Value to store (for write)"},
        },
        "required": ["action"],
    },
)
async def scratchpad(args: dict) -> str:
    action = args["action"]

    if action == "write":
        key = args.get("key", "")
        if not key:
            return "Error: 'key' is required for write"
        _scratchpad[key] = args.get("value", "")
        return f"Stored key '{key}' ({len(_scratchpad[key])} chars). Total keys: {len(_scratchpad)}"

    elif action == "read":
        key = args.get("key", "")
        if not key:
            return "Error: 'key' is required for read"
        if key not in _scratchpad:
            return f"Key '{key}' not found. Available: {', '.join(sorted(_scratchpad.keys())) or '(empty)'}"
        return _scratchpad[key]

    elif action == "list":
        if not _scratchpad:
            return "Scratchpad is empty."
        lines = [f"  {k} ({len(v)} chars)" for k, v in sorted(_scratchpad.items())]
        return f"Scratchpad keys ({len(_scratchpad)}):\n" + "\n".join(lines)

    elif action == "clear":
        count = len(_scratchpad)
        _scratchpad.clear()
        return f"Cleared {count} keys from scratchpad."

    return f"Unknown action: {action}"


@tool_handler(
    name="save_note",
    description=(
        "Persist a note to disk under the notes directory. "
        "Survives server restarts. Use for accumulating project knowledge, patterns, decisions."
    ),
    schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Note topic (used as filename, e.g. 'architecture-decisions')"},
            "content": {"type": "string", "description": "Markdown content to save"},
            "append": {"type": "boolean", "description": "Append to existing note instead of overwriting (default: false)"},
        },
        "required": ["topic", "content"],
    },
)
async def save_note(args: dict) -> str:
    notes_dir()  # ensure directory exists
    topic = cfg.sanitize_topic(args["topic"])
    path = cfg.safe_note_path(topic)
    if path is None:
        return "Error: invalid topic name"

    if args.get("append") and path.exists():
        existing = path.read_text(encoding="utf-8")
        path.write_text(existing + "\n\n---\n\n" + args["content"], encoding="utf-8")
    else:
        path.write_text(args["content"], encoding="utf-8")
    return f"Saved note '{topic}' ({len(args['content'])} chars) to {path}"


@tool_handler(
    name="recall_note",
    description="Read a saved note by topic",
    schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Note topic to recall"},
        },
        "required": ["topic"],
    },
)
async def recall_note(args: dict) -> str:
    nd = notes_dir()
    topic = cfg.sanitize_topic(args["topic"])
    path = cfg.safe_note_path(topic)
    if path is None:
        return "Error: invalid topic name"
    if not path.exists():
        available = [f.stem for f in nd.glob("*.md")] if nd.exists() else []
        return f"Note '{topic}' not found. Available: {', '.join(sorted(available)) or '(none)'}"
    return path.read_text(encoding="utf-8")


@tool_handler(
    name="list_notes",
    description="List all saved note topics",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_notes(args: dict) -> str:
    nd = notes_dir()
    if not nd.exists():
        return "No notes directory yet. Use save_note to create your first note."
    notes = sorted(nd.glob("*.md"))
    if not notes:
        return "No notes saved yet."
    lines = []
    for f in notes:
        size = f.stat().st_size
        lines.append(f"  {f.stem} ({size} bytes)")
    return f"Saved notes ({len(notes)}):\n" + "\n".join(lines)


@tool_handler(
    name="delete_note",
    description="Remove a saved note by topic",
    schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Note topic to delete"},
        },
        "required": ["topic"],
    },
)
async def delete_note(args: dict) -> str:
    topic = cfg.sanitize_topic(args["topic"])
    path = cfg.safe_note_path(topic)
    if path is None:
        return "Error: invalid topic name"
    if not path.exists():
        return f"Note '{topic}' not found."
    path.unlink()
    return f"Deleted note '{topic}'."
