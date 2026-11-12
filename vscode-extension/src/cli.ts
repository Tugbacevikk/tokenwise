// Runs the tokenwise command line and reads its JSON output. No VS Code APIs here,
// so this file can be reasoned about (and tested) on its own.
import { execFile } from "child_process";
import * as fs from "fs";
import * as path from "path";

export interface Change {
  path: string;
  old: string | null;
  new: string;
  diff: string;
  added: number;
  removed: number;
}

export interface AskResult {
  ok: boolean;
  error: string | null;
  run_id: number;
  files: string[];
  difficulty: string;
  model: string;
  changes: Change[];
  lines: { added: number; removed: number };
  check?: { command: string; passed: boolean | null; output: string };
  usage: { input: number; output: number; cache_read: number; cache_write: number };
  cost_usd: number;
}

export interface Summary {
  runs: number;
  ok: number;
  applied: number;
  calls: number;
  cache_hit_rate: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  lines_added: number;
  lines_removed: number;
  cost_usd: number;
  avg_cost_per_run: number;
  by_model: { model: string; calls: number; cached: number; cost: number }[];
}

export interface Report {
  recent: Summary;
  all_time: Summary;
  budget?: { usd_week: number; spent_7d_usd: number };
}

export class CliError extends Error {}

/** Picks the tokenwise program: the setting, then the workspace's virtualenv, then PATH. */
export function findCommand(setting: string, root: string): string {
  if (setting.trim()) {
    return setting.trim();
  }
  const candidates = [
    path.join(root, ".venv", "Scripts", "tokenwise.exe"),
    path.join(root, ".venv", "bin", "tokenwise"),
  ];
  return candidates.find((c) => fs.existsSync(c)) ?? "tokenwise";
}

/** Runs `tokenwise <args> --json --root <root>` and returns the parsed JSON. */
export function runJson<T>(
  command: string,
  args: string[],
  root: string,
  log: (line: string) => void,
): Promise<T> {
  const fullArgs = [...args, "--json", "--root", root];
  log(`$ ${command} ${fullArgs.map((a) => JSON.stringify(a)).join(" ")}`);
  return new Promise((resolve, reject) => {
    execFile(
      command,
      fullArgs,
      {
        cwd: root,
        maxBuffer: 64 * 1024 * 1024,
        env: { ...process.env, PYTHONIOENCODING: "utf-8", NO_COLOR: "1" },
        windowsHide: true,
      },
      (error, stdout, stderr) => {
        if (stderr.trim()) {
          log(stderr.trim());
        }
        const text = stdout.trim();
        if (text) {
          log(text.length > 2000 ? `${text.slice(0, 2000)}…` : text);
        }
        const spawnError = error as NodeJS.ErrnoException | null;
        if (spawnError?.code === "ENOENT") {
          reject(
            new CliError(
              `Could not find "${command}". Install tokenwise or set "tokenwise.command" in settings.`,
            ),
          );
          return;
        }
        const lastLine = text.split(/\r?\n/).pop() ?? "";
        try {
          resolve(JSON.parse(lastLine) as T);
        } catch {
          reject(new CliError(stderr.trim().split(/\r?\n/).pop() || "tokenwise gave no JSON output"));
        }
      },
    );
  });
}

export function formatUsd(value: number): string {
  return value < 1 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}
