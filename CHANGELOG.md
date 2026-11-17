# Changelog

Version numbers follow `major.minor.patch`: a bug fix bumps patch, a new feature bumps minor,
a change that breaks existing use bumps major.

## 1.0.0 — unreleased

First release.

- Hosted version (`pip install "tokenwise-coder[server]"`, `tokenwise server web|worker|migrate`):
  a GitHub App that scans the default branch on every push, keeps one risk comment per pull
  request (drafts included) up to date, and turns a finding into a draft PR on a
  `tokenwise/fix-N` branch once an organization owner approves the Agent's proposal. PostgreSQL
  holds the data and the job queue (`FOR UPDATE SKIP LOCKED`). Organizations with one-member
  personal accounts, membership checked on every request, model keys encrypted and brought by
  the customer, a repository's `tokenwise.toml` limited to its check and file choices, all data
  deleted when the app is removed. Dockerfile, docker-compose.yml and docs/hosting.md.
- `sandbox = "docker"`: the check runs in a container with no network, a read-only system, no
  capabilities and CPU/memory/process limits. Without Docker the check is skipped, not failed.
- `scan_tools` / `tokenwise scan --tools ruff,bandit,coverage`: findings from Ruff, Bandit and
  an existing coverage report. A tool that did not run does not resolve its earlier findings.
- ReleaseGuard shows the changed functions with their callers and the tests that call them
  (Python with `ast`; JS/TS with Tree-sitter via the `impact` extra).
- `tokenwise calibrate`: replays past commits as PRs, measures each risk signal's AUC against
  later fixes and suggests weights; `--save` makes `tokenwise risk` use them for the project.
- `tokenwise findings --stats`: per rule, how many findings were resolved and how many a person
  ignored (false alarm rate).

- Prices can have a long-prompt tier (`long_threshold`, `long_multiplier`): Claude Haiku 5.5
  costs 5x above 100K prompt tokens, which the cost ledger now counts. Overrides take two
  optional extra values: `[input, cache_write, cache_read, output, long_threshold, long_multiplier]`.
- `tokenwise serve`: a local web page for one or more projects (`--root` repeated). Chat,
  model choice, file picker (the selector is skipped for picked files), token and cost estimate
  before running, diffs (unified or side by side) with Apply / Discard / Undo, weekly budget.
  Install with `pip install "tokenwise-coder[web]"`. It listens on 127.0.0.1 only; another
  `--host` needs `--token`, which every API call must send.
- Planning in the page: a goal is split into work threads with a ready-to-paste prompt, the
  files, a check command, a level and the threads they depend on. Plans are saved in
  `.tokenwise/plans/` with each thread's status, and can be exported as Markdown.
  "Planı sırayla çalıştır" runs the threads in order, waiting for Apply or Discard on each.
- Commit from the page: an applied change can be committed to git, containing only the files
  tokenwise changed.
- Chat mode in the page works as a conversation kept per project (`.tokenwise/sessions/`). Each
  message is routed to a question, an explanation, a plan, or a code change. Vague requests get
  clarifying questions first; the next message answers them. "Düzelt" changes the last proposal
  without starting over; "İncele" asks a second model to review a proposal.
- After Apply the project check runs. If it fails, the page offers "Geri al" (undo) and
  "Hatayla düzelt" (sends the check output back as a correction).
- Voice: "Sesle yaz" dictates into the input (Turkish, where the browser supports it);
  "yanıtı sesli oku" reads replies aloud.
- Plan chat: a plan can be changed with an instruction. Threads that keep their title keep their
  status.
- `pipeline.run(files=...)`: the caller can name the files, skipping the selector model call.
- CI runs the tests on Windows too.
- One platform, three modules on a shared core. The core (`graph.py`, `history.py`) reads the
  import graph of Python and JS/TS files and the git history (churn, fix commits, files that
  change together, AI co-authored commits).
- RepoDoctor: `tokenwise scan` runs deterministic analyzers (complexity, long functions, large
  files, missing tests, import cycles, shell/eval/unsafe deserialization, hard-coded secrets,
  TODO density) and keeps findings in `.tokenwise/health.db` with a line-independent
  fingerprint: open, resolved, reopened or ignored. Category scores and a snapshot per scan.
  `tokenwise findings` ranks them by severity and hotspot; `--report` prints the weekly report.
- ReleaseGuard: `tokenwise risk --base main` scores the branch's change on six signals, each
  with its evidence and confidence, and prints a pre-release checklist and agent tasks
  (`--format markdown|json`, optional `--fail-above`).
- `tokenwise fix <id>`: the agent fixes a finding, then a re-scan verifies it is gone. The web
  page has a health panel, the findings list with one-click fixes and the risk report.
- `examples/github/tokenwise.yml`: risk report on every pull request, weekly health scan.
- The router no longer treats topic nouns (`cache`, `database`, `schema`, `protocol`) as a sign
  of a hard change; "add --json to the cache command" now starts below the top model.
- Files defining a function or class named in the request are added to the selection
  (first) and focused on that symbol; invented paths (`core/isci_takip.py`) and copied
  placeholders (`path/to/predict.py`) are matched to the one real file with that name.
- A check that exits with 77 is "skipped", not failed: the GitHub tasks' checks skip
  when node, php or javac is not installed. Check output is read as UTF-8 on Windows.
- Guard: a change that leaves a Python file unparsable or uncompilable ('return' outside a
  function), or removes a name the file or
  another module still uses, goes back to the model as a failed attempt.
- `map_max_tokens`: large projects get a shorter map (tests as paths, then names only,
  then top-level names, then paths); 8000 for local models, 16000 otherwise.
- Failed runs report their last problem and, in benchmark results, the model's last reply.
- `<think>` blocks from reasoning models are removed before parsing.
- Check commands quote `{checks}` and `{python}`, so paths with spaces
  (`C:\Users\ADIL CEVIK\...`) work on Windows.
- `benchmark` shows the diff and check output of failed runs, writes them under
  "Failures" in the results, saves results next to the tasks file by default, and keeps
  going when one task's prompt does not fit the local model's context.
- `provider = "ollama"` uses Ollama's own API and sizes the context window to each
  prompt (`max_context` caps it); its OpenAI endpoint silently cut long prompts.
  `init --local` now writes it.
- `tokenwise doctor`: config, server reachability, configured models present or missing.
- Model errors (server down, unknown model, HTTP errors) are one readable line, not a traceback.
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
