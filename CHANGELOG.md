# Changelog

Version numbers follow `major.minor.patch`: a bug fix bumps patch, a new feature bumps minor,
a change that breaks existing use bumps major.

## 1.0.0 — unreleased

First release.

- Benchmark tasks can pin a GitHub repository at a commit (`repo`, `ref`, `subdir`);
  `benchmark/github/` has 20 tasks from real projects with checks and an offline estimate.
- The router sizes a focused file by the part that is sent, not the whole file.
- `budget_usd_week`: refuse new requests once the last 7 days cost that much
  (`--over-budget` to override); shown after each request, in the report and in VS Code.
- Map support for Java, Kotlin, C#, Go, Rust and PHP, and JS/TS class methods.
- Large files are sent focused: header, the functions the selector named
  (`path::name`), and a list of the rest; `NEED_FILES` gets the whole file.
- `check` setting / `--check`: a change must pass a command (usually the tests) in a
  throwaway copy before it is offered; failures go back to the model, then up the ladder.
- Line endings (CRLF/LF) and BOM are kept when a file is edited; non-UTF-8 files get a
  clear error.
- Edit blocks from small local models are accepted even with decorated or missing paths,
  loose markers or wrong indentation; SEARCH only matches at line starts.

- `tokenwise map`: project map with function and class signatures only, and how much
  smaller it is than the full source.
- `tokenwise ask`: the cheapest model picks the needed files from the map, a rule-based
  router picks where to start on the model ladder, the editor returns SEARCH/REPLACE edit
  blocks, failures climb to the next model; diff preview, `--dry-run`, `-y`, `--model`.
- `tokenwise apply` / `tokenwise undo`.
- Claude API (with prompt caching) and any OpenAI-compatible server (Ollama, LM Studio, vLLM);
  `tokenwise init --local`.
- Reply cache in `.tokenwise/cache.db`, keyed on the full prompt.
- Cost ledger in `.tokenwise/ledger.db` and `tokenwise report` (recent window, all time,
  by model, `--csv`, `--json`).
- `tokenwise benchmark`: baseline vs tokenwise on TOML-defined tasks in throwaway copies,
  judged by each task's check command, with a markdown results table.
- VS Code extension: ask, diff view, apply/discard/undo, cost panel and status bar.
