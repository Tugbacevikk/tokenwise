import * as path from "path";
import * as vscode from "vscode";
import { AskResult, CliError, Report, findCommand, formatUsd, runJson } from "./cli";

const SCHEME = "tokenwise-proposal";

/** Serves the proposed file contents so VS Code's own diff view can show them. */
class ProposalProvider implements vscode.TextDocumentContentProvider {
  private contents = new Map<string, string>();
  private emitter = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.emitter.event;

  set(uri: vscode.Uri, text: string): void {
    this.contents.set(uri.toString(), text);
    this.emitter.fire(uri);
  }

  clear(): void {
    this.contents.clear();
  }

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.contents.get(uri.toString()) ?? "";
  }
}

class ReportItem extends vscode.TreeItem {
  constructor(
    label: string,
    description: string,
    readonly children: ReportItem[] = [],
    icon?: string,
  ) {
    super(
      label,
      children.length ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None,
    );
    this.description = description;
    if (icon) {
      this.iconPath = new vscode.ThemeIcon(icon);
    }
  }
}

/** The "Tokenwise" panel in the Explorer sidebar: what has been spent. */
class ReportProvider implements vscode.TreeDataProvider<ReportItem> {
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;
  report: Report | undefined;
  message = "Run “Tokenwise: Ask for a change” to start.";

  update(report: Report | undefined, message = ""): void {
    this.report = report;
    this.message = message;
    this.emitter.fire();
  }

  getTreeItem(item: ReportItem): vscode.TreeItem {
    return item;
  }

  getChildren(item?: ReportItem): ReportItem[] {
    if (item) {
      return item.children;
    }
    if (!this.report) {
      return [new ReportItem(this.message, "", [], "info")];
    }
    const days = vscode.workspace.getConfiguration("tokenwise").get<number>("reportDays", 7);
    const r = this.report.recent;
    const all = this.report.all_time;
    return [
      new ReportItem(`Cost, last ${days} days`, formatUsd(r.cost_usd), [], "credit-card"),
      new ReportItem("Cost, all time", formatUsd(all.cost_usd), [], "credit-card"),
      new ReportItem("Average per request", formatUsd(all.avg_cost_per_run), [], "graph"),
      new ReportItem("Requests", `${all.runs} (${all.applied} applied)`, [], "comment-discussion"),
      new ReportItem("Cache hit rate", `${Math.round(100 * all.cache_hit_rate)}%`, [], "database"),
      new ReportItem(
        "Tokens",
        `${all.input_tokens.toLocaleString()} in · ${all.output_tokens.toLocaleString()} out`,
        [],
        "symbol-number",
      ),
      new ReportItem("Lines changed", `+${all.lines_added} −${all.lines_removed}`, [], "diff"),
      new ReportItem(
        "By model",
        "",
        all.by_model.map((m) => new ReportItem(m.model, `${m.calls} calls · ${formatUsd(m.cost)}`)),
        "server",
      ),
    ];
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Tokenwise");
  const log = (line: string) => output.appendLine(line);
  const proposals = new ProposalProvider();
  const reportView = new ReportProvider();
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "tokenwise.ask";
  status.tooltip = "Tokenwise: ask for a change";
  status.text = "$(sparkle) tokenwise";
  status.show();

  context.subscriptions.push(
    output,
    status,
    vscode.workspace.registerTextDocumentContentProvider(SCHEME, proposals),
    vscode.window.registerTreeDataProvider("tokenwise.report", reportView),
  );

  function workspaceRoot(): string | undefined {
    const active = vscode.window.activeTextEditor?.document.uri;
    const folder =
      (active && vscode.workspace.getWorkspaceFolder(active)) ?? vscode.workspace.workspaceFolders?.[0];
    if (!folder) {
      vscode.window.showErrorMessage("Tokenwise: open a folder first.");
    }
    return folder?.uri.fsPath;
  }

  function command(root: string): string {
    return findCommand(vscode.workspace.getConfiguration("tokenwise").get<string>("command", ""), root);
  }

  function fail(error: unknown): void {
    const message = error instanceof CliError ? error.message : String(error);
    vscode.window.showErrorMessage(`Tokenwise: ${message}`, "Show log").then((choice) => {
      if (choice) {
        output.show();
      }
    });
  }

  async function refresh(): Promise<void> {
    const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!root) {
      return;
    }
    const days = vscode.workspace.getConfiguration("tokenwise").get<number>("reportDays", 7);
    try {
      const report = await runJson<Report>(command(root), ["report", "--days", String(days)], root, log);
      reportView.update(report);
      status.text = `$(sparkle) tokenwise ${formatUsd(report.recent.cost_usd)}`;
      status.tooltip = `Tokenwise: ${formatUsd(report.recent.cost_usd)} in the last ${days} days. Click to ask for a change.`;
    } catch (error) {
      reportView.update(undefined, error instanceof CliError ? error.message : String(error));
    }
  }

  async function showDiffs(root: string, result: AskResult): Promise<void> {
    proposals.clear();
    for (const change of result.changes) {
      const right = vscode.Uri.from({ scheme: SCHEME, path: `/${change.path}`, query: String(result.run_id) });
      proposals.set(right, change.new);
      let left: vscode.Uri;
      if (change.old === null) {
        left = vscode.Uri.from({ scheme: SCHEME, path: `/${change.path}`, query: "empty" });
        proposals.set(left, "");
      } else {
        left = vscode.Uri.file(path.join(root, change.path));
      }
      const label = change.old === null ? "new file" : `+${change.added} −${change.removed}`;
      await vscode.commands.executeCommand("vscode.diff", left, right, `${change.path} (tokenwise: ${label})`, {
        preview: result.changes.length === 1,
      });
    }
  }

  async function apply(root: string): Promise<void> {
    try {
      const done = await runJson<{ ok: boolean; applied?: string[]; error?: string }>(
        command(root),
        ["apply"],
        root,
        log,
      );
      if (!done.ok) {
        throw new CliError(done.error ?? "could not apply");
      }
      const choice = await vscode.window.showInformationMessage(
        `Tokenwise applied: ${done.applied?.join(", ")}`,
        "Undo",
      );
      if (choice === "Undo") {
        await undo(root);
      }
    } catch (error) {
      fail(error);
    } finally {
      await refresh();
    }
  }

  async function undo(root: string): Promise<void> {
    try {
      const done = await runJson<{ ok: boolean; restored: string[] }>(command(root), ["undo"], root, log);
      vscode.window.showInformationMessage(
        done.ok ? `Tokenwise restored: ${done.restored.join(", ")}` : "Tokenwise: nothing to undo.",
      );
    } catch (error) {
      fail(error);
    }
  }

  async function ask(): Promise<void> {
    const root = workspaceRoot();
    if (!root) {
      return;
    }
    const request = await vscode.window.showInputBox({
      title: "Tokenwise",
      prompt: "What should change? Plain words, e.g. “add a length check to login”.",
      ignoreFocusOut: true,
    });
    if (!request?.trim()) {
      return;
    }
    const model = vscode.workspace.getConfiguration("tokenwise").get<string>("model", "");
    const args = ["ask", request.trim(), ...(model ? ["--model", model] : [])];

    let result: AskResult;
    try {
      result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Tokenwise: writing the change…" },
        () => runJson<AskResult>(command(root), args, root, log),
      );
    } catch (error) {
      fail(error);
      return;
    }
    const checkFailed = result.check?.passed === false && result.changes?.length > 0;
    if (!result.ok && !checkFailed) {
      fail(new CliError(`no change produced: ${result.error} (cost ${formatUsd(result.cost_usd ?? 0)})`));
      await refresh();
      return;
    }

    await showDiffs(root, result);
    const summary =
      `${result.changes.length} file(s) · +${result.lines.added} −${result.lines.removed} lines · ` +
      `${formatUsd(result.cost_usd)} · ${result.model} (${result.difficulty})`;
    let choice: string | undefined;
    if (checkFailed) {
      output.appendLine(`Check failed: ${result.check?.command}\n${result.check?.output}`);
      choice = await vscode.window.showWarningMessage(
        `Tokenwise: the check failed (${result.check?.command}). ${summary}`,
        "Show output",
        "Apply anyway",
        "Discard",
      );
      if (choice === "Show output") {
        output.show();
        choice = await vscode.window.showWarningMessage("Tokenwise: apply the change anyway?", "Apply anyway", "Discard");
      }
    } else {
      const passed = result.check?.passed ? " · check passed" : "";
      choice = await vscode.window.showInformationMessage(`Tokenwise: ${summary}${passed}`, "Apply", "Discard");
    }
    if (choice === "Apply" || choice === "Apply anyway") {
      await apply(root);
    } else {
      await refresh();
    }
  }

  context.subscriptions.push(
    vscode.commands.registerCommand("tokenwise.ask", ask),
    vscode.commands.registerCommand("tokenwise.apply", async () => {
      const root = workspaceRoot();
      if (root) {
        await apply(root);
      }
    }),
    vscode.commands.registerCommand("tokenwise.undo", async () => {
      const root = workspaceRoot();
      if (root) {
        await undo(root);
        await refresh();
      }
    }),
    vscode.commands.registerCommand("tokenwise.refresh", refresh),
    vscode.commands.registerCommand("tokenwise.showLog", () => output.show()),
  );

  void refresh();
}

export function deactivate(): void {}
