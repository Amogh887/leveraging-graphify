"""Adversarial barrage: probes for weak points across security, cross-platform
behavior, and search quality.

Passing tests are regression guards for behavior that is already correct.
`xfail(strict=True)` tests assert the *desired* behavior for a confirmed
weakness — they show up as `xfailed` today and will flip to `XPASS` (a loud
signal) the moment the weakness is fixed. Each carries a WEAKNESS: note.
"""
from __future__ import annotations

import json
import os
import pickle
import time

import pytest

from graphnav import graph_cache, multirepo
from graphnav.graph_query import _tokenize
from graphnav.mcp_server import GraphTools


def _write_graph(root, graph):
    out = root / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(json.dumps(graph))
    return root


@pytest.fixture(autouse=True)
def _no_autorebuild(monkeypatch):
    monkeypatch.setenv("GRAPHNAV_NO_AUTO_REBUILD", "1")
    graph_cache.clear_memo()
    yield
    graph_cache.clear_memo()


# ===========================================================================
# Section A — Security
# ===========================================================================

def test_poisoned_cache_does_not_execute_code(tmp_path):
    marker = tmp_path / "code_ran"

    class Evil:
        def __reduce__(self):
            return (os.system, (f"touch {marker}",))

    repo = _write_graph(tmp_path / "repo", {
        "nodes": [{"id": 1, "source_file": "a.py", "label": "foo",
                   "file_type": "code", "source_location": "L1"}],
        "links": [],
    })
    gp = str(repo / "graphify-out" / "graph.json")
    poisoned = repo / "graphify-out" / ".graphnav-cache.pkl"
    with open(poisoned, "wb") as f:
        pickle.dump(Evil(), f)

    bundle = graph_cache.load_bundle(gp, [], repo_root=str(repo))
    assert not marker.exists(), "pickle payload executed — cache load is an RCE sink"
    assert bundle.nav.find_symbols("foo")


def test_context_pack_cannot_read_via_parent_traversal(tmp_path):
    (tmp_path / "secret.py").write_text("SECRET_TOKEN = 'leaked'\n")
    repo = _write_graph(tmp_path / "repo", {
        "nodes": [{"id": 1, "source_file": "../secret.py", "label": "SECRET_TOKEN",
                   "file_type": "code", "source_location": "L1"}],
        "links": [],
    })
    out = multirepo.build_context_pack_inline(str(repo), "SECRET_TOKEN")
    assert "SECRET_TOKEN = 'leaked'" not in out


def test_context_pack_cannot_read_absolute_path(tmp_path):
    secret = tmp_path / "abs_secret.py"
    secret.write_text("ABS_SECRET = 42\n")
    repo = _write_graph(tmp_path / "repo", {
        "nodes": [{"id": 1, "source_file": str(secret), "label": "ABS_SECRET",
                   "file_type": "code", "source_location": "L1"}],
        "links": [],
    })
    out = multirepo.build_context_pack_inline(str(repo), "ABS_SECRET")
    assert "ABS_SECRET = 42" not in out


def test_mcp_read_region_blocks_traversal(tmp_path):
    """Positive control: the MCP read_region tool DOES contain paths (_safe_path).
    This is the exact protection build_context_pack_inline is missing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.txt").write_text("MCP_SECRET\n")
    tools = GraphTools(str(repo))
    for rel in ("../outside.txt", "/etc/hostname", "sub/../../outside.txt"):
        out = tools.read_region(rel, 1, 5)
        assert "MCP_SECRET" not in out and out.startswith("error:")


# ===========================================================================
# Section B — Cross-platform & monorepo-layout coverage
# ===========================================================================

@pytest.mark.xfail(strict=True, reason=(
    "WEAKNESS(medium): _service_of splits source_file on '/' only. graphify on "
    "Windows emits backslash paths, so cross-service bridges are never detected "
    "there — the monorepo map silently shows zero connections."
))
def test_bridges_detected_with_backslash_paths(tmp_path):
    graph = {
        "nodes": [
            {"id": 1, "source_file": "svc-a\\x.py", "label": "A"},
            {"id": 2, "source_file": "svc-b\\y.py", "label": "B"},
        ],
        "links": [{"source": 1, "target": 2, "relation": "calls",
                   "source_file": "svc-a\\x.py"}],
    }
    gp = tmp_path / "g.json"
    gp.write_text(json.dumps(graph))
    svcs = [
        multirepo.ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a" / "g.json")),
        multirepo.ServiceInfo("svc-b", str(tmp_path / "svc-b"), str(tmp_path / "svc-b" / "g.json")),
    ]
    bridges = multirepo.analyze_bridges(str(gp), svcs)
    assert sum(len(v) for v in bridges.values()) == 1


@pytest.mark.xfail(strict=True, reason=(
    "WEAKNESS(medium): detect_services only scans ONE level below root, so the most "
    "common JS monorepo layouts (packages/*, apps/*, services/*) collapse into a "
    "single graph with no bridges. resolve_services returns single=True for pnpm/nx/"
    "lerna/turborepo repos."
))
def test_nested_packages_layout_detects_services(tmp_path):
    root = tmp_path / "repo"
    for svc in ("auth", "api"):
        d = root / "packages" / svc
        d.mkdir(parents=True)
        (d / "package.json").write_text("{}")
        (d / "index.js").write_text("function x(){}\n")
    services, single = multirepo.resolve_services(str(root), ["package.json"])
    assert not single and {s.name for s in services} == {"auth", "api"}


def test_flat_multiservice_layout_still_works(tmp_path):
    """Positive control: the flat layout (services as top-level dirs) is detected."""
    root = tmp_path / "repo"
    for svc in ("frontend", "backend"):
        d = root / svc
        d.mkdir(parents=True)
        (d / "package.json").write_text("{}")
        (d / "m.js").write_text("f(){}\n")
    services, single = multirepo.resolve_services(str(root), ["package.json"])
    assert not single and len(services) == 2


# ===========================================================================
# Section C — Search quality / recall
# ===========================================================================

@pytest.mark.parametrize("singular,plural", [
    ("query", "queries"), ("directory", "directories"), ("gateway", "gateways"),
])
def test_regular_plurals_share_token(singular, plural):
    assert set(_tokenize(singular)) & set(_tokenize(plural))


@pytest.mark.xfail(strict=True, reason=(
    "WEAKNESS(low): the stemmer only handles regular -s/-es/-ies plurals. Irregular "
    "plurals common in code vocab (index/indices, child/children) don't unify, so a "
    "search for one term misses symbols named with the other."
))
@pytest.mark.parametrize("a,b", [("index", "indices"), ("child", "children")])
def test_irregular_plurals_share_token(a, b):
    assert set(_tokenize(a)) & set(_tokenize(b))


# ===========================================================================
# Section D — Robustness confirmations (these should all PASS)
# ===========================================================================

def test_tokenizer_is_not_quadratic_on_pathological_input():
    t0 = time.perf_counter()
    _tokenize(("Aa0" * 20000) + ("A" * 200000))
    assert (time.perf_counter() - t0) < 1.0  # linear-ish, no ReDoS


def test_read_region_handles_out_of_range_lines(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.py").write_text("a\nb\nc\n")
    tools = GraphTools(str(repo))
    assert "a" in tools.read_region("f.py", -100, 2)
    assert "c" in tools.read_region("f.py", 1, 10 ** 12)


@pytest.mark.parametrize("graph", [
    [1, 2, 3], "not-a-graph", {"nodes": None, "links": []},
    {"nodes": {"a": 1}}, {"nodes": ["str"], "links": None},
])
def test_context_pack_survives_malformed_graph(tmp_path, graph):
    repo = tmp_path / "repo"
    (repo / "graphify-out").mkdir(parents=True)
    (repo / "graphify-out" / "graph.json").write_text(json.dumps(graph))
    out = multirepo.build_context_pack_inline(str(repo), "anything")
    assert "Context for" in out
