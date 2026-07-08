from __future__ import annotations

import json
import os
import pickle
import struct
import tempfile
from pathlib import Path

import pytest

from graphnav.graph_cache import _MEMO, clear_memo, load_bundle
from graphnav.graph_query import RankedFile
from graphnav.pathsafe import safe_join
from tests.conftest import write_graph

NODES_BASE = [
    {
        "id": "alpha_func",
        "label": "alpha_func",
        "source_file": "pkg/mod.py",
        "file_type": "code",
        "source_location": "L1",
        "community": 0,
    },
]
LINKS_BASE: list[dict] = []


def graph_file(root: Path) -> str:
    return str(root / "graphify-out" / "graph.json")


@pytest.fixture(autouse=True)
def fresh_memo():
    clear_memo()
    yield
    clear_memo()


@pytest.fixture()
def no_git(monkeypatch):
    monkeypatch.setattr("graphnav.multirepo._git_sha", lambda root: None)


@pytest.fixture()
def graph_root(tmp_path):
    write_graph(tmp_path / "graphify-out" / "graph.json", NODES_BASE, LINKS_BASE)
    return tmp_path


class _MaliciousPickle:
    def __init__(self, sentinel: Path):
        self._sentinel = str(sentinel)

    def __reduce__(self):
        import os as _os
        return (_os.system, (f"touch {self._sentinel}",))


class TestPickleRCEGone:
    def test_malicious_pkl_not_loaded(self, graph_root, no_git, tmp_path):
        sentinel = tmp_path / "PWNED"
        out_dir = graph_root / "graphify-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        pkl_path = out_dir / ".graphnav-cache.pkl"
        payload = pickle.dumps(_MaliciousPickle(sentinel))
        pkl_path.write_bytes(payload)

        bundle = load_bundle(graph_file(graph_root), repo_root=str(graph_root))

        assert not sentinel.exists(), (
            "Sentinel was created — pickle RCE payload was executed via load_bundle"
        )
        assert bundle is not None
        assert bundle.nav is not None
        assert bundle.index is not None

    def test_no_pkl_written_after_load(self, graph_root, no_git):
        load_bundle(graph_file(graph_root), repo_root=str(graph_root))
        out_dir = graph_root / "graphify-out"
        pkl_files = list(out_dir.glob("*.pkl"))
        assert pkl_files == [], (
            f"Unexpected .pkl files found after load_bundle: {pkl_files}"
        )

    def test_bundle_nav_and_index_usable(self, graph_root, no_git):
        bundle = load_bundle(graph_file(graph_root), repo_root=str(graph_root))
        assert bundle.nav.find_symbols("alpha") is not None
        ranked = bundle.index.rank("alpha", 5, 2.0, 1.5, 0.75)
        assert isinstance(ranked, list)


class TestPathContainmentMultirepo:
    def _make_crafted_root(self, tmp_path: Path, secret_content: str) -> tuple[Path, Path]:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "graphify-out").mkdir()

        secret_dir = tmp_path / "outside"
        secret_dir.mkdir()
        secret_file = secret_dir / "secret.py"
        secret_file.write_text(secret_content)
        return root, secret_file

    def _write_crafted_graph(
        self, root: Path, source_file_value: str, label: str = "secrettoken"
    ) -> str:
        nodes = [
            {
                "id": "crafted_node",
                "label": label,
                "source_file": source_file_value,
                "file_type": "code",
                "source_location": "L1",
                "community": 0,
            }
        ]
        graph_path = root / "graphify-out" / "graph.json"
        write_graph(graph_path, nodes, [])
        return str(graph_path)

    def test_relative_escape_not_in_context_pack(self, tmp_path, no_git):
        from graphnav.multirepo import build_context_pack_inline

        root, secret_file = self._make_crafted_root(tmp_path, "SUPERSECRET_CONTENT_XYZ")
        rel_escape = "../outside/secret.py"
        self._write_crafted_graph(root, rel_escape, label="secrettoken")

        result = build_context_pack_inline(
            str(root), "secrettoken", top_files=5, auto_rebuild=False
        )
        assert "SUPERSECRET_CONTENT_XYZ" not in result, (
            "Secret file contents appeared in context pack via relative path escape"
        )

    def test_absolute_escape_not_in_context_pack(self, tmp_path, no_git):
        from graphnav.multirepo import build_context_pack_inline

        root, secret_file = self._make_crafted_root(tmp_path, "SUPERSECRET_ABSOLUTE_XYZ")
        self._write_crafted_graph(root, str(secret_file), label="secrettoken")

        result = build_context_pack_inline(
            str(root), "secrettoken", top_files=5, auto_rebuild=False
        )
        assert "SUPERSECRET_ABSOLUTE_XYZ" not in result, (
            "Secret file contents appeared in context pack via absolute path escape"
        )


class TestPathContainmentRunner:
    def _make_runner_setup(self, tmp_path: Path, secret_text: str, source_file_value: str):
        from graphnav.config import Config

        root = tmp_path / "repo"
        root.mkdir()
        secret_file = tmp_path / "secret.py"
        secret_file.write_text(secret_text)

        rf = RankedFile(source_file=source_file_value, score=1.0)
        cfg = Config()
        return root, rf, cfg

    def test_relative_escape_skipped_in_build_prompt(self, tmp_path):
        from graphnav.runner import build_prompt

        root, rf, cfg = self._make_runner_setup(
            tmp_path,
            "RUNNER_SECRET_RELATIVE",
            "../secret.py",
        )
        result = build_prompt("do something", [rf], cfg, str(root))
        assert "RUNNER_SECRET_RELATIVE" not in result, (
            "Runner included secret content via relative escape"
        )
        assert "[path outside repo root — skipped]" in result, (
            "Runner did not emit the skip placeholder for escaped path"
        )

    def test_absolute_escape_skipped_in_build_prompt(self, tmp_path):
        from graphnav.runner import build_prompt

        secret_file = tmp_path / "secret.py"
        secret_file.write_text("RUNNER_SECRET_ABSOLUTE")
        root = tmp_path / "repo"
        root.mkdir()
        rf = RankedFile(source_file=str(secret_file), score=1.0)
        from graphnav.config import Config
        cfg = Config()

        result = build_prompt("do something", [rf], cfg, str(root))
        assert "RUNNER_SECRET_ABSOLUTE" not in result, (
            "Runner included secret content via absolute path escape"
        )
        assert "[path outside repo root — skipped]" in result


class TestPathContainmentMCPServer:
    def _make_tools(self, tmp_path: Path) -> "GraphTools":
        from graphnav.mcp_server import GraphTools

        root = tmp_path / "repo"
        root.mkdir()
        write_graph(root / "graphify-out" / "graph.json", NODES_BASE, LINKS_BASE)
        return GraphTools(str(root), auto_rebuild=False)

    def test_dotdot_relative_path_returns_error(self, tmp_path):
        tools = self._make_tools(tmp_path)
        secret_file = tmp_path / "secret.py"
        secret_file.write_text("MCP_DOTDOT_SECRET")

        result = tools.read_region("../secret.py", 1, 5)
        assert result.startswith("error:"), (
            f"Expected error string, got: {result!r}"
        )
        assert "MCP_DOTDOT_SECRET" not in result

    def test_absolute_path_returns_error(self, tmp_path):
        tools = self._make_tools(tmp_path)
        secret_file = tmp_path / "secret.py"
        secret_file.write_text("MCP_ABSOLUTE_SECRET")

        result = tools.read_region(str(secret_file), 1, 5)
        assert result.startswith("error:"), (
            f"Expected error string, got: {result!r}"
        )
        assert "MCP_ABSOLUTE_SECRET" not in result

    def test_nested_escape_returns_error(self, tmp_path):
        tools = self._make_tools(tmp_path)
        secret_file = tmp_path / "secret.py"
        secret_file.write_text("MCP_NESTED_SECRET")

        result = tools.read_region("sub/../../secret.py", 1, 5)
        assert result.startswith("error:"), (
            f"Expected error string, got: {result!r}"
        )
        assert "MCP_NESTED_SECRET" not in result


class TestSafeJoinUnit:
    def test_normal_relative_path_resolves_inside(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").touch()
        result = safe_join(str(tmp_path), "pkg/mod.py")
        assert result is not None
        assert result == str((tmp_path / "pkg" / "mod.py").resolve())

    def test_dotdot_returns_none(self, tmp_path):
        assert safe_join(str(tmp_path), "..") is None

    def test_absolute_path_escaping_root_returns_none(self, tmp_path):
        assert safe_join(str(tmp_path), "/etc/passwd") is None

    def test_symlink_outside_root_returns_none(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "real.py").touch()
        repo = tmp_path / "repo"
        repo.mkdir()
        link = repo / "link.py"
        link.symlink_to(outside / "real.py")
        result = safe_join(str(repo), "link.py")
        assert result is None, (
            "safe_join should return None for symlink whose target is outside root"
        )

    def test_sibling_directory_with_shared_prefix_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        sibling = tmp_path / "repo-secrets"
        sibling.mkdir()
        (sibling / "x.py").touch()
        result = safe_join(str(repo), "../repo-secrets/x.py")
        assert result is None, (
            "safe_join should reject paths that escape to sibling dirs sharing a name prefix"
        )

    def test_path_equal_to_root_returns_none(self, tmp_path):
        result = safe_join(str(tmp_path), ".")
        assert result is None or result == str(tmp_path.resolve()), (
            "safe_join for root itself should either return root or None (not raise)"
        )

    def test_valid_nested_path_resolves(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "f.py").touch()
        result = safe_join(str(tmp_path), "a/b/c/f.py")
        assert result is not None
        assert result.startswith(str(tmp_path.resolve()))


class TestLegitimatePathNotBlocked:
    def test_in_repo_file_appears_in_context_pack(self, tmp_path, no_git):
        from graphnav.multirepo import build_context_pack_inline

        root = tmp_path / "repo"
        root.mkdir()

        pkg = root / "pkg"
        pkg.mkdir()
        source_file = pkg / "alpha_module.py"
        source_file.write_text("def legitimate_alpha_function():\n    pass\n")

        nodes = [
            {
                "id": "legitimate_alpha_function",
                "label": "legitimate_alpha_function",
                "source_file": "pkg/alpha_module.py",
                "file_type": "code",
                "source_location": "L1",
                "community": 0,
            }
        ]
        write_graph(root / "graphify-out" / "graph.json", nodes, [])

        result = build_context_pack_inline(
            str(root), "legitimate_alpha_function", top_files=3, auto_rebuild=False
        )
        assert "legitimate_alpha_function" in result, (
            "Containment check over-blocked a legitimate in-repo file"
        )
        assert "def legitimate_alpha_function" in result, (
            "Code snippet for legitimate in-repo file was not included in context pack"
        )


class TestInProcessMemo:
    def test_two_calls_same_process_return_same_object(self, graph_root, no_git):
        first = load_bundle(graph_file(graph_root), repo_root=str(graph_root))
        second = load_bundle(graph_file(graph_root), repo_root=str(graph_root))
        assert first is second, "Memo did not return the same cached bundle object"

    def test_no_cache_env_returns_fresh_each_time(self, graph_root, no_git, monkeypatch):
        monkeypatch.setenv("GRAPHNAV_NO_CACHE", "1")
        first = load_bundle(graph_file(graph_root), repo_root=str(graph_root))
        second = load_bundle(graph_file(graph_root), repo_root=str(graph_root))
        assert first is not second, (
            "GRAPHNAV_NO_CACHE=1 should build fresh bundles each call"
        )

    def test_no_cache_env_memo_stays_empty(self, graph_root, no_git, monkeypatch):
        monkeypatch.setenv("GRAPHNAV_NO_CACHE", "1")
        load_bundle(graph_file(graph_root), repo_root=str(graph_root))
        load_bundle(graph_file(graph_root), repo_root=str(graph_root))
        assert _MEMO == {}, (
            "GRAPHNAV_NO_CACHE=1 should leave _MEMO empty after calls"
        )


class TestDoctorNoDiskCacheRef:
    def test_doctor_module_has_no_cache_path_for(self):
        import importlib
        import graphnav.doctor as doctor_mod
        import inspect

        source = inspect.getsource(doctor_mod)
        assert "cache_path_for" not in source, (
            "doctor.py still references cache_path_for — old disk cache not fully removed"
        )

    def test_check_index_cache_runs_without_error(self, tmp_path, no_git):
        from graphnav.config import Config
        from graphnav.doctor import _check_index_cache

        root = tmp_path
        write_graph(root / "graphify-out" / "graph.json", NODES_BASE, LINKS_BASE)
        cfg = Config()
        result = _check_index_cache(str(root), cfg, graph_readable=True)
        assert result.status in ("ok", "warn"), (
            f"_check_index_cache returned unexpected status: {result.status} — {result.detail}"
        )

    def test_check_index_cache_label_says_in_process(self, tmp_path, no_git):
        from graphnav.config import Config
        from graphnav.doctor import _check_index_cache

        root = tmp_path
        write_graph(root / "graphify-out" / "graph.json", NODES_BASE, LINKS_BASE)
        cfg = Config()
        result = _check_index_cache(str(root), cfg, graph_readable=True)
        assert result.status == "ok"
        assert "in-process" in result.detail.lower(), (
            f"Expected 'in-process' in detail, got: {result.detail!r}"
        )
