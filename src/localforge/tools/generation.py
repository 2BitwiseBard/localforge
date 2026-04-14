"""Code generation, docs, and transformation tools."""

from localforge import config as cfg
from localforge.client import chat, task_type_context
from localforge.tools import tool_handler


@tool_handler(
    name="generate_test_stubs",
    description="Generate test stub functions for the public API in the given source code",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to generate test stubs for"},
            "language": {"type": "string", "description": "Language hint (optional, overrides context)"},
            "module_name": {"type": "string", "description": "Module/crate name for imports (optional)"},
        },
        "required": ["code"],
    },
)
async def generate_test_stubs(args: dict) -> str:
    lang = args.get("language", cfg._context.get("language", ""))
    module = args.get("module_name", "")
    prompt = (
        f"Generate test stub functions for the public API in this {lang or 'code'}. "
        f"Use the idiomatic test framework for the language. "
        f"Stubs should have empty/todo bodies.\n"
    )
    if module:
        prompt += f"Module/crate name for imports: {module}\n"
    prompt += f"\n```\n{args['code']}\n```\n\nOutput only the test code."
    async with task_type_context("code"):
        return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="suggest_refactor",
    description="Given code and a refactoring goal, return a refactored version",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to refactor"},
            "goal": {"type": "string", "description": "Refactoring goal (e.g. 'extract method', 'improve error handling')"},
        },
        "required": ["code", "goal"],
    },
)
async def suggest_refactor(args: dict) -> str:
    prompt = (
        f"Refactor the following code to achieve this goal: {args['goal']}\n\n"
        f"```\n{args['code']}\n```\n\n"
        f"Output the refactored code with brief comments explaining changes."
    )
    async with task_type_context("code"):
        return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="draft_docs",
    description="Generate documentation: doc comments, README sections, or API docs",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to document"},
            "style": {"type": "string", "description": "Doc style: 'inline' (doc comments), 'readme' (README section), 'api' (API reference)"},
            "language": {"type": "string", "description": "Language hint (optional, overrides context)"},
        },
        "required": ["code"],
    },
)
async def draft_docs(args: dict) -> str:
    lang = args.get("language", cfg._context.get("language", ""))
    style = args.get("style", "inline")
    style_map = {
        "inline": "Add idiomatic doc comments to each public item. Output the code with docs added.",
        "readme": "Write a README section documenting the public API. Use markdown.",
        "api": "Write API reference documentation covering all public items, parameters, return types, and examples.",
    }
    instruction = style_map.get(style, style_map["inline"])
    prompt = (
        f"{instruction}\n\n"
        f"Language: {lang or 'auto-detect'}\n\n"
        f"```\n{args['code']}\n```"
    )
    return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="translate_code",
    description="Translate code between programming languages idiomatically",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to translate"},
            "from_lang": {"type": "string", "description": "Source language (optional, auto-detect if omitted)"},
            "to_lang": {"type": "string", "description": "Target language"},
        },
        "required": ["code", "to_lang"],
    },
)
async def translate_code(args: dict) -> str:
    from_lang = args.get("from_lang", "auto-detect")
    prompt = (
        f"Translate this code from {from_lang} to {args['to_lang']}.\n\n"
        f"Requirements:\n"
        f"- Use idiomatic patterns for the target language\n"
        f"- Preserve the logic and behavior\n"
        f"- Add brief comments where the translation is non-obvious\n\n"
        f"```\n{args['code']}\n```\n\n"
        f"Output only the translated code."
    )
    async with task_type_context("code"):
        return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="generate_regex",
    description="Generate and explain a regex pattern from a natural language description",
    schema={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Natural language description of what to match"},
            "flavor": {"type": "string", "description": "Regex flavor: 'pcre', 'python', 'javascript', 'rust' (default: 'pcre')"},
            "examples": {"type": "string", "description": "Optional example strings that should/shouldn't match"},
        },
        "required": ["description"],
    },
)
async def generate_regex(args: dict) -> str:
    flavor = args.get("flavor", "pcre")
    examples_block = ""
    if args.get("examples"):
        examples_block = f"\n\nExamples:\n{args['examples']}"
    prompt = (
        f"Generate a {flavor} regex that matches: {args['description']}{examples_block}\n\n"
        f"Output:\n"
        f"1. The regex pattern\n"
        f"2. A breakdown explaining each part\n"
        f"3. Edge cases to watch out for"
    )
    return await chat(prompt)


@tool_handler(
    name="optimize_query",
    description="Optimize a SQL, Polars, or DuckDB query for performance",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The query to optimize"},
            "engine": {"type": "string", "description": "Query engine: 'sql', 'polars', 'duckdb' (default: auto-detect)"},
            "context": {"type": "string", "description": "Optional schema/table info or performance constraints"},
        },
        "required": ["query"],
    },
)
async def optimize_query(args: dict) -> str:
    engine = args.get("engine", "auto-detect")
    context_block = ""
    if args.get("context"):
        context_block = f"\n\nSchema/context:\n{args['context']}"
    prompt = (
        f"Optimize this {engine} query for performance.{context_block}\n\n"
        f"```\n{args['query']}\n```\n\n"
        f"Output:\n"
        f"1. The optimized query\n"
        f"2. What changed and why\n"
        f"3. Expected performance impact"
    )
    return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="structured_output",
    description=(
        "Get a JSON-structured response from the local model. "
        "Useful for extracting structured data, generating configs, or tool chaining. "
        "Supports GBNF grammar constraints for guaranteed valid output."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to generate as JSON"},
            "schema_hint": {"type": "string", "description": "Example JSON structure the output should follow"},
            "use_grammar": {"type": "boolean", "description": "Use GBNF JSON grammar for guaranteed valid JSON (default: true)"},
        },
        "required": ["prompt"],
    },
)
async def structured_output(args: dict) -> str:
    from localforge.chunking import BUILTIN_GRAMMARS
    schema_block = ""
    if args.get("schema_hint"):
        schema_block = f"\n\nExpected structure:\n```json\n{args['schema_hint']}\n```"
    prompt = (
        f"Respond with valid JSON only. No markdown, no explanation, just the JSON.\n\n"
        f"{args['prompt']}{schema_block}"
    )
    kwargs = {}
    if args.get("use_grammar", True):
        kwargs["grammar_string"] = BUILTIN_GRAMMARS["json"]
    return await chat(prompt, **kwargs)
