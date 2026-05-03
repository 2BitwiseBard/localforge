"""Orchestration tools — auto_route, workflow, pipeline, save/list pipelines."""

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from localforge import config as cfg
from localforge.chunking import BUILTIN_GRAMMARS, TEXT_EXTENSIONS
from localforge.client import chat
from localforge.paths import pipelines_dir
from localforge.tools import tool_handler

PIPELINES_DIR = pipelines_dir()


def _sanitize_topic(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip())[:80]


@tool_handler(
    name="auto_route",
    description=(
        "Classify a task, recommend the optimal model, and suggest a tool pipeline. "
        "Optionally auto-sets context and loads the recommended model. "
        "One-call 'just do it' routing for any task."
    ),
    schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Description of the task to route"},
            "auto_load": {
                "type": "boolean",
                "description": "Auto-load the recommended model if not already loaded (default: false)",
            },
            "auto_context": {"type": "boolean", "description": "Auto-detect and set project context (default: false)"},
        },
        "required": ["task"],
    },
)
async def auto_route(args: dict) -> str:
    from localforge.tools.analysis import classify_task

    task = args["task"]
    classification = await classify_task({"task": task})

    task_lower = task.lower()
    suggested_tools = []

    if any(w in task_lower for w in ["review", "diff", "pr", "pull request"]):
        suggested_tools = ["review_diff", "diff_explain"]
    elif any(w in task_lower for w in ["refactor", "improve", "clean"]):
        suggested_tools = ["analyze_code", "suggest_refactor"]
    elif any(w in task_lower for w in ["test", "testing"]):
        suggested_tools = ["generate_test_stubs"]
    elif any(w in task_lower for w in ["document", "docs", "explain"]):
        suggested_tools = ["draft_docs", "summarize_file"]
    elif any(w in task_lower for w in ["search", "find", "where"]):
        suggested_tools = ["search_index", "rag_query"]
    elif any(w in task_lower for w in ["image", "screenshot", "visual"]):
        suggested_tools = ["analyze_image"]
    elif any(w in task_lower for w in ["translate", "convert"]):
        suggested_tools = ["translate_code"]
    elif any(w in task_lower for w in ["error", "bug", "fix"]):
        suggested_tools = ["explain_error", "analyze_code"]
    else:
        suggested_tools = ["local_chat"]

    context_msg = ""
    if args.get("auto_context"):
        cwd = Path.cwd()
        cwd_name = cwd.name.lower()
        lang_markers = {
            "Cargo.toml": "rust",
            "pyproject.toml": "python",
            "setup.py": "python",
            "package.json": "javascript",
            "go.mod": "go",
            "build.gradle": "java",
            "Gemfile": "ruby",
            "mix.exs": "elixir",
            "CMakeLists.txt": "cpp",
        }
        detected_lang = None
        for marker, lang in lang_markers.items():
            if (cwd / marker).exists():
                detected_lang = lang
                break
        if detected_lang:
            cfg._context.clear()
            cfg._context["language"] = detected_lang
            cfg._context["project"] = cwd_name
            context_msg = f"\nContext: {detected_lang}/{cwd_name} (auto-detected)"
        else:
            cfg._context.clear()
            cfg._context["project"] = cwd_name
            context_msg = f"\nContext: {cwd_name} (auto-detected)"

    load_msg = ""
    if args.get("auto_load"):
        for line in classification.splitlines():
            if line.strip().startswith("-> "):
                recommended = line.strip()[3:].strip()
                if cfg.MODEL and recommended in cfg.MODEL:
                    load_msg = f"\nModel: {cfg.MODEL} (already loaded, good fit)"
                else:
                    load_msg = f"\nModel swap recommended: {recommended}"
                    load_msg += "\n(auto_load will not swap automatically — call swap_model)"
                break

    pipeline_str = " -> ".join(suggested_tools) if suggested_tools else "(general chat)"

    return f"{classification}{context_msg}{load_msg}\n\nSuggested pipeline: {pipeline_str}"


@tool_handler(
    name="workflow",
    description=(
        "Execute a predefined multi-step workflow. Available workflows:\n"
        "- 'full-review': git diff -> review_diff + diff_explain\n"
        "- 'pr-review': like full-review but includes RAG context from project index\n"
        "- 'deep-analyze': index directory -> rag_query on key questions -> summary\n"
        "- 'onboard-project': detect language -> set_context -> index -> summarize entry files\n"
        "- 'research': check KG first, then deep_research if topic is novel"
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": ["full-review", "pr-review", "deep-analyze", "onboard-project", "research"],
                "description": "Workflow to execute",
            },
            "directory": {"type": "string", "description": "Target directory (for deep-analyze, onboard-project)"},
            "diff": {"type": "string", "description": "Git diff input (for full-review, pr-review)"},
            "question": {"type": "string", "description": "Research question (for research workflow)"},
            "max_sources": {"type": "integer", "description": "Max sources for research (default 3)"},
        },
        "required": ["name"],
    },
)
async def workflow_tool(args: dict) -> str:
    from localforge.tools.knowledge import _get_kg
    from localforge.tools.rag import index_directory, rag_query
    from localforge.tools.web import deep_research

    wf_name = args["name"]

    if wf_name == "full-review":
        diff = args.get("diff", "")
        if not diff:
            return "Error: 'diff' is required for full-review workflow"

        review = await chat(
            f"Review this git diff for bugs, security issues, and style problems.\n"
            f"For each issue: file, line, severity (critical/warning/nit), what's wrong, how to fix.\n"
            f"If clean, say so.\n\n```diff\n{diff}\n```",
            system=cfg.get_system_preamble(),
        )
        explanation = await chat(
            f"Explain what this git diff does in plain English. Focus on what changed and why.\n\n```diff\n{diff}\n```",
            system=cfg.get_system_preamble(),
        )
        return f"## Review\n\n{review}\n\n## Summary\n\n{explanation}"

    elif wf_name == "deep-analyze":
        directory = args.get("directory", "")
        if not directory:
            return "Error: 'directory' is required for deep-analyze workflow"

        dir_path = Path(os.path.expanduser(directory)).resolve()
        if not dir_path.exists():
            return f"Error: directory not found: {dir_path}"

        ext_counts: dict[str, int] = {}
        for f in dir_path.rglob("*"):
            if f.is_file() and f.suffix:
                ext_counts[f.suffix] = ext_counts.get(f.suffix, 0) + 1

        top_ext = max(ext_counts, key=ext_counts.get) if ext_counts else "*.*"
        ext_to_glob = {
            ".rs": "**/*.rs",
            ".py": "**/*.py",
            ".ts": "**/*.ts",
            ".tsx": "**/*.tsx",
            ".js": "**/*.js",
            ".go": "**/*.go",
        }
        glob_pattern = ext_to_glob.get(top_ext, "**/*.*")

        index_name = _sanitize_topic(dir_path.name)
        idx_result = await index_directory(
            {
                "name": index_name,
                "directory": directory,
                "glob_pattern": glob_pattern,
            }
        )

        questions = [
            "What are the main entry points and how is the application structured?",
            "What are the key data types and how do they relate to each other?",
            "What external dependencies or APIs does this project use?",
        ]
        answers = []
        for q in questions:
            answer = await rag_query({"index_name": index_name, "question": q, "top_k": 3})
            answers.append(f"**Q: {q}**\n{answer}")

        return f"## Index\n{idx_result}\n\n## Analysis\n\n" + "\n\n---\n\n".join(answers)

    elif wf_name == "onboard-project":
        directory = args.get("directory", "")
        if not directory:
            return "Error: 'directory' is required for onboard-project workflow"

        dir_path = Path(os.path.expanduser(directory)).resolve()
        if not dir_path.exists():
            return f"Error: directory not found: {dir_path}"

        ext_counts = {}
        for f in dir_path.rglob("*"):
            if f.is_file() and f.suffix in TEXT_EXTENSIONS:
                ext_counts[f.suffix] = ext_counts.get(f.suffix, 0) + 1

        ext_to_lang = {
            ".rs": "rust",
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".go": "go",
        }
        top_ext = max(ext_counts, key=ext_counts.get) if ext_counts else ""
        lang = ext_to_lang.get(top_ext, "auto")

        project_name = dir_path.name
        cfg._context.clear()
        cfg._context["language"] = lang
        cfg._context["project"] = project_name

        ext_to_glob = {
            ".rs": "**/*.rs",
            ".py": "**/*.py",
            ".ts": "**/*.{ts,tsx}",
            ".tsx": "**/*.{ts,tsx}",
            ".js": "**/*.{js,jsx}",
            ".go": "**/*.go",
        }
        glob_pattern = ext_to_glob.get(top_ext, "**/*.*")
        index_name = _sanitize_topic(project_name)
        idx_result = await index_directory(
            {
                "name": index_name,
                "directory": directory,
                "glob_pattern": glob_pattern,
            }
        )

        entry_candidates = [
            "main.rs",
            "lib.rs",
            "main.py",
            "app.py",
            "index.ts",
            "index.js",
            "main.go",
            "mod.rs",
            "Cargo.toml",
            "package.json",
        ]
        found_entries = []
        for candidate in entry_candidates:
            matches = list(dir_path.rglob(candidate))
            found_entries.extend(matches[:2])

        summaries = []
        for entry_file in found_entries[:5]:
            try:
                content = entry_file.read_text(encoding="utf-8", errors="replace")
                if len(content) > 50000:
                    content = content[:50000]
                summary = await chat(
                    f"Summarize the structure of this file ({entry_file.name}):\n```\n{content}\n```",
                    system=cfg.get_system_preamble(),
                )
                summaries.append(f"**{entry_file.relative_to(dir_path)}:**\n{summary}")
            except Exception:
                pass

        return (
            f"## Onboarding: {project_name}\n\n"
            f"Language: {lang}\n"
            f"Context: set to {lang}/{project_name}\n\n"
            f"## Index\n{idx_result}\n\n"
            f"## Key Files\n\n" + "\n\n---\n\n".join(summaries or ["(no entry files found)"])
        )

    elif wf_name == "pr-review":
        diff = args.get("diff", "")
        if not diff:
            return "Error: 'diff' is required for pr-review workflow"

        review = await chat(
            f"Review this git diff for bugs, security issues, and style problems.\n"
            f"For each issue: file, line, severity (critical/warning/nit), what's wrong, how to fix.\n"
            f"If clean, say so.\n\n```diff\n{diff}\n```",
            system=cfg.get_system_preamble(),
        )

        rag_context = ""
        from localforge.chunking import _index_cache

        indexes = list(_index_cache.keys())
        if indexes:
            idx_name = indexes[0]
            try:
                symbols = re.findall(r"(?:fn|def|function|class|struct|impl)\s+(\w+)", diff)
                if symbols:
                    query = " ".join(symbols[:5])
                    rag_result = await rag_query({"index_name": idx_name, "question": query, "top_k": 3})
                    if rag_result and "error" not in rag_result.lower()[:20]:
                        rag_context = f"\n\n## Related Code (from {idx_name} index)\n\n{rag_result[:1500]}"
            except Exception:
                pass

        explanation = await chat(
            f"Explain what this git diff does in plain English. Focus on what changed and why.\n\n```diff\n{diff}\n```",
            system=cfg.get_system_preamble(),
        )

        return f"## Review\n\n{review}{rag_context}\n\n## Summary\n\n{explanation}"

    elif wf_name == "research":
        question = args.get("question", "")
        if not question:
            return "Error: 'question' is required for research workflow"

        kg = _get_kg()
        existing = kg.query(question, max_results=3)
        if existing:
            recent = [e for e in existing if time.time() - e.get("updated_at", 0) < 7 * 86400]
            if recent:
                context_parts = [f"- **{e['name']}** ({e['type']}): {e['content']}" for e in recent]
                return (
                    "## Existing Research Found\n\n"
                    "Recent KG entries (within 7 days):\n\n"
                    + "\n".join(context_parts)
                    + "\n\nTo force new research, use `deep_research` directly."
                )

        return await deep_research(
            {
                "question": question,
                "max_sources": args.get("max_sources", 3),
                "save_to_kg": True,
            }
        )

    return f"Unknown workflow: {wf_name}"


@tool_handler(
    name="pipeline",
    description=(
        "Chain sequential prompts where each step's output feeds the next. "
        "Use {input} placeholder in each step's prompt to reference the previous output. "
        "Use 'template' to load a saved pipeline template by name."
    ),
    schema={
        "type": "object",
        "properties": {
            "initial_input": {"type": "string", "description": "Input for the first step"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Prompt template (use {input} for previous output)",
                        },
                        "max_tokens": {"type": "integer", "description": "Max tokens for this step"},
                        "grammar": {"type": "string", "description": "GBNF grammar for this step (optional)"},
                    },
                    "required": ["prompt"],
                },
                "description": "Ordered list of pipeline steps (or omit if using template)",
            },
            "template": {"type": "string", "description": "Load steps from a saved pipeline template name"},
        },
        "required": ["initial_input"],
    },
)
async def pipeline_tool(args: dict) -> str:
    current_input = args["initial_input"]

    steps = args.get("steps")
    if args.get("template"):
        tmpl_name = _sanitize_topic(args["template"])
        tmpl_path = PIPELINES_DIR / f"{tmpl_name}.json"
        if not tmpl_path.exists():
            available = [f.stem for f in PIPELINES_DIR.glob("*.json")] if PIPELINES_DIR.exists() else []
            return f"Template '{tmpl_name}' not found. Available: {', '.join(sorted(available)) or '(none)'}"
        with open(tmpl_path) as f:
            tmpl_data = json.load(f)
        steps = tmpl_data.get("steps", [])

    if not steps:
        return "Error: provide at least one step"
    if len(steps) > 10:
        return "Error: max 10 steps"

    results = []
    for i, step in enumerate(steps):
        prompt = step["prompt"].replace("{input}", current_input)
        kwargs: dict[str, Any] = {}
        if step.get("max_tokens"):
            kwargs["max_tokens"] = step["max_tokens"]
        if step.get("grammar"):
            kwargs["grammar_string"] = BUILTIN_GRAMMARS.get(step["grammar"], step["grammar"])

        result = await chat(prompt, system=cfg.get_system_preamble(), **kwargs)
        results.append(f"--- Step {i + 1} ---\n{result}")
        current_input = result

    return "\n\n".join(results)


@tool_handler(
    name="save_pipeline",
    description=(
        "Save a pipeline template for reuse. Pipelines are stored as JSON files "
        "and can be loaded by name with the pipeline tool."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Template name"},
            "description": {"type": "string", "description": "What this pipeline does"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "max_tokens": {"type": "integer"},
                        "grammar": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
                "description": "Pipeline steps",
            },
        },
        "required": ["name", "steps"],
    },
)
async def save_pipeline_tool(args: dict) -> str:
    name = _sanitize_topic(args["name"])
    PIPELINES_DIR.mkdir(parents=True, exist_ok=True)

    template = {
        "name": name,
        "description": args.get("description", ""),
        "steps": args["steps"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    path = PIPELINES_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(template, f, indent=2)

    return f"Pipeline '{name}' saved ({len(args['steps'])} steps) to {path}"


@tool_handler(
    name="list_pipelines",
    description="List all saved pipeline templates.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_pipelines(args: dict) -> str:
    if not PIPELINES_DIR.exists():
        return "No saved pipelines. Use save_pipeline to create one."

    files = sorted(PIPELINES_DIR.glob("*.json"))
    if not files:
        return "No saved pipelines."

    lines = [f"Saved pipelines ({len(files)}):"]
    for f in files:
        try:
            with open(f) as fp:
                data = json.load(fp)
            desc = data.get("description", "")
            steps = len(data.get("steps", []))
            lines.append(f"  {data['name']}: {steps} steps — {desc}")
        except Exception:
            lines.append(f"  {f.stem}: (unreadable)")

    return "\n".join(lines)
