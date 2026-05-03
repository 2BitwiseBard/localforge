"""Diff review and commit message tools."""

from localforge import config as cfg
from localforge.client import chat, task_type_context
from localforge.tools import tool_handler


@tool_handler(
    name="review_diff",
    description="Review a git diff for bugs, security issues, and style problems (any language)",
    schema={
        "type": "object",
        "properties": {
            "diff": {"type": "string", "description": "Git diff output to review"},
            "focus": {
                "type": "string",
                "description": "Optional focus area (e.g. 'security', 'performance', 'correctness')",
            },
        },
        "required": ["diff"],
    },
)
async def review_diff(args: dict) -> str:
    focus = args.get("focus", "bugs, security issues, and style problems")
    prompt = (
        f"Review this git diff for: {focus}\n\n"
        f"For each issue found, state:\n"
        f"- File and line (from the diff)\n"
        f"- Severity (critical / warning / nit)\n"
        f"- What's wrong and how to fix it\n\n"
        f"If the diff looks clean, say so.\n\n"
        f"```diff\n{args['diff']}\n```"
    )
    async with task_type_context("code"):
        return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="draft_commit_message",
    description="Generate a conventional commit message from a git diff",
    schema={
        "type": "object",
        "properties": {
            "diff": {"type": "string", "description": "Git diff output"},
            "style": {"type": "string", "description": "Commit style (default: 'conventional')"},
        },
        "required": ["diff"],
    },
)
async def draft_commit_message(args: dict) -> str:
    style = args.get("style", "conventional")
    prompt = (
        f"Generate a {style} commit message for this diff.\n\n"
        f"Rules:\n"
        f"- First line: type(scope): description (max 72 chars)\n"
        f"- Types: feat, fix, refactor, docs, test, chore, perf, style, ci\n"
        f"- Body: brief explanation of why, not what (the diff shows what)\n"
        f"- Output ONLY the commit message, no extra commentary\n\n"
        f"```diff\n{args['diff']}\n```"
    )
    return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="diff_explain",
    description="Explain a git diff in plain English — what changed and why it likely matters",
    schema={
        "type": "object",
        "properties": {
            "diff": {"type": "string", "description": "Git diff output"},
            "detail": {"type": "string", "description": "Detail level: 'brief' or 'detailed' (default: 'brief')"},
        },
        "required": ["diff"],
    },
)
async def diff_explain(args: dict) -> str:
    detail = args.get("detail", "brief")
    if detail == "detailed":
        instruction = (
            "Explain this diff in detail. For each file changed:\n"
            "1. What was changed\n"
            "2. Why it likely matters\n"
            "3. Any potential risks or side effects"
        )
    else:
        instruction = (
            "Explain this diff in 2-3 sentences. Focus on what changed and why. "
            "Don't list every file — summarize the intent."
        )
    prompt = f"{instruction}\n\n```diff\n{args['diff']}\n```"
    return await chat(prompt, system=cfg.get_system_preamble())
