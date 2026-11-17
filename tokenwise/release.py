"""ReleaseGuard: how risky is this change for this repository? Compares the working tree with a
base branch and scores six signals, each with the evidence behind it. The score informs a
person's merge decision; it never blocks on its own unless `--fail-above` is asked for.

The weights are a starting guess to be calibrated against real regressions, not a law."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .analyzers import python_findings
from .callgraph import FunctionImpact, describe, function_impact
from .config import Settings
from .graph import Graph, build_graph
from .history import Commit, History, git, load_history
from .repo_map import is_test_file, list_source_files
from .textio import read_source

WEIGHTS = {
    "impact": 0.25,
    "history": 0.20,
    "tests": 0.20,
    "criticality": 0.15,
    "complexity": 0.10,
    "size": 0.10,
}
LABELS = {
    "impact": "Değişiklik etkisi",
    "history": "Geçmiş hata sıklığı",
    "tests": "Test eksikliği",
    "criticality": "Bağımlılık kritikliği",
    "complexity": "Kod karmaşıklığı",
    "size": "PR büyüklüğü",
}


WEIGHTS_FILE = "risk-weights.json"


class GuardError(Exception):
    pass


def weights_path(settings: Settings) -> Path:
    return settings.state_dir / WEIGHTS_FILE


def load_weights(settings: Settings) -> dict[str, float]:
    """The project's calibrated weights (`tokenwise calibrate --save`), else the defaults."""
    path = weights_path(settings)
    try:
        saved = json.loads(path.read_text("utf-8")).get("weights", {})
    except (OSError, ValueError, AttributeError):
        return dict(WEIGHTS)
    weights = {k: float(saved[k]) for k in WEIGHTS if isinstance(saved.get(k), (int, float))}
    if set(weights) != set(WEIGHTS) or not 0.99 <= sum(weights.values()) <= 1.01:
        return dict(WEIGHTS)
    return weights


@dataclass
class ChangeInput:
    """Everything the six signals need, whether the change is a working tree or a past commit.
    `history` is what git knew before the change; `graph` and `read` show the code after it."""

    stats: dict[str, tuple[int, int]]  # path -> (added, removed) lines
    sources: set[str]
    graph: Graph
    history: History
    read: Callable[[str], str]
    commits: list[Commit] = field(default_factory=list)  # the change's own commits
    functions: list[FunctionImpact] = field(default_factory=list)


@dataclass
class Signal:
    name: str
    value: float  # 0..1
    weight: float
    confidence: str  # high | medium | low
    reasons: list[str] = field(default_factory=list)

    @property
    def points(self) -> float:
        return round(100 * self.value * self.weight, 1)


@dataclass
class RiskReport:
    base: str
    changed: list[str]
    tests_changed: list[str]
    affected: dict[str, int]
    co_changed: list[tuple[str, str, int]]
    lines_added: int
    lines_removed: int
    signals: list[Signal]
    checklist: list[str]
    suggestions: list[dict]  # tasks the agent can take: {"title", "request", "files"}
    ai_commits: int
    commits: int
    functions: list[dict] = field(default_factory=list)  # changed functions, callers, tests

    @property
    def score(self) -> int:
        return round(sum(s.points for s in self.signals))

    @property
    def level(self) -> str:
        if self.score >= 70:
            return "yüksek"
        if self.score >= 40:
            return "orta"
        return "düşük"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["score"] = self.score
        data["level"] = self.level
        for item, signal in zip(data["signals"], self.signals, strict=True):
            item["points"] = signal.points
            item["label"] = LABELS[signal.name]
        data["markdown"] = to_markdown(self)
        return data


def resolve_base(root: Path, base: str) -> str:
    """The merge base of `base` and HEAD, so only this branch's work is compared."""
    if git(root, "rev-parse", "--git-dir") is None:
        raise GuardError("Bu klasör bir git deposu değil.")
    candidates = [base] if base else ["origin/main", "main", "origin/master", "master"]
    for ref in candidates:
        if git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}"):
            merge = git(root, "merge-base", ref, "HEAD")
            return (merge or ref).strip()
    raise GuardError(f"Temel dal bulunamadı: {base or 'main/master'}. --base ile belirtin.")


def _diff_numstat(root: Path, base: str) -> dict[str, tuple[int, int]]:
    out = git(root, "diff", "--numstat", "-M", base) or ""
    stats = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if " => " in path:  # renamed: keep the new name
            path = path.split(" => ")[-1].replace("}", "").replace("{", "")
        stats[path] = (
            int(added) if added.isdigit() else 0,
            int(removed) if removed.isdigit() else 0,
        )
    untracked = git(root, "ls-files", "--others", "--exclude-standard") or ""
    for path in untracked.splitlines():
        if path and path not in stats:
            try:
                count = len(read_source(root / path, strict=False).splitlines())
            except (OSError, ValueError):
                count = 0
            stats[path] = (count, 0)
    return stats


def assess(settings: Settings, base: str = "") -> RiskReport:
    root = settings.root
    merge_base = resolve_base(root, base)
    stats = _diff_numstat(root, merge_base)
    sources = set(list_source_files(settings))
    graph = build_graph(settings)

    def read(path: str) -> str:
        return read_source(root / path, strict=False)

    changed = sorted(p for p in stats if p in sources)
    change = ChangeInput(
        stats=stats,
        sources=sources,
        graph=graph,
        history=load_history(root, revision=merge_base),
        read=read,
        commits=load_history(root, revision=f"{merge_base}..HEAD").commits,
        functions=function_impact(root, graph, merge_base, changed, read),
    )
    return score_change(change, merge_base[:12], load_weights(settings), settings.check)


def score_change(
    change: ChangeInput, base: str, weights: dict[str, float], check: str = ""
) -> RiskReport:
    stats, sources, graph, history = change.stats, change.sources, change.graph, change.history
    changed = sorted(p for p in stats if p in sources)
    tests_changed = [p for p in changed if is_test_file(p)]
    code_changed = [p for p in changed if not is_test_file(p)]
    added = sum(a for a, _ in stats.values())
    removed = sum(r for _, r in stats.values())

    # tests that import the change are coverage, not impact
    affected = {f: d for f, d in graph.affected(code_changed).items() if not is_test_file(f)}
    co_changed = [c for c in history.co_changed(changed, min_count=3) if c[0] in sources][:8]

    signals: list[Signal] = []
    checklist: list[str] = []
    suggestions: list[dict] = []

    # 1. impact: how much of the project imports what changed
    direct = sorted(f for f, d in affected.items() if d == 1)
    indirect = sorted(f for f, d in affected.items() if d > 1)
    reasons = []
    if direct:
        reasons.append(f"{len(direct)} dosya doğrudan etkileniyor: {_few(direct)}")
    if indirect:
        reasons.append(f"{len(indirect)} dosya dolaylı etkileniyor: {_few(indirect)}")
    reasons += describe(change.functions)
    if co_changed:
        reasons.append(
            "Geçmişte birlikte değişen ama bu PR'da olmayan: "
            + ", ".join(f"{o} ({n} kez, {m} ile)" for o, m, n in co_changed[:3])
        )
        for other, mine, count in co_changed[:3]:
            checklist.append(f"`{other}` dosyasını kontrol et: {count} kez `{mine}` ile değişti")
    impact = min(1.0, (len(direct) + 0.5 * len(indirect) + len(co_changed)) / 10)
    signals.append(Signal("impact", impact, weights["impact"], "high", reasons or ["Etki yok"]))

    # 2. history: were these files fixed often?
    fixes = {p: history.fixes[p] for p in code_changed if history.fixes[p]}
    churn = {p: history.churn[p] for p in code_changed if history.churn[p]}
    worst = max(fixes.values(), default=0)
    reasons = [f"`{p}`: {n} hata düzeltmesi, {churn.get(p, 0)} commit" for p, n in
               sorted(fixes.items(), key=lambda x: -x[1])[:4]]  # fmt: skip
    known = len(history.commits)
    confidence = "high" if known >= 50 else "low" if known < 10 else "medium"
    if confidence != "high":
        reasons.append(f"Geçmiş kısa ({known} commit); bu sinyal zayıf")
    signals.append(
        Signal("history", min(1.0, worst / 5), weights["history"], confidence,
               reasons or ["Değişen dosyalarda geçmiş hata düzeltmesi yok"])
    )  # fmt: skip

    # 3. tests: changed code that no test covers
    untested = [p for p in code_changed if not graph.tested(p)]
    reasons = []
    if untested:
        reasons.append(f"Testi olmayan değişen dosya: {_few(untested)}")
    if code_changed and not tests_changed:
        # Shown, but not scored: on 10 real repositories, commits that changed no test needed a
        # later fix less often than those that did (AUC 0.36-0.45), because touching tests
        # marks a change of behaviour. See docs/measurements.md.
        reasons.append("Bu PR hiçbir test dosyasını değiştirmiyor")
    test_value = len(untested) / len(code_changed) if code_changed else 0.0
    signals.append(Signal("tests", min(1.0, test_value), weights["tests"], "medium",
                          reasons or ["Değişen kod testlerle kapsanıyor"]))  # fmt: skip
    no_test = [f for f in change.functions if not f.tests and f.callers]
    if no_test:
        reasons.append(
            "Kullanılan ama hiçbir testin çağırmadığı değişen fonksiyon: "
            + ", ".join(f"`{f.function}`" for f in no_test[:4])
        )
    called = [f for f in change.functions if f.tests]
    if called:
        tests = sorted({t.split(":", 1)[0] for f in called for t in f.tests})
        checklist.append(f"Değişen fonksiyonları çağıran testleri çalıştır: {_few(tests)}")
    for path in untested[:5]:
        checklist.append(f"`{path}` için test ekle")
        suggestions.append(
            {
                "title": f"{path} için test yaz",
                "request": f"{path} dosyasındaki değişen davranış için testler yaz. "
                "Mevcut test düzenini izle; kodu değiştirme.",
                "files": [path],
            }
        )

    # 4. criticality: how many files depend on what changed
    fan = {p: graph.fan_in(p) for p in code_changed if graph.fan_in(p)}
    top_fan = max(fan.values(), default=0)
    reasons = [f"`{p}` dosyasını {n} dosya içe aktarıyor" for p, n in
               sorted(fan.items(), key=lambda x: -x[1])[:3]]  # fmt: skip
    signals.append(Signal("criticality", min(1.0, top_fan / 8), weights["criticality"], "high",
                          reasons or ["Değişen dosyalara bağımlı dosya yok"]))  # fmt: skip

    # 5. complexity of the changed Python files as they are now
    complex_hits = []
    for path in code_changed:
        if not path.endswith(".py"):
            continue
        try:
            source = change.read(path)
        except (OSError, ValueError, KeyError):
            continue
        complex_hits += [f for f in python_findings(path, source) if f.rule == "high-complexity"]
    reasons = [f"`{f.file}` {f.title}" for f in complex_hits[:4]]
    reasons = reasons or ["Değişen dosyalarda yüksek karmaşıklık yok"]
    value = min(1.0, len(complex_hits) / 4)
    signals.append(Signal("complexity", value, weights["complexity"], "high", reasons))
    for f in complex_hits[:2]:
        suggestions.append(
            {
                "title": f"{f.data.get('function', f.file)} fonksiyonunu sadeleştir",
                "request": f"{f.file} içindeki {f.data.get('function')} fonksiyonunu davranışı "
                "değiştirmeden daha küçük, okunur parçalara böl.",
                "files": [f.file],
            }
        )

    # 6. size
    size = added + removed
    signals.append(Signal("size", min(1.0, size / 600), weights["size"], "high",
                          [f"{len(stats)} dosya, +{added} −{removed} satır"]))  # fmt: skip

    if check:
        checklist.append(f"Proje kontrolünü çalıştır: `{check}`")
    if direct:
        checklist.append(f"Etkilenen modülleri gözden geçir: {_few(direct)}")
    ai_commits = sum(c.ai_assisted for c in change.commits)
    if ai_commits:
        checklist.append(
            f"{ai_commits} commit AI yardımıyla yazılmış: testleri ve kenar durumları elle doğrula"
        )

    return RiskReport(
        base=base,
        changed=changed,
        tests_changed=tests_changed,
        affected=affected,
        co_changed=co_changed,
        lines_added=added,
        lines_removed=removed,
        signals=signals,
        checklist=checklist,
        suggestions=suggestions,
        ai_commits=ai_commits,
        commits=len(change.commits),
        functions=[f.to_dict() for f in change.functions],
    )


def _few(items: list[str], n: int = 4) -> str:
    shown = ", ".join(f"`{i}`" for i in items[:n])
    return shown + (f" ve {len(items) - n} tane daha" if len(items) > n else "")


def to_markdown(report: RiskReport) -> str:
    lines = [
        "## ReleaseGuard risk raporu",
        "",
        f"**Risk: {report.score}/100 ({report.level})** · temel `{report.base}` · "
        f"{len(report.changed)} kaynak dosya · +{report.lines_added} −{report.lines_removed}"
        f" · {report.commits} commit",
        "",
    ]
    if not report.changed:
        lines.append("Temel dala göre değişen kaynak dosya yok.")
        return "\n".join(lines) + "\n"
    lines += ["| Sinyal | Puan | Ağırlık | Güven | Kanıt |", "| --- | ---: | ---: | --- | --- |"]
    for s in report.signals:
        evidence = "<br>".join(r.replace("|", "\\|") for r in s.reasons)
        weight = f"%{round(s.weight * 100)}"
        lines.append(
            f"| {LABELS[s.name]} | {s.points:g} | {weight} | {s.confidence} | {evidence} |"
        )
    if report.checklist:
        lines += ["", "### Yayın öncesi kontrol listesi", ""]
        lines += [f"- [ ] {item}" for item in report.checklist]
    if report.suggestions:
        lines += ["", "### Agent'ın üstlenebileceği işler", ""]
        lines += [f'- {s["title"]}: `tokenwise ask "{s["request"]}"`' for s in report.suggestions]
    lines += [
        "",
        "_Skor, kanıtları özetleyen bir öneridir; merge kararını vermez. "
        "Ağırlıklar deneysel başlangıç değerleridir._",
    ]
    return "\n".join(lines) + "\n"
