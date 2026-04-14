"""Context, mode, and character tools."""

from pathlib import Path

from localforge import config as cfg
from localforge.client import resolve_model
from localforge.tools import tool_handler


@tool_handler(
    name="set_context",
    description=(
        "Configure the project context for all subsequent tool calls. "
        "Set language/project/rules to get tailored responses, or call with no arguments to reset to generic mode. "
        "Known combos (e.g. language='rust', project='quant-platform') auto-activate specialized preambles."
    ),
    schema={
        "type": "object",
        "properties": {
            "language": {"type": "string", "description": "Programming language (e.g. 'rust', 'python', 'typescript')"},
            "project": {"type": "string", "description": "Project name for known preamble lookup (e.g. 'quant-platform')"},
            "rules": {"type": "string", "description": "Freeform project rules/conventions to enforce"},
        },
        "required": [],
    },
)
async def set_context(args: dict) -> str:
    cfg._context.clear()
    if args.get("language"):
        cfg._context["language"] = args["language"]
    if args.get("project"):
        cfg._context["project"] = args["project"]
    if args.get("rules"):
        cfg._context["rules"] = args["rules"]

    preamble = cfg.get_system_preamble()
    if preamble:
        return f"Context set. Active preamble:\n\n{preamble}"
    return "Context cleared. Operating in generic mode (no language/project-specific rules)."


@tool_handler(
    name="set_mode",
    description=(
        "Switch the hub's operational mode. Each mode configures temperature, "
        "preferred model, system suffix, and max tokens. Available modes: "
        "development, research, creative, review, ops, learning. "
        "Use get_mode() to see current mode."
    ),
    schema={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["development", "research", "creative", "review", "ops", "learning"],
                "description": "Mode to activate",
            },
        },
        "required": ["mode"],
    },
)
async def set_mode(args: dict) -> str:
    mode_name = args["mode"]
    modes = cfg._config.get("modes", {})
    if mode_name not in modes:
        return f"Unknown mode: {mode_name}. Available: {', '.join(modes.keys())}"

    mode_cfg = modes[mode_name]
    cfg._current_mode = {"name": mode_name, **mode_cfg}

    if mode_cfg.get("temperature") is not None:
        cfg._runtime_overrides["temperature"] = mode_cfg["temperature"]
    if mode_cfg.get("max_tokens"):
        cfg._runtime_overrides["max_tokens"] = mode_cfg["max_tokens"]
    if mode_cfg.get("system_suffix") is not None:
        cfg._runtime_overrides["system_suffix"] = mode_cfg["system_suffix"]

    swap_msg = ""
    if mode_cfg.get("auto_swap") and mode_cfg.get("prefer_model"):
        current = cfg.MODEL or ""
        preferred = mode_cfg["prefer_model"]
        if not any(p.lower() in current.lower() for p in preferred):
            swap_msg = f"\nPreferred model: {preferred[0]} (use swap_model to load it)"

    return (
        f"Mode: {mode_name}\n"
        f"Temperature: {mode_cfg.get('temperature', 'default')}\n"
        f"Max tokens: {mode_cfg.get('max_tokens', 'default')}\n"
        f"System suffix: {mode_cfg.get('system_suffix', '(default)')[:60]}..."
        f"{swap_msg}"
    )


@tool_handler(
    name="get_mode",
    description="Show the current hub mode and its settings.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def get_mode(args: dict) -> str:
    if not cfg._current_mode:
        return "No mode set. Using default settings. Available modes: " + \
               ", ".join(cfg._config.get("modes", {}).keys())
    char_info = ""
    if cfg._current_character:
        char_info = f"\nCharacter: {cfg._current_character.get('name', 'default')}"
    return (
        f"Current mode: {cfg._current_mode.get('name', 'unknown')}\n"
        f"Temperature: {cfg._current_mode.get('temperature', 'default')}\n"
        f"Max tokens: {cfg._current_mode.get('max_tokens', 'default')}\n"
        f"Preferred model: {cfg._current_mode.get('prefer_model', ['any'])}\n"
        f"System suffix: {cfg._current_mode.get('system_suffix', '(none)')[:80]}"
        f"{char_info}"
    )


@tool_handler(
    name="set_character",
    description=(
        "Set an agent character/persona that applies to all chat interactions. "
        "Characters provide a system prompt that shapes the model's behavior. "
        "Combinable with modes. Available: default, code-reviewer, architect, "
        "brainstorm, teacher, devops, security."
    ),
    schema={
        "type": "object",
        "properties": {
            "character": {
                "type": "string",
                "description": "Character name to activate",
            },
        },
        "required": ["character"],
    },
)
async def set_character(args: dict) -> str:
    char_name = args["character"]
    characters = cfg._config.get("characters", {})
    if char_name not in characters:
        return f"Unknown character: {char_name}. Available: {', '.join(characters.keys())}"

    char_cfg = characters[char_name]
    cfg._current_character = {"name": char_name, **char_cfg}

    if char_cfg.get("temperature_override") is not None:
        cfg._runtime_overrides["temperature"] = char_cfg["temperature_override"]

    prompt_preview = char_cfg.get("system_prompt", "(none)")[:100]
    return f"Character: {char_cfg.get('name', char_name)}\nSystem prompt: {prompt_preview}..."


@tool_handler(
    name="list_characters",
    description="List all available agent characters/personas.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_characters(args: dict) -> str:
    characters = cfg._config.get("characters", {})
    if not characters:
        return "No characters configured in config.yaml."
    parts = []
    for key, char_cfg in characters.items():
        active = " (active)" if cfg._current_character.get("name") == key else ""
        temp = f", temp={char_cfg['temperature_override']}" if char_cfg.get("temperature_override") else ""
        parts.append(f"  {key}: {char_cfg.get('name', key)}{temp}{active}")
    return "Available characters:\n" + "\n".join(parts)


@tool_handler(
    name="list_modes",
    description="List all available hub modes.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_modes(args: dict) -> str:
    modes = cfg._config.get("modes", {})
    if not modes:
        return "No modes configured in config.yaml."
    parts = []
    for key, mode_cfg in modes.items():
        active = " (active)" if cfg._current_mode.get("name") == key else ""
        parts.append(
            f"  {key}: temp={mode_cfg.get('temperature', '?')}, "
            f"model={mode_cfg.get('prefer_model', ['any'])[0]}, "
            f"max_tokens={mode_cfg.get('max_tokens', '?')}{active}"
        )
    return "Available modes:\n" + "\n".join(parts)


@tool_handler(
    name="check_model",
    description=(
        "Check which model is currently loaded in text-generation-webui and verify connectivity. "
        "Also re-resolves the model name, so call this after swapping models in the webUI."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def check_model(args: dict) -> str:
    cfg.MODEL = None
    cfg.MODEL = await resolve_model()
    preamble = cfg.get_system_preamble()
    ctx_status = "Active context preamble set" if preamble else "Generic mode (no context set)"
    return f"Connected. Model: {cfg.MODEL}\nEndpoint: {cfg.TGWUI_BASE}\nContext: {ctx_status}"


# Project manifest -> language detection map
_MANIFEST_MAP = [
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("requirements.txt", "python"),
    ("package.json", "typescript"),
    ("tsconfig.json", "typescript"),
    ("Gemfile", "ruby"),
    ("build.gradle", "java"),
    ("pom.xml", "java"),
    ("build.gradle.kts", "kotlin"),
    ("*.csproj", "csharp"),
    ("*.sln", "csharp"),
    ("mix.exs", "elixir"),
    ("pubspec.yaml", "dart"),
    ("CMakeLists.txt", "cpp"),
    ("Makefile", "c"),
    ("dune-project", "ocaml"),
    ("stack.yaml", "haskell"),
    ("Dockerfile", "docker"),
]


def _detect_project(directory: str) -> dict:
    """Detect project language and name from directory contents."""
    d = Path(directory).expanduser().resolve()
    if not d.is_dir():
        return {}

    result: dict = {"directory": str(d)}

    # Detect language from manifest files
    for filename, lang in _MANIFEST_MAP:
        if "*" in filename:
            if list(d.glob(filename)):
                result["language"] = lang
                break
        elif (d / filename).exists():
            result["language"] = lang
            break

    # Detect project name from common sources
    cargo = d / "Cargo.toml"
    if cargo.exists():
        for line in cargo.read_text(errors="replace").splitlines()[:20]:
            if line.strip().startswith("name"):
                name = line.split("=", 1)[-1].strip().strip('"').strip("'")
                if name:
                    result["project_name"] = name
                    break

    pkg = d / "package.json"
    if pkg.exists():
        try:
            import json
            data = json.loads(pkg.read_text(errors="replace"))
            if data.get("name"):
                result["project_name"] = data["name"]
        except Exception:
            pass

    pyproj = d / "pyproject.toml"
    if pyproj.exists():
        for line in pyproj.read_text(errors="replace").splitlines()[:30]:
            if line.strip().startswith("name"):
                name = line.split("=", 1)[-1].strip().strip('"').strip("'")
                if name:
                    result["project_name"] = name
                    break

    gomod = d / "go.mod"
    if gomod.exists():
        first_line = gomod.read_text(errors="replace").splitlines()[0] if gomod.stat().st_size > 0 else ""
        if first_line.startswith("module "):
            result["project_name"] = first_line.split()[-1].split("/")[-1]

    # Check for .localforge-context.yaml or .forge-context.yaml
    for ctx_file in [".localforge-context.yaml", ".forge-context.yaml"]:
        ctx_path = d / ctx_file
        if ctx_path.exists():
            try:
                import yaml
                ctx_data = yaml.safe_load(ctx_path.read_text())
                if isinstance(ctx_data, dict):
                    result["context_file"] = str(ctx_path)
                    if ctx_data.get("language"):
                        result["language"] = ctx_data["language"]
                    if ctx_data.get("project"):
                        result["project_name"] = ctx_data["project"]
                    if ctx_data.get("rules"):
                        result["rules"] = ctx_data["rules"]
            except Exception:
                pass

    # Fallback: use directory name as project name
    if "project_name" not in result:
        result["project_name"] = d.name

    return result


@tool_handler(
    name="auto_context",
    description=(
        "Auto-detect project language and name from a directory, then set context. "
        "Detects from Cargo.toml, package.json, pyproject.toml, go.mod, etc. "
        "Also reads .localforge-context.yaml if present for custom rules. "
        "Optionally pass apply=false to preview without setting."
    ),
    schema={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Directory to scan (default: current working directory)",
            },
            "apply": {
                "type": "boolean",
                "description": "Whether to apply detected context (default: true)",
            },
        },
        "required": [],
    },
)
async def auto_context(args: dict) -> str:
    import os
    directory = args.get("directory", os.getcwd())
    apply = args.get("apply", True)

    detected = _detect_project(directory)
    if not detected.get("language"):
        return f"Could not detect project language in {directory}. Use set_context() manually."

    parts = [f"Detected: {detected['language']} project"]
    if detected.get("project_name"):
        parts.append(f"Name: {detected['project_name']}")
    if detected.get("context_file"):
        parts.append(f"Context file: {detected['context_file']}")
    if detected.get("rules"):
        parts.append(f"Rules: {detected['rules'][:100]}")

    if apply:
        cfg._context.clear()
        cfg._context["language"] = detected["language"]
        if detected.get("project_name"):
            cfg._context["project"] = detected["project_name"]
        if detected.get("rules"):
            cfg._context["rules"] = detected["rules"]

        preamble = cfg.get_system_preamble()
        if preamble:
            parts.append("\nContext applied. Preamble active.")
        else:
            parts.append(f"\nContext set to: {detected['language']}")
    else:
        parts.append("\n(preview only — not applied)")

    return "\n".join(parts)
