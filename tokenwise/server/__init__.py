"""The hosted Tokenwise: a GitHub App that scans repositories on every push, comments a risk
report on every pull request and, when an organization owner approves, opens a draft pull
request with an Agent fix. PostgreSQL holds the state and the job queue; the worker runs project
checks only inside locked containers. Install with `pip install tokenwise-coder[server]`."""
