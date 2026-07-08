from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

from graphnav import CodexNotFoundError, CodexTimeoutError
from graphnav.config import Config
from graphnav.graph_query import RankedFile
from graphnav.pathsafe import safe_join


def _read_file(path: str, max_chars: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 1)
        if len(content) > max_chars:
            truncated = content[:max_chars]
            last_newline = truncated.rfind("\n")
            if last_newline > 0:
                truncated = truncated[:last_newline]
            return truncated + "\n[... truncated ...]"
        return content
    except OSError:
        return "[FILE NOT FOUND ON DISK]"


def build_prompt(
    prompt: str,
    ranked_files: list[RankedFile],
    cfg: Config,
    project_root: str,
) -> str:
    parts: list[str] = []

    if ranked_files:
        parts.append(
            "You have access to the following files from the codebase, "
            "selected as most relevant to your task. Use them as context when answering.\n"
        )
        for rf in ranked_files:
            abs_path = safe_join(project_root, rf.source_file)
            header = f"=== FILE: {rf.source_file}"
            if cfg.context.show_scores:
                header += f" (score: {rf.score:.2f})"
            header += " ==="
            if abs_path is None:
                content = "[path outside repo root — skipped]"
            else:
                content = _read_file(abs_path, cfg.context.max_file_chars)
            parts.append(f"{header}\n---\n{content}\n---\n")
        parts.append("=== END OF CONTEXT ===\n")

    parts.append(f"USER TASK:\n{prompt}")
    return "\n".join(parts)


def _stream_fd(src, dst) -> None:
    for line in src:
        dst.write(line)
        dst.flush()


def run(
    prompt: str,
    ranked_files: list[RankedFile],
    cfg: Config,
    project_root: str,
) -> int:
    codex_path = shutil.which(cfg.codex.command)
    if codex_path is None:
        raise CodexNotFoundError(
            f"'{cfg.codex.command}' not found on PATH.\n"
            "Install Codex CLI: npm install -g @openai/codex"
        )

    enriched = build_prompt(prompt, ranked_files, cfg, project_root)

    args = [codex_path]
    if cfg.codex.subcommand:
        args.append(cfg.codex.subcommand)

    if cfg.codex.inject_via == "stdin":
        args += ["-", "-C", project_root, "--color", "never"]
    else:
        args += [enriched, "-C", project_root, "--color", "never"]

    args += cfg.codex.extra_args

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if cfg.codex.inject_via == "stdin" else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except PermissionError as e:
        raise CodexNotFoundError(str(e))

    if cfg.codex.inject_via == "stdin":
        proc.stdin.write(enriched)
        proc.stdin.close()

    stderr_thread = threading.Thread(
        target=_stream_fd, args=(proc.stderr, sys.stderr), daemon=True
    )
    stderr_thread.start()

    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()

    stderr_thread.join()

    try:
        proc.wait(timeout=cfg.codex.timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise CodexTimeoutError(
            f"codex did not complete within {cfg.codex.timeout_seconds}s"
        )

    return proc.returncode
