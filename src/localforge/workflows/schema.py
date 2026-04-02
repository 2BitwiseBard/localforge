"""Workflow DAG schema: nodes, edges, and workflow definitions.

Supports: prompt, tool, parallel, condition, loop, and set_variable nodes.
Workflows are stored as YAML files in the pipelines/workflows/ directory.
"""

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class NodeDef:
    """A single node in a workflow DAG."""
    id: str
    type: str  # prompt, tool, parallel, condition, loop, set_variable
    config: dict = field(default_factory=dict)
    # For prompt: {template, system, max_tokens}
    # For tool: {tool_name, arguments}
    # For parallel: {node_ids: list[str]}
    # For condition: {expression, true_node, false_node}
    # For loop: {node_ids: list[str], max_iterations, until}
    # For set_variable: {name, value_template}

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "config": self.config}

    @classmethod
    def from_dict(cls, d: dict) -> "NodeDef":
        return cls(id=d["id"], type=d["type"], config=d.get("config", {}))


@dataclass
class EdgeDef:
    """A directed edge between workflow nodes."""
    from_id: str
    to_id: str
    condition: Optional[str] = None  # Python expression, evaluated if present

    def to_dict(self) -> dict:
        d = {"from": self.from_id, "to": self.to_id}
        if self.condition:
            d["condition"] = self.condition
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EdgeDef":
        return cls(
            from_id=d.get("from", d.get("from_id", "")),
            to_id=d.get("to", d.get("to_id", "")),
            condition=d.get("condition"),
        )


@dataclass
class WorkflowDef:
    """A complete workflow definition (DAG of nodes + edges)."""
    id: str
    name: str
    description: str = ""
    nodes: list[NodeDef] = field(default_factory=list)
    edges: list[EdgeDef] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "variables": self.variables,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkflowDef":
        return cls(
            id=d.get("id", uuid.uuid4().hex[:12]),
            name=d.get("name", "unnamed"),
            description=d.get("description", ""),
            nodes=[NodeDef.from_dict(n) for n in d.get("nodes", [])],
            edges=[EdgeDef.from_dict(e) for e in d.get("edges", [])],
            variables=d.get("variables", {}),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )

    @classmethod
    def from_yaml(cls, path: Path) -> "WorkflowDef":
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)

    def get_node(self, node_id: str) -> Optional[NodeDef]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def get_successors(self, node_id: str) -> list[tuple[str, Optional[str]]]:
        """Return list of (successor_id, condition) for edges from node_id."""
        return [(e.to_id, e.condition) for e in self.edges if e.from_id == node_id]

    def get_predecessors(self, node_id: str) -> list[str]:
        """Return predecessor node IDs."""
        return [e.from_id for e in self.edges if e.to_id == node_id]

    def root_nodes(self) -> list[str]:
        """Nodes with no incoming edges (entry points)."""
        targets = {e.to_id for e in self.edges}
        return [n.id for n in self.nodes if n.id not in targets]

    def validate(self) -> list[str]:
        """Validate the workflow. Returns list of error messages (empty = valid)."""
        errors = []
        node_ids = {n.id for n in self.nodes}
        valid_types = {"prompt", "tool", "parallel", "condition", "loop", "set_variable"}

        for n in self.nodes:
            if n.type not in valid_types:
                errors.append(f"Node '{n.id}': unknown type '{n.type}'")

        for e in self.edges:
            if e.from_id not in node_ids:
                errors.append(f"Edge from unknown node '{e.from_id}'")
            if e.to_id not in node_ids:
                errors.append(f"Edge to unknown node '{e.to_id}'")

        roots = self.root_nodes()
        if not roots:
            errors.append("No root nodes found (possible cycle)")

        return errors
