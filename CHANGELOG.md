# Changelog

Version numbers follow `major.minor.patch`: a bug fix bumps patch, a new feature bumps minor,
a change that breaks existing use bumps major.

## 1.0.0 — unreleased

First release.

- Hosted worker under load and failure, tested against PostgreSQL (`tests/test_server_load.py`):
  8 workers with 300 events arriving, a 50-request webhook storm, a worker process killed
  mid-job, a dropped and a refused database connection. Two fixes came out of it: a job
  put back in the queue while a new job with the same key had just arrived crashed the
  worker (unique index); now the new one takes over. And a lost database connection no
  longer ends the worker: it reconnects with a growing wait and puts its interrupted job
  back at once instead of after 10 minutes.

- Model benchmark on GitHub's machines (`.github/workflows/benchmark.yml`, started by hand):
  the smart and the simple router on the same tasks with OpenRouter models, a spending
  ceiling per task run, and a summary table. Needs the secret `OPENROUTER_API_KEY`.
- A model server behind a refusing proxy or firewall is reported as "cannot reach ..."
  instead of a traceback.

- Small local models: a reply that is the whole file instead of edit blocks is now accepted
  for a file the model saw in full (or a new file), unless it looks shortened; the check
  still decides. `NEED_FILES: cart.py` finds `shop/cart.py`, and asking for a file that does
  not exist or is already shown gets a specific answer instead of a wasted attempt. Seen in
  a benchmark with qwen2.5-coder:7b, where these replies made 12 of 15 runs fail.

- Beyond Python: Go and Java now get complexity and length findings, an import graph
  (missing tests, a change's reach, the callers of a changed function) and the smart
  router's complexity signal. Same-package files are linked by the top-level names they
  use, since neither language imports them. JS/TS imports are read from the syntax tree:
  an `import` in a comment or a string is no longer counted, and a `require()` inside a
  function no longer makes an import cycle. New JS/TS security rules: `eval`,
  `new Function`, string `setTimeout` (dynamic-code), `child_process.exec` and
  `spawn({shell: true})` (shell-command), and `innerHTML`, `document.write`,
  `dangerouslySetInnerHTML` (unsafe-html). The grammars ship in the `impact` extra.

- Smart router (`router = "smart"`, default): the starting model now also depends on the
  complexity of the target functions (Python, JS/TS), how many files import the target,
  and this project's last 30 days: when most requests starting at a level had to climb
  anyway, it starts one rung higher. Every decision says why (CLI "Why:", web "Neden bu
  model"). `router = "simple"` keeps the old wording-and-size rules, for A/B benchmarks.

- OpenRouter provider (`tokenwise init --openrouter`, `provider = "openrouter"`): one key
  for DeepSeek, Qwen, Mistral, Llama, Claude and more. Prices come from OpenRouter's model
  list (cached a day) so costs, the weekly budget and `max_usd_per_request` work for every
  model. Hosted organizations can choose it in the model settings.

- Benchmark measures more than the task's check: regressions (`suite`: the project's own
  tests passing before and failing after), cost per solved task, median time, model calls
  and escalations. `--repeat N` shows how much the solve rate moves between identical
  runs; `--json-out` / `--compare` show what changed between two versions.

- `max_usd_per_request`: a ceiling for one request, escalation included. Each call's
  output is capped to what the rest pays for (pessimistic input estimate); a call that does
  not fit is not made and the best proposal so far is kept. Hosted AI fixes default to
  $0.50 (`TOKENWISE_MAX_USD_PER_FIX`), which a repository cannot raise.
- README: "Bu ölçümlerin sınırları" — what the published numbers do and do not show.

- Independent verification kit (docs/verification.md): `benchmark/reproduce/run.py` and a
  "reproduce measurements" workflow re-run the 12-repository measurements from pinned
  commits and fail when numbers drift; `benchmark/labeling/` builds fixed-seed review sheets
  (findings, "sorunlu" commit labels) for human reviewers and scores them with Wilson
  intervals and Cohen's kappa. `tokenwise calibrate --json --samples` lists each scored
  commit and the fix behind its label.

- Privacy: the worker deletes records past their keeping period about once an hour
  (expired sessions, finished jobs and orphan users after 30 days, removed installations
  after 90). Owners can download everything stored for their organization as JSON; anyone
  can delete their own record ("Hesabımı sil"). `docs/privacy.md`: a draft of what is
  stored, why and for how long, to be reviewed by a lawyer before publishing.

- PR reports also mark findings the change made worse ("kötüleşti: karmaşıklık 24 → 31"):
  same finding, higher severity or a larger measure (complexity, lines, TODOs, fan-in).

- `tokenwise server selftest`: checks a running setup from outside, the way GitHub and a
  browser reach it: public address, a signed webhook ping, the GitHub App's webhook URL
  (catches an old tunnel address), the sign-in redirect and a worker taking a job.

- Hosted worker: every claim of a job gets a lease number. A worker whose job was handed to
  another stops at the heartbeat or at the check before each outside step (push, PR,
  comment, records), and can no longer finish, fail or defer the job. Rate limits are
  counted in PostgreSQL, shared by every web process (memory fallback). `server doctor`
  warns when checks run without gVisor or rootless Docker. Schema version 4.

- The three modules feed each other. A ReleaseGuard report (CLI, local page, PR comment)
  lists the RepoDoctor findings in the changed files and marks the ones the change brought
  in; serious new ones become Agent suggestions. On the hosted dashboard a PR's suggestions
  go to the Agent with one click, and its draft PR targets the PR's own branch (not for
  forks). A merged fix PR is marked "merged", and "verified" once the next scan of the
  default branch no longer finds its finding. Database schema version 3.

- Local web page, after a full browser walkthrough: the side-by-side diff switch redraws
  existing diffs; the last chat reopens after a reload and proposals that can no longer be
  applied say so; plan threads can be skipped, marked done or reopened, and plans edited by
  chat; a full weekly budget offers "Yine de çalıştır"; pipeline errors are shown in Turkish
  with what to do next; undo after a commit points to `git revert` instead of leaving the file
  modified; unknown picked files are explained; works on phone-width screens.
- Hosted dashboard: logout buttons no longer show before sign-in; phone-width layout.
- `python -m tokenwise` works where the `tokenwise` command is not on PATH.

- Hosted: GitHub rate limits are waited out without using up a job's attempts; /api and
  /auth are rate-limited per client. Daily verified PostgreSQL backups (`backup` service).
  The dashboard shows "first scan running" while a new installation is prepared and a
  plain-language reason when a scan fails.
- ReleaseGuard's "changed function no test calls" list leaves out nested helpers and
  functions whose callers the tests reach.
- Fixed: install hints for the `web` and `server` extras lost their `[extra]` part.

- Hosted reliability: webhook deliveries are recorded (a redelivery does nothing), events
  arriving out of order cannot replace newer ones, jobs with the same key never run together,
  running jobs send heartbeats, retried jobs reuse their PR comment, branch and draft PR, and
  an approval is checked against the default branch as it is now ("stale" if a file changed).
- Hosted security: one-day sessions with membership re-read at every sign-in, logout on all
  devices, `__Host-` cookies; checks never run as root (`nobody` when the worker is root),
  open files limited, optional gVisor (`TOKENWISE_SANDBOX_RUNTIME=runsc`).
- `GET /metrics` (Prometheus) behind `TOKENWISE_METRICS_TOKEN`.
- Tokenwise runs ReleaseGuard on its own pull requests and RepoDoctor weekly
  (.github/workflows/tokenwise.yml).

- JS/TS functions get complexity and length findings (Tree-sitter), and ReleaseGuard's
  complexity signal now covers them: on 4 JS/TS repositories its AUC went from 0.50 to
  0.54-0.83. Imports of a project's own packages by name (`"zod/v4"`) are resolved through
  package.json `name`/`exports`. `fixtures/`, `__mocks__/` and `test-*.ts` count as tests.

- Measured on 10 open-source repositories (docs/measurements.md): findings 1537 -> 394 after
  the fixes below, about 10% false alarms in a hand-checked sample, risk score mean AUC 0.68.
- Quieter RepoDoctor: docs, examples, scripts and tool config are support code (no test, style
  or cycle rules); a file reached by a test through its package's `__init__.py`/`index.js` or
  one module in between counts as tested; modules without functions or classes need no tests;
  Bandit reports medium severity and confidence and up; Ruff checks only bug rules in projects
  without a Ruff config and never in tests; a problem Bandit and a built-in rule both see is
  counted once; import cycles count only imports that run at load time, and `import json` no
  longer resolves to a project's own `json/` package.
- ReleaseGuard's tests signal scores only changed files without tests. "This PR changes no
  test" is still shown but no longer scored: it pointed the wrong way on all 10 repositories.
- Fixed: a scan crashed on `require("..")` from a top-level folder.
- `tokenwise server doctor`: checks the database, secrets, the GitHub App's events and
  permissions, Docker, the sandbox image and that check copies are visible to containers.
- Internal: business logic moved from web.py and cli.py into `tokenwise/services/`.

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
- Type checking: `mypy` runs in CI and passes on the whole package. Fixing its findings
  also made a few edge cases explicit (a suggestion on a PR that is gone answers 404, an
  uninstall event without an installation id is ignored, custom long-prompt prices in
  `[tokenwise.prices]` read their sixth value). `cli.py` is split into cli.py (Agent),
  cli_bench.py, cli_health.py and cli_server.py; commands and options are unchanged.
