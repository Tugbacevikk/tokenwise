"""Re-runs the RepoDoctor and ReleaseGuard measurements on the pinned repositories.

    python benchmark/reproduce/run.py                 # all 12 repositories
    python benchmark/reproduce/run.py --only flask    # one
    python benchmark/reproduce/run.py --check         # also compare with expected.json

Each repository is cloned (full history: calibration reads it) into --workdir and checked out
at the commit in repos.toml. Then `tokenwise scan --tools ruff,bandit` and `tokenwise
calibrate` run in it, exactly as a user would run them. Raw outputs, a summary table
(summary.md, summary.json) and every finding (findings.jsonl, for benchmark/labeling) are
written to --out. With --check the run fails when a number moves past the tolerance below.

Nothing here calls a model or needs an API key."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOLERANCE = {"findings": 0.05, "auc": 0.02}  # relative for counts, absolute for AUC
SIGNALS = ("impact", "history", "tests", "criticality", "complexity", "size", "score")


def sh(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed in {cwd}:\n{done.stderr[-2000:]}")
    return done.stdout


def checkout(repo: dict, workdir: Path) -> Path:
    path = workdir / repo["name"]
    if not (path / ".git").exists():
        sh("git", "clone", "--quiet", repo["url"], str(path))
    sh("git", "checkout", "--quiet", "--force", repo["commit"], cwd=path)
    sh("git", "clean", "-qfdx", cwd=path)  # also drops an earlier run's .tokenwise
    return path


def tokenwise(path: Path, *args: str) -> dict | list:
    out = sh(sys.executable, "-m", "tokenwise", *args, "--json", cwd=path)
    return json.loads(out)


def measure(repo: dict, workdir: Path, out: Path) -> dict:
    path = checkout(repo, workdir)
    scan = tokenwise(path, "scan", "--tools", "ruff,bandit")
    found = tokenwise(path, "findings", "--status", "open", "--limit", "100000")
    calibration = tokenwise(path, "calibrate", "--samples")
    for kind, data in (("scan", scan), ("findings", found), ("calibrate", calibration)):
        (out / f"{repo['name']}.{kind}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), "utf-8"
        )
    by_rule: dict[str, int] = {}
    for f in found:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    return {
        "name": repo["name"],
        "commit": repo["commit"][:12],
        "files": scan["files"],
        "findings": scan["open"],
        "by_rule": dict(sorted(by_rule.items())),
        "positives": calibration["positives"],
        "sample": calibration["sample_count"],
        "auc": calibration["auc"],
        "found": found,
    }


def versions() -> dict:
    def version(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
        except OSError:
            return "missing"

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tokenwise": version(sys.executable, "-m", "tokenwise", "--version"),
        "ruff": version("ruff", "--version"),
        "bandit": version("bandit", "--version").splitlines()[0]
        if shutil.which("bandit")
        else "missing",  # fmt: skip
        "git": version("git", "--version"),
        "tree-sitter": _package("tree-sitter"),
    }


def _package(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return "missing"


def markdown(rows: list[dict], env: dict) -> str:
    lines = [
        "# Tekrar üretilen ölçüm",
        "",
        " · ".join(f"{k}: `{v}`" for k, v in env.items()),
        "",
        "| Depo | Commit | Kaynak dosya | Bulgu | Sorunlu / örnek | " + " | ".join(SIGNALS) + " |",
        "| --- | --- | ---: | ---: | ---: |" + " ---: |" * len(SIGNALS),
    ]
    for r in rows:
        aucs = " | ".join(f"{r['auc'].get(s, 0):.2f}" for s in SIGNALS)
        lines.append(f"| {r['name']} | {r['commit'][:8]} | {r['files']} | {r['findings']} | "
                     f"{r['positives']} / {r['sample']} | {aucs} |")  # fmt: skip
    mean = sum(r["auc"]["score"] for r in rows) / len(rows)
    lines += ["", f"Risk skoru ortalama AUC: **{mean:.3f}** ({len(rows)} depo)", ""]
    return "\n".join(lines)


def compare(rows: list[dict], expected: dict) -> list[str]:
    """Differences past the tolerance, in words."""
    problems = []
    for r in rows:
        want = expected.get(r["name"])
        if want is None:
            problems.append(f"{r['name']}: expected.json has no numbers for it")
            continue
        if abs(r["findings"] - want["findings"]) > max(1, TOLERANCE["findings"] * want["findings"]):
            problems.append(f"{r['name']}: {r['findings']} findings, expected {want['findings']}")
        for signal, value in want["auc"].items():
            if abs(r["auc"].get(signal, 0) - value) > TOLERANCE["auc"]:
                problems.append(f"{r['name']}: AUC {signal} {r['auc'].get(signal, 0):.3f}, "
                                f"expected {value:.3f}")  # fmt: skip
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workdir", type=Path, default=Path(".reproduce/repos"))
    parser.add_argument("--out", type=Path, default=Path(".reproduce/results"))
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--check", action="store_true", help="compare with expected.json")
    parser.add_argument("--write-expected", action="store_true",
                        help="store this run's numbers as expected.json")  # fmt: skip
    args = parser.parse_args()

    repos = tomllib.loads((HERE / "repos.toml").read_text("utf-8"))["repo"]
    if args.only:
        repos = [r for r in repos if r["name"] in args.only]
    args.workdir.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        rows = list(pool.map(lambda r: measure(r, args.workdir, args.out), repos))

    env = versions()
    with (args.out / "findings.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            for finding in r.pop("found"):
                f.write(json.dumps({"repo": r["name"], "commit": next(
                    x["commit"] for x in repos if x["name"] == r["name"]), **finding},
                    ensure_ascii=False) + "\n")  # fmt: skip
    (args.out / "summary.json").write_text(
        json.dumps({"env": env, "repos": rows}, ensure_ascii=False, indent=1), "utf-8"
    )
    text = markdown(rows, env)
    (args.out / "summary.md").write_text(text, "utf-8")
    print(text)

    expected_path = HERE / "expected.json"
    if args.write_expected:
        expected = json.loads(expected_path.read_text("utf-8")) if expected_path.exists() else {}
        expected.update({r["name"]: {"findings": r["findings"], "auc": r["auc"]} for r in rows})
        expected_path.write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n", "utf-8")
    if args.check:
        problems = compare(rows, json.loads(expected_path.read_text("utf-8")))
        for p in problems:
            print(f"FARK: {p}")
        if problems:
            return 1
        print("Bütün sayılar beklenen aralıkta.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
