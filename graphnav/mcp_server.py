from __future__ import annotations

import json
import os
import sys

from graphnav import GraphNotFoundError
from graphnav.config import QueryConfig, load_config
from graphnav.graph_cache import DEFAULT_PACK_SKIP_PATTERNS, load_bundle
from graphnav.graph_nav import GraphNav
from graphnav.multirepo import (
    _overarching_graph_path,
    build_context_pack_inline,
    maybe_auto_rebuild,
)
from graphnav.pathsafe import safe_join

MAX_REGION_LINES = 200

_NO_GRAPH = (
    "No knowledge graph found. Run `graphnav map` (monorepo) or "
    "`graphify extract .` first."
)


def _safe_path(root: str, rel: str) -> str:
    t = safe_join(root, rel)
    if t is None:
        raise ValueError("path escapes repo root")
    return t


class GraphTools:
    def __init__(
        self,
        root: str,
        skip_patterns: list[str] | None = None,
        query_cfg: QueryConfig | None = None,
        auto_rebuild: bool = True,
    ):
        self.root = os.path.abspath(root)
        self.skip_patterns = skip_patterns or list(DEFAULT_PACK_SKIP_PATTERNS)
        self.query_cfg = query_cfg or QueryConfig()
        self.auto_rebuild = auto_rebuild
        self.graph_path = _overarching_graph_path(self.root)

    @property
    def nav(self) -> GraphNav | None:
        try:
            return load_bundle(
                self.graph_path,
                self.skip_patterns,
                relation_weights=self.query_cfg.edge_relation_weights,
                repo_root=self.root,
            ).nav
        except (GraphNotFoundError, OSError, json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError):
            return None

    def graph_context(self, task: str) -> str:
        return build_context_pack_inline(
            root=self.root, task=task, skip_patterns=self.skip_patterns,
            query_cfg=self.query_cfg, auto_rebuild=self.auto_rebuild,
        )

    def graph_find(self, query: str) -> str:
        maybe_auto_rebuild(self.root, enabled=self.auto_rebuild)
        if self.nav is None:
            return _NO_GRAPH
        hits = self.nav.find_symbols(query, k=8)
        if not hits:
            return "(no matches)"
        lines = []
        if all(h.get("fuzzy") for h in hits):
            lines.append("(no exact match — closest symbols:)")
        lines += [f"{h['symbol']} — {h['file']}:{h['loc']}" for h in hits]
        return "\n".join(lines)

    def graph_neighbors(self, symbol: str) -> str:
        maybe_auto_rebuild(self.root, enabled=self.auto_rebuild)
        if self.nav is None:
            return _NO_GRAPH
        r = self.nav.neighbors(symbol)
        if not r.get("found", True):
            return "(symbol not found)"
        parts = [f"{r['symbol']} defined at {r['defined_at']}"]
        if r.get("fuzzy"):
            parts.append(f'(closest match for "{r["query"]}")')
        if r.get("callers"):
            parts.append("callers:\n" + "\n".join("  " + c for c in r["callers"]))
        if r.get("callees"):
            parts.append("calls:\n" + "\n".join("  " + c for c in r["callees"]))
        return "\n".join(parts)

    def read_region(self, path: str, start_line: int, end_line: int) -> str:
        try:
            abs_path = _safe_path(self.root, path)
            start = max(1, int(start_line))
            end = min(start + MAX_REGION_LINES, int(end_line))
            with open(abs_path, errors="replace") as f:
                lines = f.read().splitlines()
            chunk = lines[start - 1:end]
            return "\n".join(f"{start + i:>5}  {ln}" for i, ln in enumerate(chunk)) or "(empty range)"
        except (ValueError, OSError) as exc:
            return f"error: {exc}"

    def impact(self, symbol: str) -> str:
        maybe_auto_rebuild(self.root, enabled=self.auto_rebuild)
        if self.nav is None:
            return _NO_GRAPH
        r = self.nav.neighbors(symbol)
        if not r.get("found", True):
            return "(symbol not found)"
        out = [f"# Blast radius of {r['symbol']} (defined at {r['defined_at']})", ""]
        if r.get("fuzzy"):
            out.insert(1, f'(closest match for "{r["query"]}")')
        callers = r.get("callers") or []
        out.append("## Direct callers (break if you change the signature or behavior)")
        out.extend("- " + c for c in callers) if callers else out.append("_none found_")
        callees = r.get("callees") or []
        if callees:
            out += ["", "## This symbol depends on", *("- " + c for c in callees)]
        out += [
            "",
            "Before editing, confirm cross-service impact with "
            f'`graphify affected "{r["symbol"]}"`.',
        ]
        return "\n".join(out)


def serve(root: str = ".", config_path: str | None = None) -> int:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print(
            "Could not import the 'mcp' package (a core dependency of graphnav).\n"
            "Reinstall graphnav, or install it directly with: pip install 'mcp>=1.2'",
            file=sys.stderr,
        )
        return 1

    cfg = load_config(config_path)
    tools = GraphTools(
        os.path.abspath(root), cfg.graph.skip_patterns, query_cfg=cfg.query,
        auto_rebuild=cfg.mono.auto_rebuild,
    )
    server = FastMCP("graphnav")

    @server.tool()
    def graph_context(task: str) -> str:
        """Minimal, token-budgeted context pack for a coding task: the most relevant
        files with their code regions inline, plus cross-reference closure. Use this
        FIRST instead of find/ls/cat or reading whole files."""
        return tools.graph_context(task)

    @server.tool()
    def graph_find(query: str) -> str:
        """Find the most relevant symbols (functions/classes) for a query from the
        knowledge graph. Returns symbol name + file:line. Use instead of text search."""
        return tools.graph_find(query)

    @server.tool()
    def graph_neighbors(symbol: str) -> str:
        """Show a symbol's callers and callees from the knowledge graph."""
        return tools.graph_neighbors(symbol)

    @server.tool()
    def read_region(path: str, start_line: int, end_line: int) -> str:
        """Read a specific line range of a file (cheaper than reading the whole file)."""
        return tools.read_region(path, start_line, end_line)

    @server.tool()
    def impact(symbol: str) -> str:
        """Blast radius of changing a symbol: its direct callers and dependencies."""
        return tools.impact(symbol)

    server.run()
    return 0
