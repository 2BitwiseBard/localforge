"""Form schemas for the visual workflow editor.

The frontend renders per-node inspector forms from these specs, so adding
a new node type is just a Python dict entry — no new JS component.

Field types understood by the frontend (`workflow_editor.js`):
- text       one-line input
- textarea   multi-line input
- number     numeric input
- select     dropdown (static `options` OR `options_from` URL returning a list)
- kvmap      key/value pairs as a dict
- toggle     boolean checkbox
- code       monospace textarea (for expressions/YAML snippets)
"""

NODE_SPECS: dict[str, dict] = {
    "prompt": {
        "icon": "\U0001f4ac",
        "color": "#58a6ff",
        "label": "Prompt",
        "description": "Send a templated prompt to the local model.",
        "fields": [
            {
                "name": "template",
                "type": "textarea",
                "label": "Prompt template",
                "help": "Use {{var}} to substitute workflow variables.",
                "required": True,
            },
            {"name": "system", "type": "textarea", "label": "System prompt"},
            {"name": "max_tokens", "type": "number", "label": "Max tokens", "default": 1024},
        ],
    },
    "tool": {
        "icon": "\U0001f527",
        "color": "#3fb950",
        "label": "Tool",
        "description": "Invoke a LocalForge MCP tool.",
        "fields": [
            {
                "name": "tool_name",
                "type": "select",
                "label": "MCP tool",
                "options_from": "/api/tools",
                "searchable": True,
                "required": True,
            },
            {
                "name": "arguments",
                "type": "kvmap",
                "label": "Arguments",
                "help": "Values support {{variable}} substitution.",
            },
        ],
    },
    "set_variable": {
        "icon": "=",
        "color": "#d29922",
        "label": "Set variable",
        "description": "Store a computed value into a workflow variable.",
        "fields": [
            {"name": "name", "type": "text", "label": "Variable name", "required": True},
            {
                "name": "value_template",
                "type": "textarea",
                "label": "Value template",
                "help": "Supports {{var}} substitution.",
                "required": True,
            },
        ],
    },
    "condition": {
        "icon": "?",
        "color": "#bc8cff",
        "label": "Condition",
        "description": "Branch on a Python expression (AST-evaluated safely).",
        "fields": [
            {
                "name": "expression",
                "type": "code",
                "label": "Expression",
                "help": "e.g. len(result) > 100",
                "required": True,
            },
            {"name": "true_node", "type": "text", "label": "If true, goto node"},
            {"name": "false_node", "type": "text", "label": "If false, goto node"},
        ],
    },
    "loop": {
        "icon": "\u21bb",
        "color": "#f0883e",
        "label": "Loop",
        "description": "Repeat child nodes until a condition is met.",
        "fields": [
            {"name": "node_ids", "type": "text", "label": "Child node IDs (comma-separated)"},
            {"name": "max_iterations", "type": "number", "label": "Max iterations", "default": 10},
            {"name": "until", "type": "code", "label": "Until expression"},
        ],
    },
    "parallel": {
        "icon": "\u2225",
        "color": "#79c0ff",
        "label": "Parallel",
        "description": "Run multiple nodes concurrently.",
        "fields": [
            {"name": "node_ids", "type": "text", "label": "Node IDs (comma-separated)", "required": True},
        ],
    },
}


def categories() -> list[dict]:
    """Group node types into palette categories for the frontend."""
    return [
        {"name": "Core", "types": ["prompt", "tool", "set_variable"]},
        {"name": "Control", "types": ["condition", "loop", "parallel"]},
    ]
