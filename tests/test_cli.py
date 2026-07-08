from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import write_graph


class TestMonoSubcommandDispatch:
    def test_map_dispatched_to_run_map(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "map", "--root", str(tmp_path), "--dry-run"])
        calls = []
        monkeypatch.setattr(
            "graphnav.multirepo.shutil.which", lambda _: "/graphify"
        )
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code in (0, 1)

    def test_watch_dispatched_to_run_watch(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "watch", "--root", str(tmp_path)])
        monkeypatch.setattr("graphnav.multirepo.shutil.which", lambda _: None)
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 1

    def test_map_help_exits_0(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "map", "--help"])
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "map" in out.lower() or "monorepo" in out.lower()

    def test_watch_help_exits_0(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "watch", "--help"])
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 0

    def test_map_dry_run_flag(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "svc-a").mkdir()
        (tmp_path / "svc-a" / "pyproject.toml").touch()
        (tmp_path / "svc-b").mkdir()
        (tmp_path / "svc-b" / "package.json").touch()
        monkeypatch.setattr(sys, "argv", ["codex-graph", "map", "--root", str(tmp_path), "--dry-run"])
        monkeypatch.setattr("graphnav.multirepo.shutil.which", lambda _: "/graphify")
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "svc-a" in out
        assert "[dry-run]" in out

    def test_backend_flag_forwarded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "map", "--root", str(tmp_path), "--backend", "openai", "--dry-run"])
        monkeypatch.setattr("graphnav.multirepo.shutil.which", lambda _: "/graphify")
        with pytest.raises(SystemExit):
            from graphnav.cli import main
            main()


class TestContextCommand:
    def test_context_dispatched_and_prints_pack(self, tmp_path, monkeypatch, capsys):
        captured = {}

        def fake_pack(root, task, skip_patterns=None, **kw):
            captured["task"] = task
            captured["root"] = root
            return "# Context for: " + task

        monkeypatch.setattr("graphnav.multirepo.build_context_pack_inline", fake_pack)
        monkeypatch.setattr(sys, "argv", ["codex-graph", "context", "fix the login bug", "--root", str(tmp_path)])
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 0
        assert captured["task"] == "fix the login bug"
        assert "Context for: fix the login bug" in capsys.readouterr().out

    def test_context_defaults_to_inline(self, tmp_path, monkeypatch):
        used = {"inline": False, "locations": False}
        monkeypatch.setattr(
            "graphnav.multirepo.build_context_pack_inline",
            lambda **kw: used.__setitem__("inline", True) or "",
        )
        monkeypatch.setattr(
            "graphnav.multirepo.build_context_pack",
            lambda **kw: used.__setitem__("locations", True) or "",
        )
        monkeypatch.setattr(sys, "argv", ["codex-graph", "context", "task", "--root", str(tmp_path)])
        with pytest.raises(SystemExit):
            from graphnav.cli import main
            main()
        assert used["inline"] and not used["locations"]

    def test_context_locations_only_uses_index_pack(self, tmp_path, monkeypatch):
        used = {"inline": False, "locations": False}
        monkeypatch.setattr(
            "graphnav.multirepo.build_context_pack_inline",
            lambda **kw: used.__setitem__("inline", True) or "",
        )
        monkeypatch.setattr(
            "graphnav.multirepo.build_context_pack",
            lambda **kw: used.__setitem__("locations", True) or "",
        )
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "context", "task", "--root", str(tmp_path), "--locations-only"
        ])
        with pytest.raises(SystemExit):
            from graphnav.cli import main
            main()
        assert used["locations"] and not used["inline"]

    def test_context_forwards_budget_and_files(self, tmp_path, monkeypatch):
        captured = {}

        def fake_pack(root, task, skip_patterns=None, top_files=None, budget_tokens=None, query_cfg=None, **kw):
            captured["top_files"] = top_files
            captured["budget_tokens"] = budget_tokens
            return ""

        monkeypatch.setattr("graphnav.multirepo.build_context_pack_inline", fake_pack)
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "context", "task", "--root", str(tmp_path), "--budget", "500", "--files", "3"
        ])
        with pytest.raises(SystemExit):
            from graphnav.cli import main
            main()
        assert captured["budget_tokens"] == 500
        assert captured["top_files"] == 3

    def test_context_no_task_exits_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "context", "--root", str(tmp_path)])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 1


class TestAutoMap:
    def test_no_args_with_services_runs_map(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "svc-a").mkdir()
        (tmp_path / "svc-a" / "pyproject.toml").touch()
        (tmp_path / "svc-b").mkdir()
        (tmp_path / "svc-b" / "package.json").touch()
        monkeypatch.setattr(sys, "argv", ["codex-graph"])
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

        called_with = {}

        def fake_run_map(root, mono_cfg, backend_override=None, dry_run=False):
            called_with["root"] = root
            return 0

        monkeypatch.setattr("graphnav.multirepo.run_map", fake_run_map)
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 0
        assert str(tmp_path) == called_with["root"]

    def test_no_args_without_source_exits_with_guidance(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["codex-graph"])
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "No source code" in err

    def test_no_args_flat_repo_runs_map(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "app.py").write_text("def main():\n    return 1\n")
        monkeypatch.setattr(sys, "argv", ["codex-graph"])
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

        called = {}

        def fake_run_map(root, mono_cfg, backend_override=None, dry_run=False):
            called["root"] = root
            return 0

        monkeypatch.setattr("graphnav.multirepo.run_map", fake_run_map)
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 0
        assert called["root"] == str(tmp_path)


class TestExistingPromptPathUnaffected:
    def test_list_files_uses_existing_graph(self, tmp_path, monkeypatch, capsys):
        graph_dir = tmp_path / "graphify-out"
        graph_dir.mkdir()
        write_graph(
            graph_dir / "graph.json",
            nodes=[{"id": "n1", "label": "user model schema", "source_file": "models.py",
                    "file_type": "code", "community": 0}],
        )
        config_file = tmp_path / "config.toml"
        config_file.write_text(f'[graph]\npath = "graphify-out/graph.json"\nproject_root = "."\n')
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "--config", str(config_file), "--graph", str(graph_dir / "graph.json"),
            "--list-files", "user model"
        ])
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "models.py" in out

    def test_no_context_flag_skips_graph(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "--no-context", "--dry-run", "my task"
        ])
        graph_dir = tmp_path / "graphify-out"
        graph_dir.mkdir()
        write_graph(graph_dir / "graph.json")
        config_file = tmp_path / "config.toml"
        config_file.write_text(f'[graph]\npath = "graphify-out/graph.json"\nproject_root = "."\n')
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "--config", str(config_file), "--graph", str(graph_dir / "graph.json"),
            "--no-context", "--dry-run", "do something"
        ])
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 0

    def test_prompt_with_missing_graph_exits_2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "--graph", str(tmp_path / "nonexistent.json"), "my prompt"
        ])
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 2


class TestIndexCacheUsed:
    def test_second_find_run_skips_index_build(self, tmp_path, monkeypatch, capsys):
        import os

        from graphnav.cli import main
        from graphnav.graph_cache import clear_memo
        from graphnav.graph_query import GraphIndex
        from tests.conftest import write_graph

        graph_path = tmp_path / "graphify-out" / "graph.json"
        write_graph(graph_path, [
            {"id": "create_incident", "label": "create_incident", "source_file": "api/views.py",
             "file_type": "code", "source_location": "L2", "community": 0},
        ], [])
        argv = ["graphnav", "find", "incident", "--root", str(tmp_path)]
        clear_memo()
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            main()
        assert not os.path.exists(str(graph_path.parent / ".graphnav-cache.pkl"))
        calls = {"n": 0}
        original = GraphIndex.__init__

        def counting(self, *args, **kwargs):
            calls["n"] += 1
            original(self, *args, **kwargs)

        monkeypatch.setattr(GraphIndex, "__init__", counting)
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            main()
        assert calls["n"] == 0
        assert "create_incident" in capsys.readouterr().out


class TestDoctorDispatch:
    def test_doctor_empty_root_fails(self, tmp_path, monkeypatch, capsys):
        from graphnav import doctor

        monkeypatch.setattr(doctor, "find_graphify", lambda: None)
        monkeypatch.setattr(sys, "argv", ["graphnav", "doctor", "--root", str(tmp_path)])
        with pytest.raises(SystemExit) as exc:
            from graphnav.cli import main
            main()
        assert exc.value.code == 1
        assert "[fail]" in capsys.readouterr().out
