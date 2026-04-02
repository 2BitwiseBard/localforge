"""Chat and conversation tools."""

import asyncio
from typing import Any

from localforge import config as cfg
from localforge.client import chat, _client
from localforge.chunking import BUILTIN_GRAMMARS
from localforge.paths import sessions_dir
from localforge.tools import tool_handler

import json

# Multi-turn conversation state (in-memory, resets on server restart)
_conversations: dict[str, list[dict[str, str]]] = {}
MAX_CONVERSATION_TURNS = 20


@tool_handler(
    name="local_chat",
    description=(
        "Freeform prompt to the local model — brainstorming, explanations, Q&A, anything. "
        "Uses the current context if set. Supports optional GBNF grammar constraints."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Your prompt or question"},
            "system": {"type": "string", "description": "Optional system message override (replaces context preamble)"},
            "grammar": {"type": "string", "description": "Optional GBNF grammar constraint. Built-in: 'json', 'json_array', 'boolean'. Or provide a custom GBNF string."},
        },
        "required": ["prompt"],
    },
)
async def local_chat(args: dict) -> str:
    system = args.get("system") or cfg.get_system_preamble()
    kwargs: dict[str, Any] = {}
    grammar = args.get("grammar")
    if grammar:
        kwargs["grammar_string"] = BUILTIN_GRAMMARS.get(grammar, grammar)
    return await chat(args["prompt"], system=system, **kwargs)


@tool_handler(
    name="multi_turn_chat",
    description=(
        "Stateful multi-turn conversation with the local model. "
        "Actions: new (start fresh), continue (add to existing), history (show messages), list (show all sessions). "
        "Conversations persist in memory until server restart. Use save_session/load_session for disk persistence."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["new", "continue", "history", "list"], "description": "Action to perform"},
            "session_id": {"type": "string", "description": "Session identifier (auto-generated if omitted for 'new')"},
            "message": {"type": "string", "description": "User message (required for 'new' and 'continue')"},
            "system": {"type": "string", "description": "System message (optional, only for 'new')"},
        },
        "required": ["action"],
    },
)
async def multi_turn_chat(args: dict) -> str:
    action = args["action"]

    if action == "list":
        if not _conversations:
            return "No active conversations."
        lines = []
        for sid, msgs in sorted(_conversations.items()):
            turn_count = sum(1 for m in msgs if m["role"] == "user")
            preview = msgs[-1]["content"][:60] if msgs else "(empty)"
            lines.append(f"  {sid}: {turn_count} turns — {preview}...")
        return f"Active conversations ({len(_conversations)}):\n" + "\n".join(lines)

    if action == "new":
        message = args.get("message", "")
        if not message:
            return "Error: 'message' is required to start a new conversation"

        import time
        session_id = args.get("session_id") or f"session-{int(time.time())}"
        messages: list[dict[str, str]] = []

        system = args.get("system") or cfg.get_system_preamble()
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": message})

        # Get model response
        if cfg.MODEL is None:
            from localforge.client import resolve_model
            cfg.MODEL = await resolve_model()

        gen_params = cfg.get_generation_params(cfg.MODEL)
        suffix = cfg.get_system_suffix(cfg.MODEL)
        effective_system = system
        if suffix:
            effective_system = f"{system}\n\n{suffix}" if system else suffix

        api_messages = []
        if effective_system:
            api_messages.append({"role": "system", "content": effective_system})
        api_messages.append({"role": "user", "content": message})

        body = {"model": cfg.MODEL or "", "messages": api_messages, "stream": False, **gen_params}
        resp = await _client.post(f"{cfg.TGWUI_BASE}/chat/completions", json=body)
        resp.raise_for_status()
        assistant_msg = resp.json()["choices"][0]["message"]["content"]

        messages.append({"role": "assistant", "content": assistant_msg})
        _conversations[session_id] = messages

        return f"[{session_id}] {assistant_msg}"

    if action == "continue":
        session_id = args.get("session_id", "")
        message = args.get("message", "")
        if not session_id:
            return "Error: 'session_id' is required for continue"
        if not message:
            return "Error: 'message' is required for continue"
        if session_id not in _conversations:
            return f"Session '{session_id}' not found. Use action='list' to see active sessions."

        messages = _conversations[session_id]
        messages.append({"role": "user", "content": message})

        # Trim old turns if too long
        user_turns = sum(1 for m in messages if m["role"] == "user")
        while user_turns > MAX_CONVERSATION_TURNS:
            for i, m in enumerate(messages):
                if m["role"] != "system":
                    messages.pop(i)
                    if m["role"] == "user":
                        user_turns -= 1
                    break

        if cfg.MODEL is None:
            from localforge.client import resolve_model
            cfg.MODEL = await resolve_model()

        gen_params = cfg.get_generation_params(cfg.MODEL)
        body = {"model": cfg.MODEL or "", "messages": messages, "stream": False, **gen_params}
        resp = await _client.post(f"{cfg.TGWUI_BASE}/chat/completions", json=body)
        resp.raise_for_status()
        assistant_msg = resp.json()["choices"][0]["message"]["content"]

        messages.append({"role": "assistant", "content": assistant_msg})
        return f"[{session_id}] {assistant_msg}"

    if action == "history":
        session_id = args.get("session_id", "")
        if not session_id:
            return "Error: 'session_id' is required for history"
        if session_id not in _conversations:
            return f"Session '{session_id}' not found."
        messages = _conversations[session_id]
        lines = []
        for m in messages:
            role = m["role"].upper()
            content = m["content"][:200]
            lines.append(f"[{role}] {content}")
        return "\n\n".join(lines)

    return f"Unknown action: {action}"


@tool_handler(
    name="save_session",
    description="Save a multi-turn conversation to disk for later retrieval.",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session to save"},
        },
        "required": ["session_id"],
    },
)
async def save_session(args: dict) -> str:
    session_id = args["session_id"]
    if session_id not in _conversations:
        return f"Session '{session_id}' not found in memory."
    sd = sessions_dir()
    path = sd / f"{session_id}.json"
    messages = _conversations[session_id]
    with open(path, "w") as f:
        json.dump({"session_id": session_id, "messages": messages}, f, indent=2)
    return f"Session '{session_id}' saved ({len(messages)} messages) to {path}"


@tool_handler(
    name="load_session",
    description="Load a previously saved conversation from disk into memory.",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session to load"},
        },
        "required": ["session_id"],
    },
)
async def load_session(args: dict) -> str:
    session_id = args["session_id"]
    sd = sessions_dir()
    path = sd / f"{session_id}.json"
    if not path.exists():
        available = [f.stem for f in sd.glob("*.json")] if sd.exists() else []
        return f"Session file not found: {path}\nAvailable: {', '.join(sorted(available)) or '(none)'}"
    with open(path) as f:
        data = json.load(f)
    messages = data.get("messages", [])
    _conversations[session_id] = messages
    user_turns = sum(1 for m in messages if m["role"] == "user")
    return f"Loaded session '{session_id}': {len(messages)} messages, {user_turns} user turns"


@tool_handler(
    name="list_sessions",
    description="List all saved conversation sessions on disk.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_sessions(args: dict) -> str:
    sd = sessions_dir()
    if not sd.exists():
        return "No sessions directory."
    files = sorted(sd.glob("*.json"))
    if not files:
        return "No saved sessions."
    lines = []
    for f in files:
        size = f.stat().st_size
        in_memory = " (loaded)" if f.stem in _conversations else ""
        lines.append(f"  {f.stem} ({size} bytes){in_memory}")
    return f"Saved sessions ({len(files)}):\n" + "\n".join(lines)


@tool_handler(
    name="delete_session",
    description="Delete a saved conversation session from disk and memory.",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session to delete"},
        },
        "required": ["session_id"],
    },
)
async def delete_session(args: dict) -> str:
    session_id = args["session_id"]
    sd = sessions_dir()
    path = sd / f"{session_id}.json"
    deleted = []
    if path.exists():
        path.unlink()
        deleted.append("disk")
    if session_id in _conversations:
        del _conversations[session_id]
        deleted.append("memory")
    if not deleted:
        return f"Session '{session_id}' not found."
    return f"Deleted session '{session_id}' from: {', '.join(deleted)}"
