from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from graphnav.config import (
    BACKEND_KEY_VARS,
    MonoConfig,
    QueryConfig,
    backend_has_key,
    backend_provider,
)
from graphnav.pathsafe import safe_join


def find_graphify() -> str | None:
    path = shutil.which("graphify")
    if path:
        return path
    exe = "graphify.exe" if os.name == "nt" else "graphify"
    search_dirs = [os.path.dirname(sys.executable)]
    import sysconfig

    for scheme in sysconfig.get_scheme_names():
        try:
            d = sysconfig.get_path("scripts", scheme)
        except Exception:
            d = None
        if d:
            search_dirs.append(d)
    seen: set[str] = set()
    for d in search_dirs:
        if not d or d in seen:
            continue
        seen.add(d)
        candidate = os.path.join(d, exe)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _any_subdir_has_marker(
    root: str,
    marker_files: list[str],
    extra_skip_dirs: list[str] | None = None,
) -> bool:
    skip_dirs = SKIP_DIRS | frozenset(extra_skip_dirs or ())
    try:
        entries = os.listdir(root)
    except OSError:
        return False
    for entry in entries:
        abs_path = os.path.join(root, entry)
        if not os.path.isdir(abs_path) or entry in skip_dirs or entry.startswith("."):
            continue
        if any(os.path.exists(os.path.join(abs_path, m)) for m in marker_files):
            return True
    return False


def resolve_services(
    root: str,
    marker_files: list[str],
    extra_skip_dirs: list[str] | None = None,
) -> tuple[list[ServiceInfo], bool]:
    if _any_subdir_has_marker(root, marker_files, extra_skip_dirs):
        services = detect_services(root, marker_files, extra_skip_dirs)
        if services:
            return services, False
    skip_dirs = SKIP_DIRS | frozenset(extra_skip_dirs or ())
    if _has_source_files(root, skip_dirs=skip_dirs):
        name = os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "repo"
        root_service = ServiceInfo(
            name=name,
            abs_path=root,
            graph_path=_overarching_graph_path(root),
        )
        return [root_service], True
    return [], False


def _warn(msg: str) -> None:
    print(f"[graphnav] warning: {msg}", file=sys.stderr)


def _write_if_changed(path: str, content: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == content:
                return False
    except (OSError, UnicodeDecodeError):
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


SOURCE_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".swift", ".scala",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm", ".dart", ".ex", ".exs",
})

SKIP_DIRS = frozenset({
    "node_modules", "dist", "build", "out", "target", "vendor", "bin", "obj",
    "__pycache__", "graphify-out", "venv", ".venv", "env", "site-packages",
    ".next", ".nuxt", "coverage", "test-results", "playwright-report",
    ".pytest_cache", ".mypy_cache", ".git", ".github", ".idea", ".vscode",
})


def _find_env_file(start: str) -> str | None:
    current = os.path.abspath(start)
    while True:
        candidate = os.path.join(current, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _parse_env_file(path: str) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env_vars


def _env_file_sources(root: str) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()

    def _add(path: str | None) -> None:
        if path and path not in seen and os.path.isfile(path):
            seen.add(path)
            sources.append(path)

    _add(_find_env_file(root))
    _add(_find_env_file(os.getcwd()))
    for base in (root, os.getcwd()):
        try:
            for entry in sorted(os.listdir(base)):
                _add(os.path.join(base, entry, ".env"))
        except OSError:
            pass
    return sources


_KEY_ALIASES = {
    "ANTHROPIC_KEY": "ANTHROPIC_API_KEY",
    "OPENAI_KEY": "OPENAI_API_KEY",
    "GEMINI_KEY": "GEMINI_API_KEY",
    "DEEPSEEK_KEY": "DEEPSEEK_API_KEY",
}


def _load_env_file(root: str) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for path in _env_file_sources(root):
        for key, value in _parse_env_file(path).items():
            env_vars.setdefault(key, value)
    for alias, canonical in _KEY_ALIASES.items():
        if alias in env_vars and canonical not in env_vars:
            env_vars[canonical] = env_vars[alias]
    return env_vars


def _build_subprocess_env(root: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(_load_env_file(root))
    return env


@dataclass
class ServiceInfo:
    name: str
    abs_path: str
    graph_path: str
    bridges_to: list[str] = field(default_factory=list)


@dataclass
class RestartBackoff:
    initial: float = 1.0
    cap: float = 60.0
    stable_reset: float = 60.0
    started_at: float | None = None
    delay: float = 0.0

    def record_start(self, now: float) -> None:
        self.started_at = now

    def next_delay(self, now: float) -> float:
        if self.started_at is not None and now - self.started_at >= self.stable_reset:
            self.delay = 0.0
        self.delay = self.initial if self.delay == 0 else min(self.delay * 2, self.cap)
        return self.delay


@dataclass
class BridgeRow:
    local_file: str
    local_symbol: str
    relation: str
    remote_svc: str
    remote_file: str
    remote_symbol: str
    local_loc: str = ""
    remote_loc: str = ""


def _has_source_files(path: str, max_depth: int = 4, skip_dirs: frozenset[str] = SKIP_DIRS) -> bool:
    base = path.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(path):
        depth = dirpath.count(os.sep) - base
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [
                d for d in dirnames
                if d not in skip_dirs and not d.startswith(".")
            ]
        for fn in filenames:
            if os.path.splitext(fn)[1] in SOURCE_EXTENSIONS:
                return True
    return False


def detect_services(
    root: str,
    marker_files: list[str],
    extra_skip_dirs: list[str] | None = None,
) -> list[ServiceInfo]:
    services = []
    marker_set = set(marker_files)
    skip_dirs = SKIP_DIRS | frozenset(extra_skip_dirs or ())
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    for entry in sorted(entries):
        abs_path = os.path.join(root, entry)
        if not os.path.isdir(abs_path):
            continue
        if entry in skip_dirs or entry.startswith("."):
            continue
        has_marker = any(
            os.path.exists(os.path.join(abs_path, marker)) for marker in marker_set
        )
        if has_marker or _has_source_files(abs_path, skip_dirs=skip_dirs):
            services.append(ServiceInfo(
                name=entry,
                abs_path=abs_path,
                graph_path=os.path.join(abs_path, "graphify-out", "graph.json"),
            ))
    return services


def _stream_proc(proc: subprocess.Popen, timeout: int) -> int:
    def _relay(src, dst):
        for line in src:
            dst.write(line)
            dst.flush()

    t_out = threading.Thread(target=_relay, args=(proc.stdout, sys.stderr), daemon=True)
    t_err = threading.Thread(target=_relay, args=(proc.stderr, sys.stderr), daemon=True)
    t_out.start()
    t_err.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    return proc.returncode


def run_extract(
    service: ServiceInfo,
    graphify_path: str,
    backend: str,
    timeout: int = 600,
    env: dict[str, str] | None = None,
    semantic: bool = False,
) -> int:
    resolved_env = env if env is not None else dict(os.environ)
    if semantic and backend_has_key(backend, resolved_env):
        print(
            f"[graphnav] semantic extraction via '{backend}' — your source is sent to "
            f"{backend_provider(backend)}'s API and may incur cost.",
            file=sys.stderr,
        )
        cmd = [graphify_path, "extract", service.abs_path, "--backend", backend, "--out", service.abs_path]
    else:
        if semantic and not backend_has_key(backend, resolved_env):
            print(
                f"[graphnav] --semantic requested but no API key for '{backend}' found — "
                f"building a free local AST-only graph instead.",
                file=sys.stderr,
            )
        else:
            print(
                "[graphnav] building a free local AST-only graph (no network, no LLM, no cost). "
                "Use `graphnav map --semantic` for richer LLM-derived links.",
                file=sys.stderr,
            )
        try:
            os.remove(service.graph_path)
        except OSError:
            pass
        cmd = [graphify_path, "update", service.abs_path]
    print(f"[graphnav] extracting {service.name} ...", file=sys.stderr)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    return _stream_proc(proc, timeout)


AUTO_REBUILD_COOLDOWN = 60.0


def _newest_source_mtime(root: str, max_depth: int = 4) -> float:
    newest = 0.0
    base = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.count(os.sep) - base
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
            ]
        for fn in filenames:
            if os.path.splitext(fn)[1] in SOURCE_EXTENSIONS:
                try:
                    mt = os.stat(os.path.join(dirpath, fn)).st_mtime
                except OSError:
                    continue
                if mt > newest:
                    newest = mt
    return newest


def graph_is_stale(root: str) -> bool:
    try:
        graph_mtime = os.stat(_overarching_graph_path(root)).st_mtime
    except OSError:
        return True
    return _newest_source_mtime(root) > graph_mtime + 1.0


def maybe_auto_rebuild(root: str, enabled: bool = True) -> bool:
    if not enabled or os.environ.get("GRAPHNAV_NO_AUTO_REBUILD") == "1":
        return False
    root = os.path.abspath(root)
    if not graph_is_stale(root):
        return False
    out_dir = os.path.join(root, "graphify-out")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return False
    pid_path = os.path.join(out_dir, ".graphnav-rebuild.pid")
    try:
        st = os.stat(pid_path)
        with open(pid_path) as f:
            pid = int(f.read().strip() or 0)
        if pid > 0:
            try:
                os.kill(pid, 0)
                return False
            except OSError:
                pass
        if time.time() - st.st_mtime < AUTO_REBUILD_COOLDOWN:
            return False
    except (OSError, ValueError):
        pass
    log_path = os.path.join(out_dir, "auto-rebuild.log")
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "graphnav.cli", "map", "--root", root],
                stdout=log, stderr=log, start_new_session=True,
                env=_build_subprocess_env(root),
            )
        with open(pid_path, "w") as f:
            f.write(str(proc.pid))
        return True
    except OSError:
        return False


def _overarching_graph_path(root: str) -> str:
    return os.path.join(root, "graphify-out", "graph.json")


def _graph_meta_path(root: str) -> str:
    return os.path.join(root, "graphify-out", ".graphnav-meta.json")


def _git_sha(root: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _commits_between(root: str, a: str, b: str) -> int:
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-list", "--count", f"{a}..{b}"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return int(out.stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return 0


def write_graph_meta(root: str) -> None:
    meta = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "git_sha": _git_sha(root)}
    path = _graph_meta_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def staleness_note(root: str) -> str:
    path = _graph_meta_path(root)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    built_sha = meta.get("git_sha")
    current = _git_sha(root)
    if not built_sha or not current or built_sha == current:
        return ""
    behind = _commits_between(root, built_sha, current)
    span = f"{behind} commit(s)" if behind else "some commits"
    return (
        f"> ⚠️ Knowledge graph is stale: built at {built_sha[:8]}, HEAD is now "
        f"{current[:8]} ({span} later). Line numbers may have drifted — "
        "re-run `graphnav map`."
    )


def _overarching_service(root: str) -> ServiceInfo:
    return ServiceInfo(
        name="overarching (whole repo)",
        abs_path=root,
        graph_path=_overarching_graph_path(root),
    )


def build_overarching_graph(
    root: str,
    graphify_path: str,
    backend: str,
    timeout: int = 1200,
    env: dict[str, str] | None = None,
    semantic: bool = False,
) -> int:
    return run_extract(
        _overarching_service(root), graphify_path, backend,
        timeout=timeout, env=env, semantic=semantic,
    )


def _graph_links(graph: dict) -> list[dict]:
    links = graph.get("links")
    if links is None:
        links = graph.get("edges", [])
    return links


def partition_graph(
    overarching_graph_path: str,
    services: list[ServiceInfo],
) -> dict[str, int]:
    with open(overarching_graph_path, encoding="utf-8") as f:
        graph = json.load(f)
    if not isinstance(graph, dict):
        graph = {}

    service_names = {s.name for s in services}
    node_svc: dict[str, str] = {}
    per_nodes: dict[str, list[dict]] = {s.name: [] for s in services}
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        svc = _service_of(node.get("source_file", ""), service_names)
        if svc is not None:
            node_svc[node.get("id")] = svc
            per_nodes[svc].append(node)

    per_links: dict[str, list[dict]] = {s.name: [] for s in services}
    for link in _graph_links(graph):
        src_svc = node_svc.get(link.get("source"))
        tgt_svc = node_svc.get(link.get("target"))
        if src_svc is not None and src_svc == tgt_svc:
            per_links[src_svc].append(link)

    base_meta = {k: v for k, v in graph.items() if k not in ("nodes", "links", "edges")}
    counts: dict[str, int] = {}
    for svc in services:
        out_dir = os.path.join(svc.abs_path, "graphify-out")
        os.makedirs(out_dir, exist_ok=True)
        subgraph = dict(base_meta)
        subgraph["nodes"] = per_nodes[svc.name]
        subgraph["links"] = per_links[svc.name]
        _write_if_changed(svc.graph_path, json.dumps(subgraph, indent=2))
        counts[svc.name] = len(per_nodes[svc.name])
    return counts


def _service_of(source_file: str, service_names: set[str]) -> str | None:
    if not source_file:
        return None
    prefix = source_file.split("/")[0]
    return prefix if prefix in service_names else None


def analyze_bridges(
    overarching_graph_path: str,
    services: list[ServiceInfo],
) -> dict[str, list[BridgeRow]]:
    with open(overarching_graph_path, encoding="utf-8") as f:
        graph = json.load(f)
    if not isinstance(graph, dict):
        graph = {}

    service_names = {s.name for s in services}
    node_by_id: dict[str, dict] = {}
    for n in graph.get("nodes", []):
        if isinstance(n, dict) and n.get("id") is not None:
            node_by_id[n["id"]] = n
    bridges: dict[str, list[BridgeRow]] = {s.name: [] for s in services}

    for link in _graph_links(graph):
        src_node = node_by_id.get(link.get("source", ""))
        tgt_node = node_by_id.get(link.get("target", ""))
        if not src_node or not tgt_node:
            continue

        src_svc = _service_of(src_node.get("source_file", ""), service_names)
        tgt_svc = _service_of(tgt_node.get("source_file", ""), service_names)

        if not src_svc or not tgt_svc or src_svc == tgt_svc:
            continue

        link_sf = link.get("source_file", "")
        local_svc = _service_of(link_sf, service_names) or src_svc

        if local_svc == src_svc:
            local_node, remote_node, remote_svc = src_node, tgt_node, tgt_svc
        else:
            local_node, remote_node, remote_svc = tgt_node, src_node, src_svc

        local_file = local_node.get("source_file", "").removeprefix(local_svc + "/")
        bridges[local_svc].append(BridgeRow(
            local_file=local_file,
            local_symbol=local_node.get("label", ""),
            relation=link.get("relation", ""),
            remote_svc=remote_svc,
            remote_file=remote_node.get("source_file", ""),
            remote_symbol=remote_node.get("label", ""),
            local_loc=local_node.get("source_location", ""),
            remote_loc=remote_node.get("source_location", ""),
        ))

    for svc in services:
        remote_svcs = sorted({r.remote_svc for r in bridges[svc.name]})
        svc.bridges_to = remote_svcs

    return bridges


def write_bridges_md(service: ServiceInfo, rows: list[BridgeRow]) -> str:
    out_dir = os.path.join(service.abs_path, "graphify-out")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "BRIDGES.md")
    lines = [f"# Bridges: {service.name}", ""]
    if not rows:
        lines.append("_No cross-service connections detected._")
    else:
        lines.append(
            "> Editing a Local symbol below may require changes to the Remote symbol. "
            'Run `graphify affected "<symbol>"` to confirm impact before changing it.'
        )
        lines.append("")
        lines.append("| Local File | Symbol | Loc | Relation | → Service | Remote File | Remote Symbol | Loc |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r.local_file} | {r.local_symbol} | {r.local_loc} | {r.relation} | "
                f"{r.remote_svc} | {r.remote_file} | {r.remote_symbol} | {r.remote_loc} |"
            )
    _write_if_changed(path, "\n".join(lines) + "\n")
    return path


def _symbols_by_file(graph: dict, prefix_strip: str = "") -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    raw_nodes = graph.get("nodes") if isinstance(graph, dict) else None
    for node in raw_nodes if isinstance(raw_nodes, list) else []:
        if not isinstance(node, dict) or node.get("file_type") != "code":
            continue
        sf = node.get("source_file", "")
        label = node.get("label", "")
        if not sf or not label or label == os.path.basename(sf):
            continue
        if os.path.splitext(sf)[1] not in SOURCE_EXTENSIONS:
            continue
        key = sf
        if prefix_strip and key.startswith(prefix_strip + "/"):
            key = key[len(prefix_strip) + 1:]
        out.setdefault(key, []).append((label, node.get("source_location", "")))
    return out


def write_symbols_md(service: ServiceInfo) -> str:
    out_dir = os.path.join(service.abs_path, "graphify-out")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "SYMBOLS.md")
    try:
        with open(service.graph_path, encoding="utf-8") as f:
            graph = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"could not read {service.graph_path} ({type(exc).__name__}) — symbols index will be empty")
        graph = {"nodes": []}

    by_file = _symbols_by_file(graph, prefix_strip=service.name)
    lines = [f"# Symbols: {service.name}", ""]
    if not by_file:
        lines.append("_No code symbols extracted._")
    else:
        lines.append("Open a symbol by its `file:line` instead of reading whole files.")
        lines.append("")
        for sf in sorted(by_file):
            lines.append(f"## {sf}")
            for label, loc in by_file[sf]:
                lines.append(f"- {label}{(' — ' + loc) if loc else ''}")
            lines.append("")
    _write_if_changed(path, "\n".join(lines).rstrip() + "\n")
    return path


def write_monorepo_map(root: str, services: list[ServiceInfo]) -> str:
    out_dir = os.path.join(root, "graphify-out")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "MONOREPO_MAP.md")
    lines = ["# Monorepo Map", "", "| Service | Graph | Bridges To |", "|---|---|---|"]
    for svc in services:
        graph_rel = os.path.relpath(svc.graph_path, root)
        bridges_cell = ", ".join(svc.bridges_to) if svc.bridges_to else "_none_"
        lines.append(f"| {svc.name} | {graph_rel} | {bridges_cell} |")
    _write_if_changed(path, "\n".join(lines) + "\n")
    return path


_BLOCK_START = "<!-- graphnav:start -->"
_BLOCK_END = "<!-- graphnav:end -->"
_LEGACY_MARKERS = (("<!-- codex-graph:start -->", "<!-- codex-graph:end -->"),)


def build_playbook_text(root: str, services: list[ServiceInfo]) -> str:
    svc_names = ", ".join(s.name for s in services) if services else "(single project)"
    lines = [
        "# Coding with the codebase knowledge graph",
        "",
        "This repo has a graphify knowledge graph. Use it as your **first resort** — "
        "never use `find`, `ls`, or `cat` to explore repo structure or understand unfamiliar code.",
        "",
        "**Step 0 — always read the monorepo map first** for any task that isn't a "
        "single-file, single-line change:",
        "```",
        "graphify-out/MONOREPO_MAP.md",
        "```",
        "",
        "**Then judge scope:**",
        "- Single-file, single-line edit (rename, formatting, one-liner)? "
        "Just make it — no further graphify steps needed.",
        "- Everything else — including code changes, explanations, architecture questions, "
        '"how does X work", overviews, or anything touching unfamiliar files:',
        '  1. Run `graphnav context "<task>"` — prints the minimal files, their symbol '
        "`file:line` locations, and any cross-service impact.",
        "  2. Open ONLY those files; read the given `file:line` regions, not whole files.",
        '  3. Before changing a symbol flagged "Cross-service impact", run '
        '`graphnav impact "<symbol>"` to see its blast radius (callers/callees).',
        "  4. Implement (or answer), then run the project's tests if code changed.",
        "",
        "**Never** use `find`/`ls`/`cat` to survey the repo. If graphify doesn't give "
        "enough context, read `<service>/graphify-out/SYMBOLS.md` or "
        "`<service>/graphify-out/BRIDGES.md` next — not a raw directory listing.",
        "",
        f"Services: {svc_names}",
        "On-demand maps (open only when needed): `graphify-out/MONOREPO_MAP.md` · "
        "`<service>/graphify-out/SYMBOLS.md` · `<service>/graphify-out/BRIDGES.md`",
    ]
    return "\n".join(lines)


def _write_managed_block(path: str, content: str) -> None:
    block = f"{_BLOCK_START}\n{content}\n{_BLOCK_END}\n"
    existing = ""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        except OSError:
            existing = ""

    start_marker, end_marker = _BLOCK_START, _BLOCK_END
    if not (start_marker in existing and end_marker in existing):
        for legacy_start, legacy_end in _LEGACY_MARKERS:
            if legacy_start in existing and legacy_end in existing:
                start_marker, end_marker = legacy_start, legacy_end
                break

    if start_marker in existing and end_marker in existing:
        before = existing.split(start_marker, 1)[0]
        after = existing.split(end_marker, 1)[1]
        new_content = before + block.rstrip("\n") + after
    elif existing.strip():
        new_content = existing.rstrip("\n") + "\n\n" + block
    else:
        new_content = block

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _write_if_changed(path, new_content)


def write_copilot_instructions(root: str, services: list[ServiceInfo]) -> str:
    content = build_playbook_text(root, services)
    copilot_path = os.path.join(root, ".github", "copilot-instructions.md")
    _write_managed_block(copilot_path, content)
    _write_managed_block(os.path.join(root, "AGENTS.md"), content)
    _write_managed_block(os.path.join(root, "CLAUDE.md"), content)
    return copilot_path


def build_context_pack(
    root: str,
    task: str,
    top_files: int = 8,
    budget_tokens: int = 2000,
    skip_patterns: list[str] | None = None,
    query_cfg: QueryConfig | None = None,
    mono_cfg: MonoConfig | None = None,
) -> str:
    from graphnav.graph_cache import DEFAULT_PACK_SKIP_PATTERNS, load_bundle
    from graphnav.graph_query import query_files

    root = os.path.abspath(root)
    if mono_cfg is None:
        mono_cfg = MonoConfig()
    rebuild_started = maybe_auto_rebuild(root, enabled=mono_cfg.auto_rebuild)
    overarching_path = _overarching_graph_path(root)
    if not os.path.exists(overarching_path):
        rel = os.path.relpath(overarching_path, root)
        if rebuild_started:
            return (
                f"# Context for: {task}\n\n"
                "Knowledge graph is being built automatically in the background — "
                "retry this in ~30s.\n"
            )
        return (
            f"# Context for: {task}\n\n"
            f"No knowledge graph found at {rel}.\n"
            "Run `graphnav map` (monorepo) or `graphify extract .` first.\n"
        )

    if skip_patterns is None:
        skip_patterns = list(DEFAULT_PACK_SKIP_PATTERNS)
    if query_cfg is None:
        query_cfg = QueryConfig()

    degraded = False
    try:
        bundle = load_bundle(
            overarching_path, skip_patterns,
            relation_weights=query_cfg.edge_relation_weights, repo_root=root,
        )
        ranked = query_files(
            task, bundle.index, top_files,
            query_cfg.community_boost_weight, query_cfg.bm25_k1, query_cfg.bm25_b,
            edge_boost_weight=query_cfg.edge_boost_weight,
            recency=bundle.recency,
            recency_boost_weight=query_cfg.recency_boost_weight,
        )
        by_file = bundle.symbols_by_file
    except (json.JSONDecodeError, KeyError, OSError, ValueError, TypeError, AttributeError) as exc:
        _warn(
            f"graph.json could not be read ({type(exc).__name__}: {exc}) — "
            "run `graphnav map` to rebuild"
        )
        ranked, by_file, degraded = [], {}, True
    selected = [rf.source_file for rf in ranked]

    out_lines = [f"# Context for: {task}", ""]
    note = staleness_note(root)
    if note:
        out_lines += [note, ""]
    if rebuild_started:
        out_lines += ["_Source files changed — automatic graph rebuild started in the background._", ""]
    if degraded:
        out_lines.append(
            "_Knowledge graph could not be read (corrupt or invalid graph.json) — "
            "run `graphnav map`._"
        )
        return "\n".join(out_lines) + "\n"
    if not selected:
        out_lines.append(
            "_No matching files. Try terms from the code itself (function or class names)._"
        )
        return "\n".join(out_lines) + "\n"

    out_lines.append("## Open only these files")
    for sf in selected:
        syms = by_file.get(sf, [])
        if syms:
            shown = ", ".join(f"{label} {loc}".strip() for label, loc in syms[:12])
            out_lines.append(f"- {sf} — {shown}")
        else:
            out_lines.append(f"- {sf}")

    has_cross_service_impact = False
    services = detect_services(root, mono_cfg.marker_files, mono_cfg.extra_skip_dirs)
    if services:
        bridges = analyze_bridges(overarching_path, services)
        sel_set = set(selected)
        impact: list[str] = []
        for svc in services:
            for r in bridges[svc.name]:
                local_full = f"{svc.name}/{r.local_file}"
                if local_full in sel_set or r.remote_file in sel_set:
                    impact.append(
                        f"- {local_full}:{r.local_symbol} {r.local_loc} "
                        f"--{r.relation}--> {r.remote_file}:{r.remote_symbol} {r.remote_loc}"
                    )
        if impact:
            has_cross_service_impact = True
            out_lines.append("")
            out_lines.append("## Cross-service impact")
            out_lines.extend(impact)

    if has_cross_service_impact:
        next_line = (
            "Read only the `file:line` regions above. Before changing a symbol under "
            'Cross-service impact, run `graphify affected "<symbol>"`. Then run the tests.'
        )
    else:
        next_line = "Read only the `file:line` regions above. Then run the tests."
    out_lines += ["", "## Next", next_line]

    text = "\n".join(out_lines) + "\n"
    char_budget = max(budget_tokens, 0) * 4
    if char_budget and len(text) > char_budget:
        text = text[:char_budget].rstrip() + "\n\n_(truncated to budget)_\n"
    return text


def _extract_code_windows(abs_path, lines_wanted, before=2, after=14, max_lines=110):
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            src = f.read().splitlines()
    except OSError:
        return ""
    n = len(src)
    keep = set()
    for ln in lines_wanted:
        if 1 <= ln <= n:
            for i in range(max(1, ln - before), min(n, ln + after) + 1):
                keep.add(i)
    if not keep:
        return ""
    kept = sorted(keep)[:max_lines]
    pieces = []
    prev = None
    for i in kept:
        if prev is not None and i > prev + 1:
            pieces.append("        ...")
        pieces.append(f"{i:>5}  {src[i - 1]}")
        prev = i
    return "\n".join(pieces)


def build_context_pack_inline(
    root, task, top_files=3, budget_tokens=2500, skip_patterns=None, query_cfg=None,
    auto_rebuild=True,
):
    from graphnav.graph_cache import DEFAULT_PACK_SKIP_PATTERNS, load_bundle
    from graphnav.graph_query import query_files

    root = os.path.abspath(root)
    rebuild_started = maybe_auto_rebuild(root, enabled=auto_rebuild)
    overarching_path = _overarching_graph_path(root)
    if not os.path.exists(overarching_path):
        if rebuild_started:
            return (
                f"# Context for: {task}\n\n"
                "Knowledge graph is being built automatically in the background — "
                "retry this in ~30s.\n"
            )
        return f"# Context for: {task}\n\nNo knowledge graph found.\n"
    if skip_patterns is None:
        skip_patterns = list(DEFAULT_PACK_SKIP_PATTERNS)
    if query_cfg is None:
        query_cfg = QueryConfig()

    degraded = False
    bundle = None
    try:
        bundle = load_bundle(
            overarching_path, skip_patterns,
            relation_weights=query_cfg.edge_relation_weights, repo_root=root,
        )
        ranked = query_files(
            task, bundle.index, top_files,
            query_cfg.community_boost_weight, query_cfg.bm25_k1, query_cfg.bm25_b,
            edge_boost_weight=query_cfg.edge_boost_weight,
            recency=bundle.recency,
            recency_boost_weight=query_cfg.recency_boost_weight,
        )
        by_file = bundle.symbols_by_file
    except (json.JSONDecodeError, KeyError, OSError, ValueError, TypeError, AttributeError) as exc:
        _warn(
            f"graph.json could not be read ({type(exc).__name__}: {exc}) — "
            "run `graphnav map` to rebuild"
        )
        ranked, by_file, degraded = [], {}, True

    out = [f"# Context for: {task}", ""]
    note = staleness_note(root)
    if note:
        out += [note, ""]
    if rebuild_started:
        out += ["_Source files changed — automatic graph rebuild started in the background._", ""]
    out.append(
        "## Relevant code (extracted from the knowledge graph — already in context, do not re-open these files)"
    )
    if degraded:
        out.append(
            "_Knowledge graph could not be read (corrupt or invalid graph.json) — "
            "run `graphnav map`._"
        )
        return "\n".join(out) + "\n"
    if not ranked:
        out.append("_No confident matches; explore normally._")
        return "\n".join(out) + "\n"

    for rf in ranked:
        sf = rf.source_file
        syms = by_file.get(sf, [])
        line_nums = []
        for _label, loc in syms:
            m = re.search(r"L(\d+)", loc or "")
            if m:
                line_nums.append(int(m.group(1)))
        safe = safe_join(root, sf)
        snippet = _extract_code_windows(safe, line_nums) if safe is not None else ""
        out.append("")
        out.append(f"### {sf}")
        if syms:
            out.append("symbols: " + ", ".join(label for label, _ in syms[:10]))
        if snippet:
            out.append("```")
            out.append(snippet)
            out.append("```")

    refs = bundle.nav.references_to([rf.source_file for rf in ranked], limit=12)
    if refs:
        out.append("")
        out.append("## Other code that references the above (likely also needs edits)")
        out.extend("- " + r for r in refs)

    out += [
        "",
        "## Next",
        "The relevant code is shown above. Make the change directly; only open a file "
        "if you need a region not shown. To explore further, use the graph tools "
        "(graph_find, graph_neighbors) instead of broad searches.",
    ]
    text = "\n".join(out) + "\n"
    char_budget = max(budget_tokens, 0) * 4
    if char_budget and len(text) > char_budget:
        truncated = text[:char_budget].rstrip()
        if truncated.count("```") % 2 == 1:
            truncated += "\n```"
        text = truncated + "\n\n_(truncated to budget)_\n"
    return text


def _refresh(
    root: str,
    services: list[ServiceInfo],
    overarching_graph_path: str,
    single: bool = False,
) -> dict[str, list[BridgeRow]]:
    if single:
        bridges = {s.name: [] for s in services}
    else:
        partition_graph(overarching_graph_path, services)
        bridges = analyze_bridges(overarching_graph_path, services)
    for svc in services:
        write_bridges_md(svc, bridges[svc.name])
        write_symbols_md(svc)
    write_monorepo_map(root, services)
    write_copilot_instructions(root, services)
    write_graph_meta(root)
    return bridges


def run_map(
    root: str,
    mono_cfg: MonoConfig,
    backend_override: str | None = None,
    dry_run: bool = False,
    semantic: bool = False,
    offline: bool = False,
) -> int:
    root = os.path.abspath(root)
    graphify_path = find_graphify()
    if graphify_path is None:
        print("Error: 'graphify' not found. Install with: pip install graphifyy", file=sys.stderr)
        return 1

    services, single = resolve_services(root, mono_cfg.marker_files, mono_cfg.extra_skip_dirs)
    if not services:
        print(f"No source code found in {root}. Run graphnav from a directory that contains code.", file=sys.stderr)
        return 1

    offline = offline or os.environ.get("GRAPHNAV_OFFLINE") == "1"
    use_semantic = (semantic or mono_cfg.semantic) and not offline
    shape = "whole repo (single project)" if single else f"{len(services)} service(s): {', '.join(s.name for s in services)}"
    if dry_run:
        print(f"Detected {shape}:")
        for svc in services:
            print(f"  {svc.name}  {svc.abs_path}")
        mode = "semantic (LLM)" if use_semantic else "local AST-only (no network, no cost)"
        print(f"[dry-run] Build mode: {mode}. No graphify calls made.")
        return 0

    backend = backend_override or mono_cfg.graphify_backend
    env = _build_subprocess_env(root)
    overarching_path = _overarching_graph_path(root)
    used_llm = use_semantic and backend_has_key(backend, env)

    print(f"[graphnav] Building knowledge graph for {shape} ...", file=sys.stderr)
    rc = build_overarching_graph(root, graphify_path, backend, env=env, semantic=use_semantic)
    if rc != 0 or not os.path.exists(overarching_path):
        print(f"Error: graphify extraction failed (exit {rc}).", file=sys.stderr)
        if used_llm:
            key_hint = " or ".join(BACKEND_KEY_VARS.get(backend, ())) or "the backend's API key"
            print(f"  Check your '{backend}' backend ({key_hint}) or re-run; graphify may be misconfigured.", file=sys.stderr)
        else:
            print("  The free AST-only build failed — re-run, or delete graphify-out/ and try again.", file=sys.stderr)
        return 1

    try:
        bridges = _refresh(root, services, overarching_path, single=single)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: extracted graph could not be read ({type(exc).__name__}: {exc}).", file=sys.stderr)
        print("  Re-run `graphnav map`; if it persists, delete graphify-out/ and try again.", file=sys.stderr)
        return 1
    total_bridges = sum(len(rows) for rows in bridges.values())

    print(f"\nSetup complete. Your AI coding agents are now configured for this repo.")
    if single:
        print(f"  Knowledge graph      : {overarching_path}")
        print(f"  Symbol index         : {os.path.join(root, 'graphify-out', 'SYMBOLS.md')}")
    else:
        print(f"  {len(services)} service(s) mapped, {total_bridges} cross-service connection(s) found.")
        print(f"  Overarching graph    : {overarching_path}")
        for svc in services:
            to = ", ".join(svc.bridges_to) if svc.bridges_to else "none"
            print(f"  {svc.name}/graphify-out/  (bridges -> {to})")
    print(f"  Agent instructions   : CLAUDE.md, AGENTS.md, .github/copilot-instructions.md")
    print(f"\nNothing else to run. Open the repo in your AI coding tool and start working.")
    print(f"(Optional: `graphnav watch` keeps the graph live as you edit.)")
    return 0


def run_watch(
    root: str,
    mono_cfg: MonoConfig,
    backend_override: str | None = None,
    semantic: bool = False,
    offline: bool = False,
) -> int:
    root = os.path.abspath(root)
    graphify_path = find_graphify()
    if graphify_path is None:
        print("Error: 'graphify' not found. Install with: pip install graphifyy", file=sys.stderr)
        return 1

    services, single = resolve_services(root, mono_cfg.marker_files, mono_cfg.extra_skip_dirs)
    if not services:
        print(f"No source code found in {root}.", file=sys.stderr)
        return 1

    backend = backend_override or mono_cfg.graphify_backend
    offline = offline or os.environ.get("GRAPHNAV_OFFLINE") == "1"
    use_semantic = (semantic or mono_cfg.semantic) and not offline
    env = _build_subprocess_env(root)
    overarching_path = _overarching_graph_path(root)

    if not os.path.exists(overarching_path):
        print(f"[graphnav] Bootstrapping knowledge graph ...", file=sys.stderr)
        rc = build_overarching_graph(root, graphify_path, backend, env=env, semantic=use_semantic)
        if rc != 0 or not os.path.exists(overarching_path):
            print(f"Error: bootstrap extraction failed (exit {rc}).", file=sys.stderr)
            return 1

    try:
        _refresh(root, services, overarching_path, single=single)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"could not read graph.json ({type(exc).__name__}) — will retry as it updates")

    def _start_watch() -> subprocess.Popen:
        return subprocess.Popen(
            [graphify_path, "watch", root],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

    def _sigterm(_signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, OSError):
        pass

    watch_proc = _start_watch()
    backoff = RestartBackoff()
    backoff.record_start(time.monotonic())
    restart_at: float | None = None
    try:
        last_mtime = os.stat(overarching_path).st_mtime
    except OSError:
        last_mtime = 0.0
    pending_mtime: float | None = None

    print(f"[graphnav] Watching {root} ({len(services)} service(s)). Press Ctrl-C to stop.", file=sys.stderr)
    try:
        while True:
            time.sleep(mono_cfg.watch_poll_interval)

            try:
                mtime = os.stat(overarching_path).st_mtime
            except OSError:
                mtime = last_mtime
            if pending_mtime is not None:
                if mtime == pending_mtime:
                    last_mtime = mtime
                    pending_mtime = None
                    ts = time.strftime("%H:%M:%S")
                    print(f"[graphnav] {ts} graph updated — refreshing symbols and bridges ...", file=sys.stderr)
                    try:
                        _refresh(root, services, overarching_path, single=single)
                    except (OSError, json.JSONDecodeError) as exc:
                        _warn(f"could not read graph.json ({type(exc).__name__}) — will retry on next update")
                        last_mtime = 0.0
                else:
                    pending_mtime = mtime
            elif mtime != last_mtime:
                pending_mtime = mtime

            if watch_proc.poll() is not None:
                now = time.monotonic()
                if restart_at is None:
                    delay = backoff.next_delay(now)
                    print(f"[graphnav] WARNING: graphify watch exited (exit {watch_proc.returncode}), restarting in {delay:.0f}s ...", file=sys.stderr)
                    restart_at = now + delay
                elif now >= restart_at:
                    watch_proc = _start_watch()
                    backoff.record_start(now)
                    restart_at = None

    except KeyboardInterrupt:
        print("\n[graphnav] Stopping watch ...", file=sys.stderr)
        watch_proc.terminate()
        try:
            watch_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            watch_proc.kill()
        return 0
