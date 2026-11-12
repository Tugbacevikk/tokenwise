# Changelog

Version numbers follow `major.minor.patch`: a bug fix bumps patch, a new feature bumps minor,
a change that breaks existing use bumps major.

## 1.0.0 — unreleased

First release.

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
