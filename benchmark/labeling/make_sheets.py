"""Builds the sheets independent reviewers fill in (CSV, opens in Excel / Google Sheets).

    python benchmark/labeling/make_sheets.py .reproduce/results

Two sheets, both drawn with a fixed seed so every reviewer gets the same rows:

- findings.csv: RepoDoctor findings, up to PER_RULE per rule. Each row links to the exact
  line on GitHub at the pinned commit. The reviewer decides: is the finding true?
- regressions.csv: commits the calibration labelled "sorunlu" (a fix touched the same code
  within 20 commits). Each row links to the commit and to that fix. The reviewer decides:
  did the fix really repair something this commit broke? This measures how often the
  label behind the AUC numbers is right (its precision); missed regressions are not checked.

Reviewers must not see each other's answers or Claude's; score.py compares them afterwards."""

from __future__ import annotations

import csv
import json
import random
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261119
PER_RULE = 12  # findings per rule (all of them when a rule has fewer)
REGRESSIONS = 30  # commits labelled "sorunlu"


def repos() -> dict[str, dict]:
    data = tomllib.loads((HERE.parent / "reproduce" / "repos.toml").read_text("utf-8"))
    return {r["name"]: r for r in data["repo"]}


def finding_rows(results: Path, rng: random.Random) -> list[dict]:
    known = repos()
    found = [json.loads(line) for line in (results / "findings.jsonl").open(encoding="utf-8")]
    by_rule: dict[str, list[dict]] = {}
    for f in found:
        by_rule.setdefault(f["rule"], []).append(f)
    rows = []
    for rule in sorted(by_rule):
        group = sorted(by_rule[rule], key=lambda f: (f["repo"], f["file"], f["line"] or 0))
        for f in rng.sample(group, min(PER_RULE, len(group))):
            url = known[f["repo"]]["url"]
            line = f"#L{f['line']}" if f["line"] else ""
            rows.append({
                "satır": 0, "depo": f["repo"], "kural": rule, "iddia": f["title"],
                "ayrıntı": f["detail"], "bağlantı": f"{url}/blob/{f['commit']}/{f['file']}{line}",
                "karar": "", "not": "", "_fingerprint": f["fingerprint"], "_rule": rule,
            })  # fmt: skip
    rng.shuffle(rows)  # rules mixed, so a reviewer does not settle into one rule's rhythm
    for i, row in enumerate(rows, 1):
        row["satır"] = i
    return rows


def regression_rows(results: Path, rng: random.Random) -> list[dict]:
    known = repos()
    pos = []
    for name in known:
        path = results / f"{name}.calibrate.json"
        if path.exists():
            pos += [{**s, "repo": name} for s in json.loads(path.read_text("utf-8"))
                    .get("samples", []) if s["regression"]]  # fmt: skip
    chosen = rng.sample(pos, min(REGRESSIONS, len(pos)))
    rows = []
    for i, s in enumerate(chosen, 1):
        url = known[s["repo"]]["url"]
        rows.append({
            "satır": i, "depo": s["repo"], "commit": f"{url}/commit/{s['sha']}",
            "konu": s["subject"],
            "düzeltme": f"{url}/commit/{s['fixed_by']}",
            "karar": "", "not": "", "_label": "sorunlu",
        })  # fmt: skip
    return rows


def write(path: Path, rows: list[dict]) -> None:
    public = [k for k in rows[0] if not k.startswith("_")]
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel reads UTF-8
        writer = csv.DictWriter(f, fieldnames=public, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    key = path.with_suffix(".key.json")  # each row's rule, for score.py; not for reviewers
    key.write_text(json.dumps({r["satır"]: {k: v for k, v in r.items() if k.startswith("_")}
                               for r in rows}, indent=1), "utf-8")  # fmt: skip


def main() -> int:
    results = Path(sys.argv[1] if len(sys.argv) > 1 else ".reproduce/results")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else ".reproduce/sheets")
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    findings, regressions = finding_rows(results, rng), regression_rows(results, rng)
    write(out / "findings.csv", findings)
    if regressions:
        write(out / "regressions.csv", regressions)
    print(f"{out}/findings.csv: {len(findings)} bulgu · {out}/regressions.csv: "
          f"{len(regressions)} commit")  # fmt: skip
    print("Gözden geçirenlere yalnızca .csv dosyalarını gönder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
