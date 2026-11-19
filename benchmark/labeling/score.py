"""Scores the sheets independent reviewers filled in.

    python benchmark/labeling/score.py .reproduce/sheets ayse/findings.csv mehmet/findings.csv
    python benchmark/labeling/score.py .reproduce/sheets ayse/regressions.csv

`karar` column: d = doğru (true), y = yanlış (false alarm / wrong label), ? = emin değilim
(also accepted: doğru, yanlış, evet, hayır, e, h). Unsure rows are left out of the rates.

Reports per reviewer the false alarm rate (findings) or the label precision (regressions)
with a 95% Wilson interval, and, with two or more reviewers, how often they agree beyond
chance (Cohen's kappa). A kappa under 0.4 means the reviewers do not share a definition yet:
talk it through before trusting either number."""

from __future__ import annotations

import csv
import json
import math
import sys
from itertools import combinations
from pathlib import Path

TRUE = {"d", "doğru", "dogru", "e", "evet", "1", "true"}
FALSE = {"y", "yanlış", "yanlis", "h", "hayır", "hayir", "0", "false"}


def read(path: Path) -> dict[int, bool | None]:
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    answers = {}
    for row in rows:
        mark = (row.get("karar") or "").strip().lower()
        answers[int(row["satır"])] = True if mark in TRUE else False if mark in FALSE else None
    return answers


def wilson(hits: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    z, p = 1.96, hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def kappa(a: dict[int, bool | None], b: dict[int, bool | None]) -> tuple[float | None, int]:
    both = [(a[k], b[k]) for k in a if k in b and a[k] is not None and b[k] is not None]
    n = len(both)
    if n == 0:
        return None, 0
    agree = sum(x == y for x, y in both) / n
    pa, pb = sum(x for x, _ in both) / n, sum(y for _, y in both) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    return (1.0 if chance == 1 else (agree - chance) / (1 - chance)), n


def per_rule(key: dict, decided: dict[int, bool]) -> list[tuple[str, int, int]]:
    counts: dict[str, list[int]] = {}
    for row, info in key.items():
        if int(row) in decided:
            c = counts.setdefault(info.get("_rule", "?"), [0, 0])
            c[0] += decided[int(row)] is False
            c[1] += 1
    return [(rule, w, n) for rule, (w, n) in sorted(counts.items())]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    sheets, files = Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]]
    kind = "regressions" if "regression" in files[0].name else "findings"
    key = json.loads((sheets / f"{kind}.key.json").read_text("utf-8"))
    reviewers = {p.parent.name or p.stem: read(p) for p in files}
    print(f"{len(key)} satır · {len(reviewers)} gözden geçiren\n")
    for name, answers in reviewers.items():
        decided = {k: v for k, v in answers.items() if v is not None}
        wrong = sum(1 for v in decided.values() if v is False)
        low, high = wilson(wrong, len(decided))
        what = "yanlış alarm oranı" if kind == "findings" else "etiketin yanlış olduğu oran"
        print(f"{name}: {len(decided)} karar ({len(answers) - len(decided)} emin değil) · "
              f"{what} %{100 * wrong / max(1, len(decided)):.1f} "
              f"(%95 aralık: %{100 * low:.1f}–%{100 * high:.1f})")  # fmt: skip
        if kind == "findings":
            for rule, wrong_n, n in per_rule(key, decided):
                print(f"  {rule}: {wrong_n}/{n} yanlış alarm")
    for (n1, a), (n2, b) in combinations(reviewers.items(), 2):
        k, n = kappa(a, b)
        text = "-" if k is None else f"{k:.2f}"
        print(f"\nUyum {n1} – {n2}: Cohen's kappa {text} ({n} ortak karar)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
