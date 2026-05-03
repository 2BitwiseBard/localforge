"""DAG workflow executor.

Executes workflow definitions with support for:
- Sequential and parallel execution (topological ordering)
- Conditional branching (Python expressions)
- Loops with max iteration limits
- Variable substitution in templates
- Progress callbacks for live monitoring
"""

import ast
import asyncio
import json
import logging
import operator
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .schema import NodeDef, WorkflowDef

log = logging.getLogger("workflow-engine")

# Safe expression evaluator — walks AST instead of using eval()
_SAFE_FUNCS = {
    "len": len,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "any": any,
    "all": all,
}

_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY_OPS = {ast.Not: operator.not_, ast.USub: operator.neg}
_BOOL_OPS = {ast.And: all, ast.Or: any}


def _safe_eval(node: ast.AST, ns: dict) -> Any:
    """Recursively evaluate an AST node against a namespace, rejecting unsafe constructs."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, ns)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ns:
            return ns[node.id]
        raise NameError(f"Undefined: {node.id}")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand, ns))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left, ns), _safe_eval(node.right, ns))
    if isinstance(node, ast.BoolOp):
        fn = _BOOL_OPS.get(type(node.op))
        if fn:
            return fn(_safe_eval(v, ns) for v in node.values)
    if isinstance(node, ast.Compare):
        left = _safe_eval(node.left, ns)
        for op, comp in zip(node.ops, node.comparators):
            fn = _CMP_OPS.get(type(op))
            if not fn:
                raise ValueError(f"Unsupported comparator: {type(op).__name__}")
            right = _safe_eval(comp, ns)
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _safe_eval(node.body, ns) if _safe_eval(node.test, ns) else _safe_eval(node.orelse, ns)
    if isinstance(node, ast.Subscript):
        obj = _safe_eval(node.value, ns)
        key = _safe_eval(node.slice, ns)
        return obj[key]
    if isinstance(node, ast.Attribute):
        obj = _safe_eval(node.value, ns)
        if not isinstance(obj, dict):
            raise ValueError(f"Attribute access only on dicts, got {type(obj).__name__}")
        return obj.get(node.attr)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCS:
            args = [_safe_eval(a, ns) for a in node.args]
            return _SAFE_FUNCS[node.func.id](*args)
        raise ValueError(f"Function call not allowed: {ast.dump(node.func)}")
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_safe_eval(e, ns) for e in node.elts]
    raise ValueError(f"Unsupported expression: {type(node).__name__}")


EXECUTIONS_DIR = Path(__file__).parent.parent / "workflow_executions"


@dataclass
class WorkflowContext:
    """Runtime state for a workflow execution."""

    workflow_id: str
    execution_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    variables: dict[str, Any] = field(default_factory=dict)
    node_outputs: dict[str, str] = field(default_factory=dict)
    node_statuses: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    status: str = "running"  # running, completed, failed, cancelled
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "execution_id": self.execution_id,
            "variables": self.variables,
            "node_outputs": self.node_outputs,
            "node_statuses": self.node_statuses,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "error": self.error,
        }

    def save(self):
        EXECUTIONS_DIR.mkdir(exist_ok=True)
        path = EXECUTIONS_DIR / f"{self.execution_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, execution_id: str) -> Optional["WorkflowContext"]:
        path = EXECUTIONS_DIR / f"{execution_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        ctx = cls(workflow_id=data["workflow_id"], execution_id=data["execution_id"])
        ctx.variables = data.get("variables", {})
        ctx.node_outputs = data.get("node_outputs", {})
        ctx.node_statuses = data.get("node_statuses", {})
        ctx.started_at = data.get("started_at", 0)
        ctx.completed_at = data.get("completed_at")
        ctx.status = data.get("status", "unknown")
        ctx.error = data.get("error")
        return ctx


class WorkflowEngine:
    """Executes workflow DAGs with async support."""

    def __init__(self, chat_fn: Callable, tool_fn: Optional[Callable] = None):
        """
        chat_fn: async (prompt: str, system: str = "") -> str
        tool_fn: async (tool_name: str, arguments: dict) -> str
        """
        self._chat = chat_fn
        self._tool = tool_fn

    async def execute(
        self,
        wf: WorkflowDef,
        initial_input: str = "",
        progress_cb: Optional[Callable] = None,
    ) -> WorkflowContext:
        """Execute a workflow DAG. Returns the execution context."""
        errors = wf.validate()
        if errors:
            ctx = WorkflowContext(workflow_id=wf.id)
            ctx.status = "failed"
            ctx.error = "Validation errors: " + "; ".join(errors)
            ctx.save()
            return ctx

        ctx = WorkflowContext(
            workflow_id=wf.id,
            variables={**wf.variables, "input": initial_input},
        )

        # Initialize all node statuses
        for node in wf.nodes:
            ctx.node_statuses[node.id] = "pending"

        try:
            # Execute starting from root nodes
            roots = wf.root_nodes()
            await self._execute_nodes(wf, roots, ctx, progress_cb)
            ctx.status = "completed"
        except asyncio.CancelledError:
            ctx.status = "cancelled"
        except Exception as e:
            ctx.status = "failed"
            ctx.error = str(e)
            log.exception(f"Workflow {wf.id} failed")

        ctx.completed_at = time.time()
        ctx.save()

        if progress_cb:
            progress_cb("__workflow__", ctx.status, ctx.error or "done")

        return ctx

    async def _execute_nodes(
        self,
        wf: WorkflowDef,
        node_ids: list[str],
        ctx: WorkflowContext,
        progress_cb: Optional[Callable],
    ):
        """Execute a list of nodes, then their successors."""
        if not node_ids:
            return

        # Execute current level (parallel if multiple)
        if len(node_ids) == 1:
            await self._execute_node(wf, node_ids[0], ctx, progress_cb)
        else:
            await asyncio.gather(*[self._execute_node(wf, nid, ctx, progress_cb) for nid in node_ids])

        # Determine next nodes to execute
        next_nodes = []
        for nid in node_ids:
            if ctx.node_statuses.get(nid) in ("failed", "skipped"):
                continue
            for succ_id, condition in wf.get_successors(nid):
                if ctx.node_statuses.get(succ_id) != "pending":
                    continue
                # Check all predecessors are done
                preds = wf.get_predecessors(succ_id)
                if all(ctx.node_statuses.get(p) in ("done", "skipped") for p in preds):
                    if condition:
                        if self._eval_condition(condition, ctx, nid):
                            next_nodes.append(succ_id)
                        else:
                            ctx.node_statuses[succ_id] = "skipped"
                    else:
                        next_nodes.append(succ_id)

        # Deduplicate
        seen = set()
        unique_next = []
        for n in next_nodes:
            if n not in seen:
                seen.add(n)
                unique_next.append(n)

        if unique_next:
            await self._execute_nodes(wf, unique_next, ctx, progress_cb)

    async def _execute_node(
        self,
        wf: WorkflowDef,
        node_id: str,
        ctx: WorkflowContext,
        progress_cb: Optional[Callable],
    ):
        """Execute a single node."""
        node = wf.get_node(node_id)
        if not node:
            ctx.node_statuses[node_id] = "failed"
            return

        ctx.node_statuses[node_id] = "running"
        if progress_cb:
            progress_cb(node_id, "running", "")
        ctx.save()

        try:
            output = await self._run_node(wf, node, ctx, progress_cb)
            ctx.node_outputs[node_id] = output
            ctx.node_statuses[node_id] = "done"
            # Update variables with output
            ctx.variables[f"node.{node_id}"] = output
            if progress_cb:
                progress_cb(node_id, "done", output[:200] if output else "")
        except Exception as e:
            ctx.node_statuses[node_id] = "failed"
            ctx.node_outputs[node_id] = f"Error: {e}"
            if progress_cb:
                progress_cb(node_id, "failed", str(e))
            log.error(f"Node {node_id} failed: {e}")

        ctx.save()

    async def _run_node(
        self,
        wf: WorkflowDef,
        node: NodeDef,
        ctx: WorkflowContext,
        progress_cb: Optional[Callable],
    ) -> str:
        """Execute the specific logic for a node type."""
        cfg = node.config

        if node.type == "prompt":
            template = self._resolve_template(cfg.get("template", "{input}"), ctx)
            system = self._resolve_template(cfg.get("system", ""), ctx)
            return await self._chat(template, system)

        elif node.type == "tool":
            if not self._tool:
                return "Error: no tool_fn configured"
            tool_name = cfg.get("tool_name", "")
            arguments = {}
            for k, v in cfg.get("arguments", {}).items():
                arguments[k] = self._resolve_template(str(v), ctx) if isinstance(v, str) else v
            return await self._tool(tool_name, arguments)

        elif node.type == "parallel":
            child_ids = cfg.get("node_ids", [])
            await asyncio.gather(*[self._execute_node(wf, cid, ctx, progress_cb) for cid in child_ids])
            outputs = [ctx.node_outputs.get(cid, "") for cid in child_ids]
            return "\n---\n".join(outputs)

        elif node.type == "condition":
            expression = cfg.get("expression", "True")
            result = self._eval_condition(expression, ctx, node.id)
            target = cfg.get("true_node") if result else cfg.get("false_node")
            if target:
                await self._execute_nodes(wf, [target], ctx, progress_cb)
                return ctx.node_outputs.get(target, f"condition={result}")
            return str(result)

        elif node.type == "loop":
            child_ids = cfg.get("node_ids", [])
            max_iter = cfg.get("max_iterations", 5)
            until_expr = cfg.get("until", "")
            iteration = 0
            last_output = ""

            while iteration < max_iter:
                iteration += 1
                ctx.variables["loop.iteration"] = iteration
                # Reset child node statuses for re-execution
                for cid in child_ids:
                    ctx.node_statuses[cid] = "pending"
                await self._execute_nodes(wf, child_ids, ctx, progress_cb)
                last_output = ctx.node_outputs.get(child_ids[-1], "") if child_ids else ""
                ctx.variables["loop.output"] = last_output

                if until_expr and self._eval_condition(until_expr, ctx, node.id):
                    break

            return f"Loop completed after {iteration} iterations. Last output: {last_output[:500]}"

        elif node.type == "set_variable":
            var_name = cfg.get("name", "")
            value_template = cfg.get("value_template", "")
            value = self._resolve_template(value_template, ctx)
            ctx.variables[var_name] = value
            return f"Set {var_name} = {value[:200]}"

        else:
            return f"Unknown node type: {node.type}"

    def _resolve_template(self, template: str, ctx: WorkflowContext) -> str:
        """Substitute {input}, {variables.x}, {node.id} placeholders."""

        def replacer(match):
            key = match.group(1)
            if key == "input":
                return ctx.variables.get("input", "")
            if key.startswith("variables."):
                var_name = key[10:]
                return str(ctx.variables.get(var_name, f"{{{key}}}"))
            if key.startswith("node."):
                node_id = key[5:]
                return ctx.node_outputs.get(node_id, f"{{{key}}}")
            return str(ctx.variables.get(key, f"{{{key}}}"))

        return re.sub(r"\{([^{}]+)\}", replacer, template)

    def _eval_condition(self, expression: str, ctx: WorkflowContext, current_node_id: str = "") -> bool:
        """Safely evaluate a condition expression using AST walking."""
        namespace = {
            "output": ctx.node_outputs.get(current_node_id, ""),
            "variables": ctx.variables,
            "outputs": ctx.node_outputs,
            "True": True,
            "False": False,
            "None": None,
        }
        try:
            tree = ast.parse(expression, mode="eval")
            result = _safe_eval(tree, namespace)
            return bool(result)
        except Exception as e:
            log.warning(f"Condition eval failed: '{expression}' → {e}")
            return False


def list_executions(limit: int = 50) -> list[dict]:
    """List recent workflow executions."""
    EXECUTIONS_DIR.mkdir(exist_ok=True)
    files = sorted(EXECUTIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    results = []
    for f in files[:limit]:
        try:
            data = json.loads(f.read_text())
            results.append(
                {
                    "execution_id": data.get("execution_id"),
                    "workflow_id": data.get("workflow_id"),
                    "status": data.get("status"),
                    "started_at": data.get("started_at"),
                    "completed_at": data.get("completed_at"),
                    "node_count": len(data.get("node_statuses", {})),
                }
            )
        except Exception:
            continue
    return results
