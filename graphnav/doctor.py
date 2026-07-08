from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from graphnav.config import (
    BACKEND_KEY_VARS,
    Config,
    backend_has_key,
    backend_provider,
    load_config_report,
)
from graphnav.graph_cache import load_bundle
from graphnav.multirepo import (
    _graph_meta_path,
    _load_env_file,
    _overarching_graph_path,
    find_graphify,
    resolve_services,
    staleness_note,
)

MAX_SERVICE_NAMES_SHOWN = 5


@dataclass
class CheckResult:
    status: str
    label: str
    detail: str


def _check_graphify_binary() -> CheckResult:
    path = find_graphify()
    if path is None:
        return CheckResult("fail", "graphify binary", "not found — install with: pip install graphifyy")
    detail = path
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            version = (out.stdout or "").strip()
            if version:
                detail = f"{path} ({version})"
    except Exception:
        pass
    return CheckResult("ok", "graphify binary", detail)


def _check_config(config_path: str | None) -> tuple[Config, CheckResult]:
    cfg, source, warnings = load_config_report(config_path)
    if warnings:
        return cfg, CheckResult("warn", "config", "; ".join(warnings))
    return cfg, CheckResult("ok", "config", source or "defaults (no config file)")


def _check_graph(root: str) -> tuple[CheckResult, bool]:
    path = _overarching_graph_path(root)
    if not os.path.exists(path):
        return CheckResult("warn", "graph.json", "not built yet — run `graphnav map` (free, local)"), False
    try:
        with open(path) as f:
            graph = json.load(f)
    except (OSError, json.JSONDecodeError):
        return CheckResult("fail", "graph.json", "corrupt — run `graphnav map`"), False
    if not isinstance(graph, dict) or "nodes" not in graph or ("links" not in graph and "edges" not in graph):
        return CheckResult("fail", "graph.json", "missing nodes/links — run `graphnav map`"), False
    links = graph.get("links")
    if links is None:
        links = graph.get("edges", [])
    return CheckResult("ok", "graph.json", f"{len(graph['nodes'])} nodes, {len(links)} links"), True


def _check_staleness(root: str) -> CheckResult:
    if not os.path.exists(_graph_meta_path(root)):
        return CheckResult(
            "warn", "graph meta",
            "no .graphnav-meta.json (graph predates 1.1) — re-run `graphnav map` to enable staleness tracking",
        )
    if staleness_note(root):
        return CheckResult("warn", "graph meta", "graph is behind HEAD — re-run `graphnav map`")
    return CheckResult("ok", "graph meta", "up to date")


def _check_api_key(root: str, cfg: Config) -> CheckResult:
    backend = cfg.mono.graphify_backend
    if backend not in BACKEND_KEY_VARS:
        return CheckResult("warn", "API key", f"unknown backend '{backend}'")
    key_vars = BACKEND_KEY_VARS[backend]
    if not key_vars:
        return CheckResult("ok", "API key", "local backend, no key needed")
    for var in key_vars:
        if os.environ.get(var):
            return CheckResult("ok", "API key", f"found in environment (${var})")
    env_vars = _load_env_file(root)
    for var in key_vars:
        if env_vars.get(var):
            return CheckResult("ok", "API key", f"found in .env (${var})")
    expected = " or ".join(key_vars)
    return CheckResult("ok", "API key", f"none set — map/watch build a free AST-only graph (set {expected} for richer semantic links)")


def _check_mode(root: str, cfg: Config) -> CheckResult:
    backend = cfg.mono.graphify_backend
    env = dict(os.environ)
    env.update(_load_env_file(root))
    has_key = backend_has_key(backend, env)
    if cfg.mono.semantic and has_key:
        return CheckResult(
            "ok", "mode",
            f"semantic — `graphnav map` sends code to {backend_provider(backend)}'s API (may incur cost)",
        )
    if cfg.mono.semantic and not has_key:
        return CheckResult(
            "warn", "mode",
            f"semantic requested but no '{backend}' key — `graphnav map` builds a free local AST-only graph",
        )
    detail = "local — `graphnav map` builds an AST-only graph (no network, no LLM, no cost)"
    if has_key:
        detail += "; add --semantic for richer LLM links"
    return CheckResult("ok", "mode", detail)


def _check_services(root: str, cfg: Config) -> CheckResult:
    services, single = resolve_services(root, cfg.mono.marker_files, cfg.mono.extra_skip_dirs)
    if not services:
        return CheckResult("fail", "services", "no source code found — run graphnav from your project root")
    if single:
        return CheckResult("ok", "services", "single project (whole repo mapped as one graph)")
    names = [s.name for s in services]
    shown = ", ".join(names[:MAX_SERVICE_NAMES_SHOWN])
    if len(names) > MAX_SERVICE_NAMES_SHOWN:
        shown += "…"
    return CheckResult("ok", "services", f"{len(names)} detected: {shown}")


def _check_index_cache(root: str, cfg: Config, graph_readable: bool) -> CheckResult:
    if not graph_readable:
        return CheckResult("warn", "index cache", "skipped (no readable graph)")
    graph_path = _overarching_graph_path(root)
    try:
        load_bundle(
            graph_path, cfg.graph.skip_patterns,
            relation_weights=cfg.query.edge_relation_weights, repo_root=root,
        )
    except Exception:
        return CheckResult("warn", "index cache", "index build failed — will rebuild on query")
    return CheckResult("ok", "index cache", "index builds in-process")


def run_doctor(root: str, config_path: str | None = None) -> int:
    root = os.path.abspath(root)
    results = [_check_graphify_binary()]
    cfg, config_result = _check_config(config_path)
    results.append(config_result)
    graph_result, graph_readable = _check_graph(root)
    results.append(graph_result)
    results.append(_check_staleness(root))
    results.append(_check_api_key(root, cfg))
    results.append(_check_mode(root, cfg))
    results.append(_check_services(root, cfg))
    results.append(_check_index_cache(root, cfg, graph_readable))

    for result in results:
        print(f"  [{result.status}] {result.label} — {result.detail}")

    counts = {"ok": 0, "warn": 0, "fail": 0}
    for result in results:
        counts[result.status] += 1
    print()
    print(f"{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail")
    return 1 if counts["fail"] else 0
