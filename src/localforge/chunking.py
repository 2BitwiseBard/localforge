"""Code chunking, BM25 search, and index management for LocalForge.

Provides tree-sitter-aware semantic chunking (26 languages),
line-based fallback chunking, BM25 ranking, and on-disk index persistence.
"""

import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from localforge.paths import indexes_dir

log = logging.getLogger("localforge")

# ---------------------------------------------------------------------------
# BM25 search engine (no external dependency)
# ---------------------------------------------------------------------------


class BM25:
    """Okapi BM25 ranking. Built from tokenized documents (lists of strings)."""

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus = corpus
        self.n = len(corpus)
        self.doc_len = [len(doc) for doc in corpus]
        self.avgdl = sum(self.doc_len) / self.n if self.n else 0
        self.df: dict[str, int] = {}
        for doc in corpus:
            for term in set(doc):
                self.df[term] = self.df.get(term, 0) + 1

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log((self.n - df + 0.5) / (df + 0.5) + 1)

    def _score_one(self, query_tokens: list[str], doc_idx: int) -> float:
        doc = self.corpus[doc_idx]
        tf = Counter(doc)
        score = 0.0
        dl = self.doc_len[doc_idx]
        for term in query_tokens:
            if term not in tf:
                continue
            f = tf[term]
            idf = self._idf(term)
            num = f * (self.k1 + 1)
            den = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            score += idf * num / den
        return score

    def search(self, query_tokens: list[str], top_k: int = 5) -> list[tuple[int, float]]:
        scores = [(i, self._score_one(query_tokens, i)) for i in range(self.n)]
        scores.sort(key=lambda x: x[1], reverse=True)
        return [(i, s) for i, s in scores[:top_k] if s > 0]


def tokenize_bm25(text: str) -> list[str]:
    """Simple word tokenizer for BM25: lowercase, split on non-alphanumeric, drop short tokens."""
    return [t for t in re.sub(r"[^a-z0-9_]", " ", text.lower()).split() if len(t) > 1]


# ---------------------------------------------------------------------------
# File extension sets
# ---------------------------------------------------------------------------

TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".rs",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".xml",
        ".html",
        ".css",
        ".scss",
        ".md",
        ".txt",
        ".rst",
        ".tex",
        ".sql",
        ".r",
        ".lua",
        ".vim",
        ".el",
        ".clj",
        ".hs",
        ".ml",
        ".ex",
        ".exs",
        ".erl",
        ".nix",
        ".tf",
        ".cfg",
        ".ini",
        ".conf",
        ".lock",
        ".svg",
    }
)

# ---------------------------------------------------------------------------
# Tree-sitter code-aware chunking
# ---------------------------------------------------------------------------

TREESITTER_LANG_MAP: dict[str, str] = {
    ".rs": "rust",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".lua": "lua",
    ".hs": "haskell",
    ".ex": "elixir",
    ".exs": "elixir",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".html": "html",
    ".css": "css",
}

TOPLEVEL_NODES: dict[str, set[str]] = {
    "rust": {
        "function_item",
        "struct_item",
        "enum_item",
        "impl_item",
        "trait_item",
        "mod_item",
        "type_item",
        "const_item",
        "static_item",
        "use_declaration",
        "macro_definition",
    },
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "typescript": {
        "function_declaration",
        "class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "export_statement",
        "lexical_declaration",
    },
    "javascript": {
        "function_declaration",
        "class_declaration",
        "export_statement",
        "lexical_declaration",
        "variable_declaration",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration", "var_declaration", "const_declaration"},
    "java": {"class_declaration", "interface_declaration", "enum_declaration", "method_declaration"},
    "c": {"function_definition", "struct_specifier", "enum_specifier", "type_definition", "declaration"},
    "cpp": {
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "enum_specifier",
        "namespace_definition",
        "template_declaration",
    },
}


def chunk_file_line(path: Path, chunk_lines: int = 50, overlap: int = 10) -> list[dict[str, Any]]:
    """Chunk a file into overlapping line-based segments."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines = content.splitlines()
    if not lines:
        return []

    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(lines):
        end = min(start + chunk_lines, len(lines))
        chunk_content = "\n".join(lines[start:end])
        non_empty = sum(1 for ln in lines[start:end] if ln.strip())
        if non_empty >= 3:
            chunks.append(
                {
                    "file": str(path),
                    "start_line": start + 1,
                    "end_line": end,
                    "content": chunk_content,
                    "tokens": tokenize_bm25(chunk_content),
                }
            )
        start += chunk_lines - overlap
        if start >= len(lines):
            break

    return chunks


def chunk_file_treesitter(path: Path, max_chunk_lines: int = 80) -> list[dict[str, Any]]:
    """Chunk a file using tree-sitter for semantic boundaries.

    Falls back to line-based chunking if tree-sitter doesn't support the language.
    """
    suffix = path.suffix.lower()
    lang_name = TREESITTER_LANG_MAP.get(suffix)
    if not lang_name:
        return chunk_file_line(path)

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if not content.strip():
        return []

    try:
        import tree_sitter_languages as tsl

        parser = tsl.get_parser(lang_name)
    except Exception:
        return chunk_file_line(path)

    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    toplevel_types = TOPLEVEL_NODES.get(lang_name, set())
    lines = content.splitlines()
    chunks: list[dict[str, Any]] = []

    # Collect top-level nodes
    nodes: list[tuple[int, int]] = []  # (start_line, end_line) 0-indexed
    for child in root.children:
        if child.type in toplevel_types or not toplevel_types:
            start = child.start_point[0]
            end = child.end_point[0]
            nodes.append((start, end))

    if not nodes:
        return chunk_file_line(path)

    # Group small adjacent nodes into chunks, split large nodes
    current_start = nodes[0][0]
    current_end = nodes[0][1]

    for start, end in nodes[1:]:
        node_lines = end - start + 1
        chunk_lines_count = current_end - current_start + 1

        if chunk_lines_count + node_lines <= max_chunk_lines:
            current_end = end
        else:
            chunk_content = "\n".join(lines[current_start : current_end + 1])
            non_empty = sum(1 for ln in lines[current_start : current_end + 1] if ln.strip())
            if non_empty >= 2:
                chunks.append(
                    {
                        "file": str(path),
                        "start_line": current_start + 1,
                        "end_line": current_end + 1,
                        "content": chunk_content,
                        "tokens": tokenize_bm25(chunk_content),
                    }
                )
            current_start = start
            current_end = end

    # Flush last chunk
    chunk_content = "\n".join(lines[current_start : current_end + 1])
    non_empty = sum(1 for ln in lines[current_start : current_end + 1] if ln.strip())
    if non_empty >= 2:
        chunks.append(
            {
                "file": str(path),
                "start_line": current_start + 1,
                "end_line": current_end + 1,
                "content": chunk_content,
                "tokens": tokenize_bm25(chunk_content),
            }
        )

    # Content before first node (imports, module docstrings, etc.)
    if chunks and nodes:
        if nodes[0][0] > 2:
            preamble = "\n".join(lines[: nodes[0][0]])
            non_empty = sum(1 for ln in lines[: nodes[0][0]] if ln.strip())
            if non_empty >= 3:
                chunks.insert(
                    0,
                    {
                        "file": str(path),
                        "start_line": 1,
                        "end_line": nodes[0][0],
                        "content": preamble,
                        "tokens": tokenize_bm25(preamble),
                    },
                )

    return chunks if chunks else chunk_file_line(path)


# ---------------------------------------------------------------------------
# Index persistence (on-disk JSON)
# ---------------------------------------------------------------------------

_index_cache: dict[str, dict[str, Any]] = {}


def save_index(
    name: str,
    meta: dict,
    chunks: list[dict],
    embeddings: list[list[float]] | None = None,
    sparse_embeddings: list[dict] | None = None,
    colbert_embeddings: list[list[list[float]]] | None = None,
) -> Path:
    """Save an index to disk. Optionally saves dense, sparse, and ColBERT vectors."""
    index_dir = indexes_dir() / name
    index_dir.mkdir(parents=True, exist_ok=True)
    with open(index_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    save_chunks = [{k: v for k, v in c.items() if k != "tokens"} for c in chunks]
    with open(index_dir / "chunks.json", "w") as f:
        json.dump(save_chunks, f)
    if embeddings is not None:
        with open(index_dir / "vectors.json", "w") as f:
            json.dump(embeddings, f)
    if sparse_embeddings is not None:
        with open(index_dir / "sparse_vectors.json", "w") as f:
            json.dump(sparse_embeddings, f)
    if colbert_embeddings is not None:
        with open(index_dir / "colbert_vectors.json", "w") as f:
            json.dump(colbert_embeddings, f)
    return index_dir


def load_index(name: str) -> dict[str, Any] | None:
    """Load an index from disk into cache. Returns the cache entry or None."""
    if name in _index_cache:
        return _index_cache[name]
    index_dir = indexes_dir() / name
    meta_path = index_dir / "meta.json"
    chunks_path = index_dir / "chunks.json"
    vectors_path = index_dir / "vectors.json"
    if not meta_path.exists() or not chunks_path.exists():
        return None
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        with open(chunks_path) as f:
            chunks = json.load(f)
    except Exception:
        return None
    for chunk in chunks:
        chunk["tokens"] = tokenize_bm25(chunk["content"])
    corpus = [c["tokens"] for c in chunks]
    bm25 = BM25(corpus) if corpus else None
    # Load vector types if available
    embeddings = None
    sparse_embeddings = None
    colbert_embeddings = None
    if vectors_path.exists():
        try:
            with open(vectors_path) as f:
                embeddings = json.load(f)
        except Exception:
            log.warning("Failed to load dense vectors for index '%s'", name)
    sparse_path = index_dir / "sparse_vectors.json"
    if sparse_path.exists():
        try:
            with open(sparse_path) as f:
                sparse_embeddings = json.load(f)
        except Exception:
            log.warning("Failed to load sparse vectors for index '%s'", name)
    colbert_path = index_dir / "colbert_vectors.json"
    if colbert_path.exists():
        try:
            with open(colbert_path) as f:
                colbert_embeddings = json.load(f)
        except Exception:
            log.warning("Failed to load ColBERT vectors for index '%s'", name)
    entry = {
        "meta": meta,
        "chunks": chunks,
        "bm25": bm25,
        "embeddings": embeddings,
        "sparse_embeddings": sparse_embeddings,
        "colbert_embeddings": colbert_embeddings,
    }
    _index_cache[name] = entry
    return entry


# ---------------------------------------------------------------------------
# Built-in GBNF grammars for constrained generation
# ---------------------------------------------------------------------------

BUILTIN_GRAMMARS: dict[str, str] = {
    "json": r"""root   ::= object
value  ::= object | array | string | number | ("true" | "false" | "null") ws

object ::=
  "{" ws (
            string ":" ws value
    ("," ws string ":" ws value)*
  )? "}" ws

array  ::=
  "[" ws (
            value
    ("," ws value)*
  )? "]" ws

string ::=
  "\"" (
    [^\\"\x7F\x00-\x1F] |
    "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F])
  )* "\"" ws

number ::= ("-"? ([0-9] | [1-9] [0-9]*)) ("." [0-9]+)? (("e" | "E") ("+" | "-")? [0-9]+)? ws

ws ::= ([ \t\n] ws)?
""",
    "json_array": r"""root ::= "[" ws (value ("," ws value)*)? "]" ws
value  ::= object | array | string | number | ("true" | "false" | "null") ws
object ::= "{" ws (string ":" ws value ("," ws string ":" ws value)*)? "}" ws
array  ::= "[" ws (value ("," ws value)*)? "]" ws
string ::= "\"" ([^\\"\x7F\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\"" ws
number ::= ("-"? ([0-9] | [1-9] [0-9]*)) ("." [0-9]+)? (("e" | "E") ("+" | "-")? [0-9]+)? ws
ws ::= ([ \t\n] ws)?
""",
    "boolean": r"""root ::= ("true" | "false")""",
}
