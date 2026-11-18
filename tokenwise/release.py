"""ReleaseGuard: how risky is this change for this repository? Compares the working tree with a
base branch and scores six signals, each with the evidence behind it. The score informs a
person's merge decision; it never blocks on its own unless `--fail-above` is asked for.

The weights are a starting guess to be calibrated against real regressions, not a law."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .analyzers import SEVERITIES, analyze, code_findings, text_findings
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
    # RepoDoctor findings in the changed files; "new" ones this change brought in
    findings: list[dict] = field(default_factory=list)

    @property
    def new_findings(self) -> list[dict]:
        return [f for f in self.findings if f["new"]]

    @property
    def worse_findings(self) -> list[dict]:
        """Findings that were already there and this change made worse."""
        return [f for f in self.findings if f.get("worse")]

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
    raise GuardError(
        f"Temel dal bulunamadı: {base or 'main/master'}. "
        "Temel dalın adını yaz (komut satırında: --base)."
    )


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
    report = score_change(change, merge_base[:12], load_weights(settings), settings.check)
    attach_findings(report, changed_findings(settings, graph, merge_base, changed))
    return report


# measures a finding can grow on, and how a report names them
MEASURES = {"complexity": "karmaşıklık", "lines": "satır", "count": "not", "fan_in": "bağımlı"}


def _file_findings(path: str, source: str) -> dict:
    """The findings decided by the file alone (not by the rest of the project)."""
    return {f.fingerprint: f for f in code_findings(path, source) + text_findings(path, source)}


def changed_findings(settings: Settings, graph: Graph, base: str, changed: list[str]) -> list[dict]:
    """RepoDoctor's built-in findings in the changed files. `new`: the change brought it in (a
    file-level finding the base version of the file did not have; a project-level one, such as
    missing tests or an import cycle, in a new file). `worse`: it was already there and grew
    (higher severity, or a larger measure such as complexity)."""
    wanted = set(changed)
    head = [f for f in analyze(settings, graph) if f.file in wanted] if wanted else []
    before: dict[str, dict | None] = {}  # path -> its findings at the base; None: new file
    now: dict[str, dict] = {}
    for path in {f.file for f in head}:
        old = git(settings.root, "show", f"{base}:{path}")
        before[path] = None if old is None else _file_findings(path, old)
        now[path] = _file_findings(path, read_source(settings.root / path, strict=False))
    order = {s: i for i, s in enumerate(SEVERITIES)}
    rows = [_finding_row(f, before[f.file], now[f.file]) for f in head]
    return sorted(rows, key=lambda r: (not r["new"], not r["worse"], order[r["severity"]],
                                       r["file"], r["line"]))  # fmt: skip


def _finding_row(f, before: dict | None, now: dict) -> dict:
    worse = None
    if before is None:  # a new file: everything in it is new
        new = True
    elif f.fingerprint in now:
        new = f.fingerprint not in before
        worse = None if new else _worse(before[f.fingerprint], f)
    else:  # decided by the whole project, and the file was already there
        new = False
    return {
        "rule": f.rule, "category": f.category, "severity": f.severity, "file": f.file,
        "line": f.line, "title": f.title, "detail": f.detail, "data": f.data,
        "fingerprint": f.fingerprint, "new": new, "worse": worse,
    }  # fmt: skip


def _worse(old, new) -> str | None:
    """How a finding grew, in words ("karmaşıklık 24 → 31"), or None if it did not."""
    order = {s: i for i, s in enumerate(SEVERITIES)}
    grown = [f"{label} {old.data[key]} → {new.data[key]}" for key, label in MEASURES.items()
             if isinstance(old.data.get(key), int) and isinstance(new.data.get(key), int)
             and new.data[key] > old.data[key]]  # fmt: skip
    if order[new.severity] < order[old.severity]:
        grown.insert(0, f"önem {old.severity} → {new.severity}")
    return ", ".join(grown) or None


def attach_findings(report: RiskReport, findings: list[dict]) -> None:
    """Adds the findings to the report, its checklist and the Agent's suggestions."""
    from .health import task_for

    report.findings = findings
    serious = [f for f in report.new_findings if f["severity"] in ("high", "medium")]
    if report.new_findings:
        report.checklist.append(
            f"Bu değişiklik {len(report.new_findings)} yeni RepoDoctor bulgusu getiriyor"
            + (f" ({len(serious)} orta/yüksek önemde)" if serious else "")
            + ": gözden geçir"
        )
    if report.worse_findings:
        report.checklist.append(
            f"Var olan {len(report.worse_findings)} bulguyu ağırlaştırıyor: "
            + "; ".join(
                f"{f['title'].split(' (')[0]} ({f['worse']})" for f in report.worse_findings[:3]
            )  # fmt: skip
        )
    for f in serious[:2]:
        task = task_for({**f, "id": None})
        if task["intent"] != "change":
            continue
        report.suggestions.append({
            "title": f"Yeni bulguyu düzelt: {f['title']}",
            "request": task["request"],
            "files": task["files"],
        })  # fmt: skip


def score_change(
    change: ChangeInput, base: str, weights: dict[str, float], check: str = ""
) -> RiskReport:
    scoring = _Scoring(change, weights)
    signals = [
        scoring.impact(),
        scoring.history(),
        scoring.tests(),
        scoring.criticality(),
        scoring.complexity(),
        scoring.size(),
    ]
    checklist = scoring.checklist
    if check:
        checklist.append(f"Proje kontrolünü çalıştır: `{check.replace('{python}', 'python')}`")
    if scoring.direct:
        checklist.append(f"Etkilenen modülleri gözden geçir: {_few(scoring.direct)}")
    ai_commits = sum(c.ai_assisted for c in change.commits)
    if ai_commits:
        checklist.append(
            f"{ai_commits} commit AI yardımıyla yazılmış: testleri ve kenar durumları elle doğrula"
        )
    return RiskReport(
        base=base,
        changed=scoring.changed,
        tests_changed=scoring.tests_changed,
        affected=scoring.affected,
        co_changed=scoring.co_changed,
        lines_added=scoring.added,
        lines_removed=scoring.removed,
        signals=signals,
        checklist=checklist,
        suggestions=scoring.suggestions,
        ai_commits=ai_commits,
        commits=len(change.commits),
        functions=[f.to_dict() for f in change.functions],
    )


class _Scoring:
    """The six signals of one change. Each method returns its Signal and adds what it found to
    the shared checklist and Agent suggestions."""

    def __init__(self, change: ChangeInput, weights: dict[str, float]) -> None:
        self.change, self.weights = change, weights
        stats, graph = change.stats, change.graph
        self.changed = sorted(p for p in stats if p in change.sources)
        self.tests_changed = [p for p in self.changed if is_test_file(p)]
        self.code = [p for p in self.changed if not is_test_file(p)]
        self.added = sum(a for a, _ in stats.values())
        self.removed = sum(r for _, r in stats.values())
        # tests that import the change are coverage, not impact
        self.affected = {f: d for f, d in graph.affected(self.code).items()
                         if not is_test_file(f)}  # fmt: skip
        self.direct = sorted(f for f, d in self.affected.items() if d == 1)
        self.co_changed = [c for c in change.history.co_changed(self.changed, min_count=3)
                           if c[0] in change.sources][:8]  # fmt: skip
        self.checklist: list[str] = []
        self.suggestions: list[dict] = []

    def _signal(self, name: str, value: float, confidence: str, reasons: list[str],
                empty: str) -> Signal:  # fmt: skip
        return Signal(name, min(1.0, value), self.weights[name], confidence, reasons or [empty])

    def impact(self) -> Signal:
        """How much of the project imports what changed."""
        indirect = sorted(f for f, d in self.affected.items() if d > 1)
        reasons = []
        if self.direct:
            reasons.append(f"{len(self.direct)} dosya doğrudan etkileniyor: {_few(self.direct)}")
        if indirect:
            reasons.append(f"{len(indirect)} dosya dolaylı etkileniyor: {_few(indirect)}")
        reasons += describe(self.change.functions)
        if self.co_changed:
            reasons.append(
                "Geçmişte birlikte değişen ama bu PR'da olmayan: "
                + ", ".join(f"{o} ({n} kez, {m} ile)" for o, m, n in self.co_changed[:3])
            )
            for other, mine, count in self.co_changed[:3]:
                self.checklist.append(
                    f"`{other}` dosyasını kontrol et: {count} kez `{mine}` ile değişti"
                )
        value = (len(self.direct) + 0.5 * len(indirect) + len(self.co_changed)) / 10
        return self._signal("impact", value, "high", reasons, "Etki yok")

    def history(self) -> Signal:
        """Were these files fixed often?"""
        history = self.change.history
        fixes = {p: history.fixes[p] for p in self.code if history.fixes[p]}
        reasons = [f"`{p}`: {n} hata düzeltmesi, {history.churn[p]} commit"
                   for p, n in sorted(fixes.items(), key=lambda x: -x[1])[:4]]  # fmt: skip
        known = len(history.commits)
        confidence = "high" if known >= 50 else "low" if known < 10 else "medium"
        if confidence != "high":
            reasons.append(f"Geçmiş kısa ({known} commit); bu sinyal zayıf")
        worst = max(fixes.values(), default=0)
        return self._signal("history", worst / 5, confidence, reasons,
                            "Değişen dosyalarda geçmiş hata düzeltmesi yok")  # fmt: skip

    def tests(self) -> Signal:
        """Changed code that no test covers."""
        functions = self.change.functions
        untested = [p for p in self.code if not self.change.graph.tested(p)]
        reasons = []
        if untested:
            reasons.append(f"Testi olmayan değişen dosya: {_few(untested)}")
        if self.code and not self.tests_changed:
            # Shown, but not scored: on 10 real repositories, commits that changed no test
            # needed a later fix less often than those that did (AUC 0.36-0.45), because
            # touching tests marks a change of behaviour. See docs/measurements.md.
            reasons.append("Bu PR hiçbir test dosyasını değiştirmiyor")
        graph = self.change.graph
        # Not reported: helpers nested in another function, and functions whose callers sit
        # in code the tests reach (tested through their caller).
        no_test = [
            f for f in functions
            if f.callers and not f.tests and not f.nested
            and not any(graph.tested(c.split(":", 1)[0]) for c in f.callers)
        ]  # fmt: skip
        if no_test:
            reasons.append(
                "Kullanılan ama hiçbir testin çağırmadığı değişen fonksiyon: "
                + ", ".join(f"`{f.function}`" for f in no_test[:4])
            )
        called = sorted({t.split(":", 1)[0] for f in functions for t in f.tests})
        if called:
            self.checklist.append(
                f"Değişen fonksiyonları çağıran testleri çalıştır: {_few(called)}"
            )
        for path in untested[:5]:
            self.checklist.append(f"`{path}` için test ekle")
            self.suggestions.append({
                "title": f"{path} için test yaz",
                "request": f"{path} dosyasındaki değişen davranış için testler yaz. "
                "Mevcut test düzenini izle; kodu değiştirme.",
                "files": [path],
            })  # fmt: skip
        value = len(untested) / len(self.code) if self.code else 0.0
        return self._signal("tests", value, "medium", reasons, "Değişen kod testlerle kapsanıyor")

    def criticality(self) -> Signal:
        """How many files depend on what changed."""
        graph = self.change.graph
        fan = {p: graph.fan_in(p) for p in self.code if graph.fan_in(p)}
        reasons = [f"`{p}` dosyasını {n} dosya içe aktarıyor"
                   for p, n in sorted(fan.items(), key=lambda x: -x[1])[:3]]  # fmt: skip
        return self._signal("criticality", max(fan.values(), default=0) / 8, "high", reasons,
                            "Değişen dosyalara bağımlı dosya yok")  # fmt: skip

    def complexity(self) -> Signal:
        """Complexity of the changed files as they are now (Python; JS/TS with Tree-sitter)."""
        hits = []
        for path in self.code:
            try:
                source = self.change.read(path)
            except (OSError, ValueError, KeyError):
                continue
            hits += [f for f in code_findings(path, source) if f.rule == "high-complexity"]
        for f in hits[:2]:
            self.suggestions.append({
                "title": f"{f.data.get('function', f.file)} fonksiyonunu sadeleştir",
                "request": f"{f.file} içindeki {f.data.get('function')} fonksiyonunu davranışı "
                "değiştirmeden daha küçük, okunur parçalara böl.",
                "files": [f.file],
            })  # fmt: skip
        reasons = [f"`{f.file}` {f.title}" for f in hits[:4]]
        return self._signal("complexity", len(hits) / 4, "high", reasons,
                            "Değişen dosyalarda yüksek karmaşıklık yok")  # fmt: skip

    def size(self) -> Signal:
        lines = self.added + self.removed
        reason = f"{len(self.change.stats)} dosya, +{self.added} −{self.removed} satır"
        return self._signal("size", lines / 600, "high", [reason], "")


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
    lines += _findings_markdown(report)
    if report.suggestions:
        lines += ["", "### Agent'ın üstlenebileceği işler", ""]
        lines += [f'- {s["title"]}: `tokenwise ask "{s["request"]}"`' for s in report.suggestions]
    lines += [
        "",
        "_Skor, kanıtları özetleyen bir öneridir; merge kararını vermez. "
        "Ağırlıklar deneysel başlangıç değerleridir._",
    ]
    return "\n".join(lines) + "\n"


def finding_mark(f: dict) -> str:
    """The short label a report puts before a finding: "yeni", "kötüleşti: …" or nothing."""
    if f["new"]:
        return "yeni"
    return f"kötüleşti: {f['worse']}" if f.get("worse") else ""


def _counts(report: RiskReport) -> str:
    new, worse = report.new_findings, report.worse_findings
    counts = [f"{len(new)} yeni" if new else "Yeni bulgu yok"]
    counts += [f"{len(worse)} kötüleşti"] if worse else []
    counts.append(f"{len(report.findings) - len(new) - len(worse)} önceden vardı")
    return " · ".join(counts)


def _findings_markdown(report: RiskReport) -> list[str]:
    if not report.findings:
        return []
    new, worse = report.new_findings, report.worse_findings
    lines = ["", "### RepoDoctor: değişen dosyalardaki bulgular", "", _counts(report), ""]
    pool = new + worse or report.findings
    shown = pool[: 8 if new or worse else 5]
    for f in shown:
        where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        mark = f"**{finding_mark(f)}** · " if finding_mark(f) else ""
        lines.append(f"- {mark}`{f['severity']}` {f['title']} (`{where}`)")
    if len(pool) > len(shown):
        lines.append(f"- … ve {len(pool) - len(shown)} tane daha")
    return lines
