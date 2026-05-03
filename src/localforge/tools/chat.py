"""Chat and conversation tools."""

import json
from typing import Any

from localforge import config as cfg
from localforge.chunking import BUILTIN_GRAMMARS
from localforge.client import _client, chat
from localforge.tools import tool_handler

# Multi-turn conversation state (in-memory, resets on server restart)
_conversations: dict[str, list[dict[str, str]]] = {}
MAX_CONVERSATION_TURNS = 20
MAX_CONVERSATIONS = 50  # LRU eviction when exceeded


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
            "grammar": {
                "type": "string",
                "description": "Optional GBNF grammar constraint. Built-in: 'json', 'json_array', 'boolean'. Or provide a custom GBNF string.",
            },
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
            "action": {
                "type": "string",
                "enum": ["new", "continue", "history", "list"],
                "description": "Action to perform",
            },
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

        # LRU eviction: remove oldest conversations if over limit
        if len(_conversations) > MAX_CONVERSATIONS:
            # Evict the conversation with the oldest last message
            oldest_sid = min(
                _conversations,
                key=lambda sid: len(_conversations[sid]),  # fewest messages = least active
            )
            del _conversations[oldest_sid]

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

        # Trim old turns if too long (build new list to avoid mutation during iteration)
        user_turns = sum(1 for m in messages if m["role"] == "user")
        if user_turns > MAX_CONVERSATION_TURNS:
            trimmed = [m for m in messages if m["role"] == "system"]
            non_system = [m for m in messages if m["role"] != "system"]
            # Keep the most recent turns
            keep_count = MAX_CONVERSATION_TURNS * 2  # user + assistant pairs
            trimmed.extend(non_system[-keep_count:])
            messages[:] = trimmed
            _conversations[session_id] = messages

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
    name="text_complete",
    description=(
        "Raw text completion (no chat wrapping). Sends a prompt directly to the "
        "completions endpoint. Useful for code completion, fill-in-middle, and templates."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Prompt to complete"},
            "max_tokens": {"type": "integer", "description": "Max tokens to generate (default: 512)"},
            "temperature": {"type": "number", "description": "Temperature (optional, uses model default)"},
            "stop": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Stop sequences",
            },
        },
        "required": ["prompt"],
    },
)
async def text_complete(args: dict) -> str:
    gen_params = cfg.get_generation_params(cfg.MODEL)
    body: dict[str, Any] = {
        "model": cfg.MODEL or "",
        "prompt": args["prompt"],
        "max_tokens": args.get("max_tokens", 512),
        "stream": False,
    }
    if args.get("temperature") is not None:
        body["temperature"] = args["temperature"]
    else:
        body["temperature"] = gen_params.get("temperature", 0.7)
    if args.get("stop"):
        body["stop"] = args["stop"]

    resp = await _client.post(f"{cfg.TGWUI_BASE}/completions", json=body, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["text"]
    return text


@tool_handler(
    name="validated_chat",
    description=(
        "Chat with validation and auto-retry. Sends a prompt, checks the response "
        "against a validation mode, and retries once if validation fails. "
        "Modes: 'json' (must parse), 'code' (syntax check), 'answer' (self-verify), 'custom'."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The prompt to send"},
            "validation": {
                "type": "string",
                "enum": ["json", "code", "answer", "custom"],
                "description": "Validation mode",
            },
            "custom_check": {
                "type": "string",
                "description": "Custom validation prompt (for mode='custom'). Use {response} placeholder.",
            },
            "system": {"type": "string", "description": "Optional system message"},
            "max_retries": {"type": "integer", "description": "Max retry attempts (default: 1)"},
        },
        "required": ["prompt", "validation"],
    },
)
async def validated_chat(args: dict) -> str:
    import logging

    log = logging.getLogger("localforge")

    prompt = args["prompt"]
    validation = args["validation"]
    system = args.get("system") or cfg.get_system_preamble()
    max_retries = args.get("max_retries", 1)
    custom_check = args.get("custom_check", "")

    result = ""
    validation_error = ""

    for attempt in range(max_retries + 1):
        result = await chat(prompt, system=system, use_cache=(attempt == 0))

        is_valid = True
        validation_error = ""

        if validation == "json":
            cleaned = result.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            try:
                json.loads(cleaned.strip())
            except json.JSONDecodeError as e:
                is_valid = False
                validation_error = f"JSON parse error: {e}"

        elif validation == "code":
            issues = []
            if result.count("{") != result.count("}"):
                issues.append("mismatched braces")
            if result.count("(") != result.count(")"):
                issues.append("mismatched parentheses")
            if result.count("[") != result.count("]"):
                issues.append("mismatched brackets")
            if issues:
                is_valid = False
                validation_error = f"Syntax issues: {', '.join(issues)}"

        elif validation == "answer":
            verify_prompt = (
                f"Original question: {prompt}\n\n"
                f"Response: {result}\n\n"
                f"Does this response correctly and completely answer the question? "
                f"Reply with ONLY 'VALID' or 'INVALID: <reason>'."
            )
            verdict = await chat(verify_prompt, use_cache=False, max_tokens=100)
            if "INVALID" in verdict.upper():
                is_valid = False
                validation_error = verdict

        elif validation == "custom" and custom_check:
            check_prompt = custom_check.replace("{response}", result)
            verdict = await chat(check_prompt, use_cache=False, max_tokens=100)
            if any(w in verdict.upper() for w in ["FAIL", "INVALID", "ERROR", "NO", "FALSE"]):
                is_valid = False
                validation_error = verdict

        if is_valid:
            prefix = f"[validated: {validation}, attempt {attempt + 1}]\n\n" if attempt > 0 else ""
            return f"{prefix}{result}"

        log.info("Validation failed (attempt %d/%d): %s", attempt + 1, max_retries + 1, validation_error)
        if attempt < max_retries:
            prompt = (
                f"{args['prompt']}\n\n"
                f"IMPORTANT: Your previous response had this issue: {validation_error}\n"
                f"Please fix this and try again."
            )

    return f"[validation failed after {max_retries + 1} attempts: {validation_error}]\n\n{result}"
