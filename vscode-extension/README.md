# tokenwise for VS Code

Ask for a code change in plain words; tokenwise sends the model only the files it needs, gets back only the lines that change, shows them in VS Code's diff view and tracks what every request cost.

## Requirements

The `tokenwise` command line tool (Python 3.11+):

```
pipx install tokenwise-coder
```

or from source: `pip install -e .` in the [tokenwise repo](https://github.com/Tugbacevikk/tokenwise). Then run `tokenwise init` (Claude API) or `tokenwise init --local` (Ollama) in your project once.

## Use

- **Tokenwise: Ask for a change** (command palette, status bar button or the ✨ in the Tokenwise panel): type the request, review the diff, choose **Apply** or **Discard**.
- **Tokenwise: Undo last change** restores the files.
- The **Tokenwise** panel in the Explorer shows cost for the last 7 days and all time, cache hit rate, tokens, lines changed and cost per model. The status bar shows the recent cost.

## Settings

| Setting | Meaning |
| --- | --- |
| `tokenwise.command` | Path to the `tokenwise` program. Empty = the workspace's `.venv`, then PATH. On Windows, e.g. `C:\Users\me\tokenwise\.venv\Scripts\tokenwise.exe`. |
| `tokenwise.model` | Force one model for every request. Empty = tokenwise picks by difficulty. |
| `tokenwise.reportDays` | Length of the recent window in the panel. |

Problems? **Tokenwise: Show log** shows every command run and its output.
