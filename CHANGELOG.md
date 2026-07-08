# Changelog

All notable changes to GraphNav are documented here. Versions follow [Semantic Versioning](https://semver.org/).

---

## [2.0.3] — 2026-07-09

### Security
- **Removed the pickle-based on-disk graph cache, which could execute arbitrary code.** Queries deserialized `graphify-out/.graphnav-cache.pkl` with `pickle.load`, and the version/stamp guards only ran *after* deserialization — so a poisoned cache file dropped into `graphify-out/` executed code the instant any query ran. Graph indexes are now cached only in-process; there is no on-disk cache, and a cold query rebuilds the index in ~150ms.
- **Context-pack and codex-prompt file reads are now contained to the repository root.** `graphnav context` and the codex prompt builder joined graph `source_file` values onto the repo root and read them without containment, so a crafted or shared `graph.json` with a `../secret.py` or absolute `source_file` could read files outside the repository and embed them in the LLM context. All file reads now resolve through a shared containment check (the same guard the MCP `read_region` tool already used) and paths that escape the repo root are skipped. Legitimate repo-relative paths are unaffected.

## [2.0.2] — 2026-06-23

### Fixed
- **`graphnav context`/`find`/`neighbors`/`impact` no longer surface GraphNav's own generated playbook files as relevant code.** Re-running `graphnav map` re-extracts the whole repo, which re-ingests the `CLAUDE.md`/`AGENTS.md`/`.github/copilot-instructions.md` it had just written as ordinary `document` nodes — and those nodes were weighted *above* real code in context ranking, so the playbook could outrank the file you actually needed. These three generated files are now excluded from the graph the same way other generated artifacts already are.
- **Manifest/config files (`package.json`, `tsconfig.json`, `pyproject.toml`, etc.) no longer surface as symbol-search or fuzzy-fallback results.** The underlying `graphify` extractor tags manifest keys (e.g. a `package.json`'s `name`/`version`/`scripts`) as `file_type: "code"`, so an unrelated or mistyped query could fall through to the character-similarity fallback and return a manifest key as if it were a code symbol. `find`/`neighbors`/`impact` now exclude manifest-derived nodes the same way non-code `document` nodes already were excluded in 2.0.1.
- **`graphnav context --locations-only` no longer tells you to check "Cross-service impact" when no such section was shown.** The closing `## Next` block referenced a "Cross-service impact" section unconditionally, even on single-folder projects or monorepo runs with no relevant cross-service edges, where that section is omitted. The reminder now only appears when the section was actually printed.

## [2.0.1] — 2026-06-18

### Fixed
- **A malformed but valid-JSON `graph.json` no longer crashes with a traceback.** Graph reads across `graphnav context`, `find`/`neighbors`/`impact`, and the MCP server guarded only `(JSONDecodeError, KeyError, OSError)`, so a graph that parsed as JSON but had the wrong shape (a top-level list, `nodes: null`, `nodes` as a table, or a node that isn't an object) escaped as an uncaught `AttributeError`/`TypeError`. The graph index and navigator now tolerate these shapes, and a clearly-corrupt graph degrades to the existing "could not be read — run `graphnav map`" message instead of crashing.
- **A graph node missing an `id` no longer crashes `graphnav map` or `graphnav context`.** `analyze_bridges` indexed nodes with `node["id"]` while the rest of the code used `node.get("id")`; a single id-less node raised an uncaught `KeyError` in the bridge analysis that `partition_graph` already tolerated. Both paths now skip id-less and non-object nodes consistently.
- **`graphnav neighbors`/`impact` no longer report a Markdown or other non-code node as a symbol's definition.** The fuzzy fallback iterated every node, so a documentation node could be returned as a symbol's `defined_at` with a bogus blast radius. The fallback now matches only code nodes, matching `find`'s behavior.
- **Singular identifiers ending in `-us`/`-is` are no longer over-stemmed.** Tokens like `status`, `analysis`, `focus`, and `virus` were truncated to `statu`/`analysi`/`focu`/`viru`, so a search for `status` missed code naming `statuses`. These endings are now preserved.
- **`context.max_file_chars` is now clamped to a non-negative value**, the one numeric config field that previously accepted a negative value silently and produced nonsensical file truncation.

---

## [2.0.0] — 2026-06-17

### Changed (breaking)
- **The Python module is now `graphnav` (was `codex_graph`).** The PyPI package and CLI were already `graphnav`; the import name now matches. CLI and MCP users are unaffected; anyone importing `codex_graph` directly must switch to `import graphnav`.
- **`graphnav map` builds a local AST-only graph by default, even when an API key is present.** LLM-based semantic extraction is now strictly opt-in via `graphnav map --semantic` (or `[mono] semantic = true`). Previously a key in the environment silently triggered an LLM build that sent your source to the provider. This makes the default fully local, free, and offline, and keeps outward-facing actions explicit.

### Added
- **Trust & transparency for first-run.** A README "Is this safe?" section documenting the local-by-default, no-telemetry behavior and exactly how `.env` is used; PyPI metadata (long description, author, project URLs, classifiers) and signed provenance attestations on publish.
- **`--offline` flag and `GRAPHNAV_OFFLINE=1`** to force the free local build even when a key is present.
- **`graphnav doctor` now reports a `mode` line** — local (no network/LLM/cost) vs semantic (sends code to the provider) — and an explicit egress notice is printed before any semantic build sends source to an LLM.

### Fixed
- `graphnav doctor` no longer reports a not-yet-built graph as a hard `[fail]`; a fresh repo now reads as a `[warn]` ("not built yet — run `graphnav map`") so a clean setup isn't flagged as broken.

---

## [1.4.1] — 2026-06-16

### Fixed
- **Keyless rebuilds now actually refresh the graph.** `graphify update` (the no-key build path added in 1.4.0) silently skips with "outputs left untouched" whenever a `graph.json` already exists, so after the first build a key-less repo's auto-rebuild never picked up code changes and staleness never cleared. The keyless path now removes the stale `graph.json` before rebuilding (the `cache/` is kept, so rebuilds stay fast); the keyed `extract` path is unchanged.

---

## [1.4.0] — 2026-06-15

### Added
- **Zero-key setup.** `graphnav map`/`watch` now build a free AST-only graph when no API key is present — symbols, call edges, and cross-service bridges all work with no key and no cost. Previously the first command failed hard with `error: backend 'claude' requires ANTHROPIC_API_KEY`. When a key *is* found (Anthropic, OpenAI, Gemini, or DeepSeek), the richer semantic `graphify extract --backend …` path is used automatically. Key detection is backend-agnostic, so an `OPENAI_API_KEY` is honored just like an Anthropic key.

### Fixed
- **Automatic background rebuilds no longer loop forever without a key.** Because rebuilds shell out to `map`, a key-less repo previously failed every rebuild silently while every query kept announcing "rebuild started" — the graph never refreshed. With the free build path the background rebuild now succeeds and the graph actually goes fresh.
- **`graphnav neighbors`/`impact` no longer list a symbol's own defining file as a "caller."** Structural `contains` edges (file → symbol) are filtered out, so callers/callees reflect real call and import relationships.
- **Unknown `graphify_backend` values are validated.** A typo like `"claud"` now warns and falls back to `claude` (known: `claude`, `openai`, `gemini`, `deepseek`, `ollama`) instead of surfacing a cryptic subprocess error.
- `graphnav doctor` now reports a missing API key as `ok` (free AST-only build available) rather than a warning.

---

## [1.3.0] — 2026-06-12

### Added
- **Automatic background graph rebuilds.** Every graph query (`context`, `find`, `neighbors`, `impact`, and all MCP tools) now checks whether source files changed since the graph was built; if so, it spawns a background `graphnav map` and tells the agent a refresh is underway. The graph stays fresh with zero user action — no daemon required. Concurrent rebuilds are prevented via a pid file and a 60s cooldown.
- If no graph exists yet when an agent queries, the build starts automatically and the agent is told to retry shortly.
- Opt out with `auto_rebuild = false` under `[mono]` in `config.toml`, or `GRAPHNAV_NO_AUTO_REBUILD=1`.

### Changed
- `graphnav watch` is now an optional eager mode (rebuild on every save) rather than the only way to keep the graph fresh.

---

## [1.2.3] — 2026-06-12

### Fixed
- **Flat projects with `src/`/`tests/` subfolders are no longer misdetected as monorepos.** A repo is treated as a monorepo only when a subdirectory has its own manifest (e.g. `package.json`, `pyproject.toml`); otherwise the whole repo is mapped as one graph, so root-level code is never orphaned.
- A malformed `config.toml` no longer crashes every command (falls back to defaults with a warning); a foreign `config.toml` (e.g. Hugo's) with none of graphnav's sections is ignored instead of spamming warnings; wrong-typed config values are replaced with defaults instead of raising `TypeError`.
- `graphnav neighbors`/`impact` now prefer an exact symbol-name match (`main` no longer resolves to `test_main`).
- A corrupt or mid-write `graph.json` no longer crashes `map`, the `watch` daemon, `find`/`neighbors`/`impact`, or the MCP tools — all report a friendly "run `graphnav map`" message and `watch` retries on the next update.
- `graphnav watch` now shuts down cleanly on SIGTERM (not just Ctrl-C), terminating its `graphify` child instead of orphaning it.
- `GraphNav` now reads graphs that use an `edges` key instead of `links`, matching every other reader.
- `.env` files saved with a UTF-8 BOM are parsed correctly; `OPENAI_KEY`/`GEMINI_KEY`/`DEEPSEEK_KEY` are now accepted as aliases like `ANTHROPIC_KEY`.
- All generated files are written/read as UTF-8 explicitly, fixing crashes on Windows default encodings.
- Ctrl-C during `graphnav map` exits cleanly instead of printing a traceback.
- `graphnav doctor` recognizes single-project (flat) repos instead of failing the services check.
- Truncated inline context packs no longer emit unbalanced code fences.

---

## [1.2.2] — 2026-06-12

### Added
- **One-command setup.** Running bare `graphnav` from any project root now does the entire setup — auto-detects the project shape, builds the graph, and writes all agent instruction files — then stops with a "nothing else to run" message. No need to know `map`.
- **Single-folder project support.** `graphnav` now works on a flat repo (code at the root with no service subfolders), mapping the whole repo as one graph. Previously it errored with "No services detected".

### Changed
- README and CLI help reframed around the single `graphnav` command; `context`/`serve`/`find`/`neighbors`/`impact` are now clearly labeled as agent-facing commands you rarely run by hand.

### Fixed
- `graphify` binary lookup now also searches every install scheme's scripts directory (via `sysconfig`), so it resolves under `pipx`, `--user`, and other installs where the scripts dir isn't on `PATH`.

---

## [1.2.1] — 2026-06-11

### Fixed
- `graphify` binary lookup now falls back to the Python interpreter's `bin` directory when it isn't on `PATH` (affects `pip install --user` and un-activated venv installs).

---

## [1.2.0] — 2026-06-11

### Added
- `graphnav doctor` — validates the entire setup (binary, config, graph freshness, API key, services, index cache) in one command, exits non-zero only on hard failures.
- Shared graph bundle loader with on-disk pickle cache; repeated `find`/`context`/`neighbors`/`impact` calls no longer re-parse `graph.json`. Set `GRAPHNAV_NO_CACHE=1` to opt out.
- Git-recency ranking signal — recently-changed files get a nudge up in context results. Configurable via `recency_boost_weight` in `config.toml`.
- Relation-weighted call-edge expansion — `calls`/`inherits` edges count more than `imports` above `references` when expanding graph neighbours. Configurable via `[query.edge_relation_weights]`.
- Fuzzy symbol fallback — `find`, `neighbors`, and `impact` fall back to closest-matching symbols on a miss and flag the result.
- Config validation with unknown-key warnings at startup.
- `graphnav watch` now debounces rapid file saves, skips artifact rewrites when content is unchanged (no mtime churn), and restarts the underlying `graphify watch` process with exponential backoff (1 s → 60 s cap) on crash.
- MCP server reloads the graph automatically when `graph.json` changes; CLI queries are cached per process.

---

## [1.1.0] — 2026-05-01

### Added
- `graphnav serve` — MCP server over stdio exposing `graph_context`, `graph_find`, `graph_neighbors`, `read_region`, and `impact` as native agent tools. `mcp` promoted to a core dependency so this works on a plain `pip install graphnav`.
- `graphnav impact` — blast-radius query: lists every symbol that would break if you change the target.
- Git-SHA staleness detection — `graphnav context` prefixes output with a warning when the graph was built on an older commit than the current `HEAD`.
- Call-edge expansion in ranking — files reachable from strong query matches are pulled in via graph edges even when their own text doesn't match.
- `graphnav context` now defaults to inline code regions (pass `--locations-only` for the old `file:line` index).

### Changed
- Package renamed to `graphnav` (was `codex-graphify` / `repomap`).
- Agent instruction blocks are now written behind `<!-- graphnav:start -->` / `<!-- graphnav:end -->` markers so re-running `map` doesn't clobber hand-written content.
- `CLAUDE.md` written alongside `AGENTS.md` and `.github/copilot-instructions.md`.

---

## [0.1.0] — 2026-04-15

Initial release.

- `graphnav map` — auto-detects service boundaries, extracts a single overarching Graphify knowledge graph, partitions it per service, writes `SYMBOLS.md`, `BRIDGES.md`, `MONOREPO_MAP.md`, and coding playbooks to `CLAUDE.md`, `AGENTS.md`, and `.github/copilot-instructions.md`.
- `graphnav context` — token-budgeted context pack for a coding task, free and instant (no LLM call).
- `graphnav watch` — long-running daemon keeping all graphs and generated files up to date.
- `graphnav find` / `neighbors` — shell graph queries.
- Single overarching graph across the whole monorepo surfaces cross-service call edges that per-service extraction misses.
- `config.toml` support for backend, poll interval, token budget, skip dirs, and ranking weights.
