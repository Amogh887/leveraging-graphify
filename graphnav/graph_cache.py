from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

from graphnav import GraphNotFoundError
from graphnav.graph_nav import GraphNav
from graphnav.graph_query import GraphIndex, merge_relation_weights

RECENCY_COMMITS = 50
RECENCY_DECAY = 0.9

DEFAULT_PACK_SKIP_PATTERNS = [
    "node_modules", ".git", "graphify-out", "dist", "build",
    "playwright-report", "test-results", ".next", "coverage",
    "CLAUDE.md", "AGENTS.md", "copilot-instructions.md",
]

_SHA_LINE = re.compile(r"^[0-9a-f]{40}$")

_MEMO: dict[str, GraphBundle] = {}


@dataclass
class GraphBundle:
    stamp: tuple[int, int]
    skip_key: tuple[str, ...]
    relation_key: tuple[tuple[str, float], ...]
    index: GraphIndex
    nav: GraphNav
    symbols_by_file: dict[str, list[tuple[str, str]]]
    recency: dict[str, float]
    recency_sha: str | None


def graph_stamp(graph_path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(graph_path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def clear_memo() -> None:
    _MEMO.clear()


def _cache_disabled() -> bool:
    return os.environ.get("GRAPHNAV_NO_CACHE") == "1"


def _git_recency(repo_root: str) -> dict[str, float]:
    try:
        out = subprocess.run(
            [
                "git", "-C", repo_root, "log", "--name-only",
                "--pretty=format:%H", "-n", str(RECENCY_COMMITS),
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    scores: dict[str, float] = {}
    commit_idx = -1
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if _SHA_LINE.match(line):
            commit_idx += 1
            continue
        if commit_idx >= 0:
            scores.setdefault(line, RECENCY_DECAY ** commit_idx)
    return scores


def _refresh_recency(bundle: GraphBundle, repo_root: str) -> bool:
    from graphnav.multirepo import _git_sha

    sha = _git_sha(repo_root)
    if sha == bundle.recency_sha:
        return False
    bundle.recency = _git_recency(repo_root) if sha else {}
    bundle.recency_sha = sha
    return True


def _build_bundle(
    graph_path: str,
    skip_patterns: list[str],
    relation_weights: dict[str, float] | None,
    stamp: tuple[int, int],
    skip_key: tuple[str, ...],
    relation_key: tuple[tuple[str, float], ...],
) -> GraphBundle:
    from graphnav.multirepo import _symbols_by_file

    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)
    if not isinstance(graph, dict):
        raise ValueError(f"graph.json is not a JSON object (got {type(graph).__name__})")
    return GraphBundle(
        stamp=stamp,
        skip_key=skip_key,
        relation_key=relation_key,
        index=GraphIndex(
            graph_path, skip_patterns, relation_weights=relation_weights, graph=graph
        ),
        nav=GraphNav(graph_path, skip_patterns, graph=graph),
        symbols_by_file=_symbols_by_file(graph),
        recency={},
        recency_sha=None,
    )


def load_bundle(
    graph_path: str,
    skip_patterns: list[str] | None = None,
    relation_weights: dict[str, float] | None = None,
    repo_root: str | None = None,
) -> GraphBundle:
    abs_graph = os.path.abspath(graph_path)
    stamp = graph_stamp(abs_graph)
    if stamp is None:
        _MEMO.pop(abs_graph, None)
        raise GraphNotFoundError(
            f"graph.json not found: {abs_graph}\n"
            "Run Graphify on the repo first, or set [graph] path in config.toml"
        )
    skip = list(skip_patterns) if skip_patterns is not None else list(DEFAULT_PACK_SKIP_PATTERNS)
    skip_key = tuple(skip)
    relation_key = tuple(sorted(merge_relation_weights(relation_weights).items()))
    root = repo_root or os.path.dirname(os.path.dirname(abs_graph))

    if _cache_disabled():
        bundle = _build_bundle(
            abs_graph, skip, relation_weights, stamp, skip_key, relation_key
        )
        _refresh_recency(bundle, root)
        return bundle

    bundle = _MEMO.get(abs_graph)
    if (
        bundle is None
        or bundle.stamp != stamp
        or bundle.skip_key != skip_key
        or bundle.relation_key != relation_key
    ):
        bundle = _build_bundle(
            abs_graph, skip, relation_weights, stamp, skip_key, relation_key
        )
        _refresh_recency(bundle, root)
        _MEMO[abs_graph] = bundle
        return bundle

    _refresh_recency(bundle, root)
    return bundle
