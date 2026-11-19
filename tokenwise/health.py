"""RepoDoctor: scans the project, keeps every finding with a lasting identity and history, and
scores health per category. State lives in `.tokenwise/health.db` (SQLite), one per project.

A finding is open while scans keep seeing it, resolved when a scan no longer does, reopened if
it comes back, and ignored only when a person says so."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .analyzers import CATEGORIES, Finding, analyze
from .config import Settings
from .graph import Graph, build_graph
from .history import History, head_commit, load_history

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    repository TEXT NOT NULL,
    commit_sha TEXT NOT NULL DEFAULT '',
    files INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    rule TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    first_scan INTEGER NOT NULL,
    last_scan INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    reopened INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS findings_status ON findings (status, category);
CREATE TABLE IF NOT EXISTS snapshots (
    scan_id INTEGER PRIMARY KEY REFERENCES scans(id),
    ts TEXT NOT NULL,
    overall INTEGER NOT NULL,
    scores TEXT NOT NULL,
    open_findings INTEGER NOT NULL,
    new_findings INTEGER NOT NULL,
    resolved_findings INTEGER NOT NULL
);
"""

SEVERITY_WEIGHT = {"high": 10, "medium": 4, "low": 1}
CATEGORY_WEIGHT = {
    "quality": 0.25,
    "testing": 0.25,
    "security": 0.25,
    "architecture": 0.15,
    "maintainability": 0.10,
}
STATUSES = ("open", "resolved", "ignored")


def source_of(rule: str) -> str:
    """Which analyzer produces a rule: "ruff:E501" -> "ruff", "low-coverage" -> "coverage"."""
    if ":" in rule:
        return rule.split(":", 1)[0]
    return "coverage" if rule == "low-coverage" else "builtin"


def health_path(settings: Settings) -> Path:
    return settings.state_dir / "health.db"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def scores_for(findings: list[dict], file_count: int) -> dict[str, int]:
    """100 per category, lowered by the weight of its open findings relative to project size:
    score = 100 * exp(-penalty / size), size = 2 per source file, at least 20."""
    size = max(20, 2 * file_count)
    penalty = dict.fromkeys(CATEGORIES, 0)
    for f in findings:
        penalty[f["category"]] += SEVERITY_WEIGHT[f["severity"]]
    scores = {c: round(100 * math.exp(-penalty[c] / size)) for c in CATEGORIES}
    scores["overall"] = round(sum(scores[c] * CATEGORY_WEIGHT[c] for c in CATEGORIES))
    return scores


@dataclass
class ScanResult:
    scan_id: int
    scores: dict[str, int]
    open: int
    new: int
    resolved: int
    reopened: int
    files: int
    seconds: float
    tools: list[str] = field(default_factory=list)  # external tools that ran
    skipped_tools: dict[str, str] = field(default_factory=dict)


class HealthStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> HealthStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def record(
        self,
        repository: str,
        commit: str,
        findings: list[Finding],
        file_count: int,
        seconds: float,
        ts: str | None = None,
        sources: set[str] | None = None,
    ) -> ScanResult:
        """`sources`: which analyzers ran ("builtin", "ruff", ...); None = all of them."""
        ts = ts or now()
        cur = self.db.execute(
            "INSERT INTO scans (ts, repository, commit_sha, files, seconds) VALUES (?, ?, ?, ?, ?)",
            (ts, repository, commit, file_count, seconds),
        )
        scan_id = cur.lastrowid
        seen: set[str] = set()
        new = reopened = 0
        for f in findings:
            fp = f.fingerprint
            if fp in seen:
                continue
            seen.add(fp)
            row = self.db.execute(
                "SELECT id, status FROM findings WHERE fingerprint = ?", (fp,)
            ).fetchone()
            values = (f.severity, f.line, f.title, f.detail, json.dumps(f.data), scan_id, ts)
            if row is None:
                new += 1
                self.db.execute(
                    "INSERT INTO findings (fingerprint, rule, category, severity, file, line,"
                    " title, detail, data, first_scan, last_scan, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (fp, f.rule, f.category, f.severity, f.file, f.line, f.title, f.detail,
                     json.dumps(f.data), scan_id, scan_id, ts, ts),
                )  # fmt: skip
                continue
            status = row["status"]
            if status == "resolved":
                reopened += 1
                status = "open"
            self.db.execute(
                "UPDATE findings SET severity = ?, line = ?, title = ?, detail = ?, data = ?,"
                " last_scan = ?, last_seen = ?, status = ?, resolved_at = NULL,"
                " reopened = reopened + ? WHERE id = ?",
                (*values, status, int(row["status"] == "resolved"), row["id"]),
            )
        resolved = 0
        stale = self.db.execute(
            "SELECT id, rule FROM findings WHERE status = 'open' AND last_scan < ?", (scan_id,)
        ).fetchall()
        for row in stale:  # a tool that did not run this time cannot say its findings are gone
            if sources is None or source_of(row["rule"]) in sources:
                self.db.execute(
                    "UPDATE findings SET status = 'resolved', resolved_at = ? WHERE id = ?",
                    (ts, row["id"]),
                )
                resolved += 1
        open_rows = self.open_findings()
        scores = scores_for(open_rows, file_count)
        self.db.execute(
            "INSERT INTO snapshots (scan_id, ts, overall, scores, open_findings, new_findings,"
            " resolved_findings) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scan_id, ts, scores["overall"], json.dumps(scores), len(open_rows), new, resolved),
        )
        self.db.commit()
        return ScanResult(
            scan_id, scores, len(open_rows), new, resolved, reopened, file_count, seconds
        )

    def open_findings(self, file: str | None = None) -> list[dict]:
        sql = "SELECT * FROM findings WHERE status = 'open'"
        args: tuple = ()
        if file is not None:
            sql += " AND file = ?"
            args = (file,)
        return [_row(r) for r in self.db.execute(sql + " ORDER BY id", args)]

    def findings(self, status: str | None = "open") -> list[dict]:
        if status is None:
            rows = self.db.execute("SELECT * FROM findings ORDER BY id")
        else:
            rows = self.db.execute("SELECT * FROM findings WHERE status = ? ORDER BY id", (status,))
        return [_row(r) for r in rows]

    def get(self, finding_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        return _row(row) if row else None

    def set_status(self, finding_id: int, status: str) -> dict | None:
        if status not in ("open", "ignored"):
            raise ValueError("status must be open or ignored")
        self.db.execute("UPDATE findings SET status = ? WHERE id = ?", (status, finding_id))
        self.db.commit()
        return self.get(finding_id)

    def rule_stats(self) -> list[dict]:
        """Per rule: how its findings ended. A finding a person ignored counts as a false alarm,
        one that went away from the code as a true one; open findings are not judged yet."""
        rows = self.db.execute(
            "SELECT rule, category, COUNT(*) AS total,"
            " SUM(status = 'open') AS open, SUM(status = 'resolved') AS resolved,"
            " SUM(status = 'ignored') AS ignored, SUM(reopened) AS reopened"
            " FROM findings GROUP BY rule, category ORDER BY total DESC, rule"
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            judged = item["resolved"] + item["ignored"]
            item["false_alarm_rate"] = round(item["ignored"] / judged, 3) if judged else None
            result.append(item)
        return result

    def snapshots(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute(
            "SELECT s.*, c.commit_sha FROM snapshots s JOIN scans c ON c.id = s.scan_id"
            " ORDER BY s.scan_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in reversed(rows):
            item = dict(r)
            item["scores"] = json.loads(item["scores"])
            result.append(item)
        return result

    def changed_since(self, days: int) -> dict[str, list[dict]]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        new = self.db.execute(
            "SELECT * FROM findings WHERE first_seen >= ? AND status = 'open' ORDER BY id", (since,)
        ).fetchall()
        resolved = self.db.execute(
            "SELECT * FROM findings WHERE resolved_at >= ? AND status = 'resolved' ORDER BY id",
            (since,),
        ).fetchall()
        return {"new": [_row(r) for r in new], "resolved": [_row(r) for r in resolved]}


def _row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["data"] = json.loads(item.get("data") or "{}")
    return item


def priority(finding: dict, history: History) -> float:
    """Severity weighted by how often the file changes and breaks: a hotspot first."""
    file = finding["file"]
    return SEVERITY_WEIGHT[finding["severity"]] * (
        1 + math.log1p(history.churn[file]) + 2 * math.log1p(history.fixes[file])
    )


def ranked(findings: list[dict], history: History) -> list[dict]:
    for f in findings:
        f["priority"] = round(priority(f, history), 2)
        f["churn"] = history.churn[f["file"]]
        f["fixes"] = history.fixes[f["file"]]
    return sorted(findings, key=lambda f: (-f["priority"], f["id"]))


def collect(settings: Settings, graph: Graph, tools: tuple[str, ...] | None = None):
    """Built-in findings plus those of the configured external tools. A coverage report, when
    there is one, is better evidence than imports: a file it covers well is not "missing tests"."""
    from .external import LOW_COVERAGE, drop_twins, run_tools

    found = analyze(settings, graph)
    run = run_tools(settings, graph.files, settings.scan_tools if tools is None else tools)
    found = drop_twins(found, run.findings)
    if run.coverage:
        found = [
            f for f in found
            if not (f.rule == "missing-tests" and run.coverage.get(f.file, 0) >= LOW_COVERAGE)
        ]  # fmt: skip
    return found + run.findings, run


def scan(
    settings: Settings, graph: Graph | None = None, tools: tuple[str, ...] | None = None
) -> ScanResult:
    """Run every analyzer and record the result as one scan."""
    started = time.monotonic()
    graph = graph or build_graph(settings)
    found, run = collect(settings, graph, tools)
    with HealthStore(health_path(settings)) as store:
        result = store.record(
            settings.root.resolve().name,
            head_commit(settings.root),
            found,
            len(graph.files),
            round(time.monotonic() - started, 2),
            sources={"builtin", *run.ran},
        )
    result.tools, result.skipped_tools = run.ran, run.skipped
    return result


def weekly_report(settings: Settings, days: int = 7, top: int = 5) -> str:
    """Markdown: score and trend, top priorities, what appeared and what was fixed."""
    history = load_history(settings.root)
    with HealthStore(health_path(settings)) as store:
        snaps = store.snapshots(100)
        if not snaps:
            return "Henüz tarama yok. Önce `tokenwise scan` çalıştırın.\n"
        latest = snaps[-1]
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        older = [s for s in snaps if s["ts"] < since]
        before = older[-1] if older else snaps[0]
        open_rows = ranked(store.open_findings(), history)
        changes = store.changed_since(days)

    lines = [f"# Haftalık sağlık raporu — {settings.root.resolve().name}", ""]
    delta = latest["overall"] - before["overall"]
    sign = f"{delta:+d}" if before is not latest else "ilk tarama"
    lines.append(f"**Genel skor: {latest['overall']}/100** ({sign}, son {days} gün)")
    lines += ["", "| Kategori | Skor | Değişim |", "| --- | ---: | ---: |"]
    for c in CATEGORIES:
        d = latest["scores"][c] - before["scores"][c]
        lines.append(f"| {c} | {latest['scores'][c]} | {d:+d} |")
    lines += [
        "",
        f"Açık bulgu: {latest['open_findings']} · yeni: {len(changes['new'])}"
        f" · çözülen: {len(changes['resolved'])}",
        "",
        "## Öncelikli işler",
        "",
    ]
    if not open_rows:
        lines.append("Açık bulgu yok.")
    for f in open_rows[:top]:
        why = []
        if f["churn"]:
            why.append(f"{f['churn']} commit'te değişti")
        if f["fixes"]:
            why.append(f"{f['fixes']} hata düzeltmesi")
        reason = f" — {', '.join(why)}" if why else ""
        lines.append(
            f"- [ ] **#{f['id']}** `{f['file']}` {f['title']} ({f['severity']}){reason}."
            f" Düzelt: `tokenwise fix {f['id']}`"
        )
    for title, key in (("Yeni bulgular", "new"), ("Çözülenler", "resolved")):
        rows = changes[key]
        if rows:
            lines += ["", f"## {title}", ""]
            lines += [f"- #{f['id']} `{f['file']}` {f['title']}" for f in rows[:20]]
            if len(rows) > 20:
                lines.append(f"- … ve {len(rows) - 20} tane daha")
    lines += [
        "",
        "_Skorlar deterministik analizlerden hesaplanır; bir güvenlik sertifikası değildir._",
    ]
    return "\n".join(lines) + "\n"


_TASKS = {
    "high-complexity": "{file} içindeki {function} fonksiyonunu davranışı değiştirmeden daha küçük,"
    " okunur fonksiyonlara böl. Dışarıdan görünen imzayı koru.",
    "long-function": "{file} içindeki {function} fonksiyonunu davranışı değiştirmeden kısalt;"
    " tekrar eden kısımları yardımcı fonksiyonlara taşı.",
    "missing-tests": "{file} için testler yaz: dışarıya açık fonksiyonların normal ve hata"
    " durumlarını kapsa. Projedeki mevcut test düzenini izle; {file} dosyasını değiştirme.",
    "shell-command": "{file} satır {line} civarındaki kabukla komut çalıştırmayı güvenli hale"
    " getir: kabuk yerine argüman listesi kullan (Python'da shell=True yerine liste, Node'da"
    " exec yerine execFile). Davranışı koru.",
    "dynamic-code": "{file} satır {line} civarındaki eval/exec/new Function kullanımını güvenli"
    " bir alternatifle değiştir.",
    "unsafe-html": "{file} satır {line} civarında HTML'e hesaplanan değer yazılmasını güvenli"
    " hale getir: düz metin için textContent kullan, HTML gerekiyorsa değeri temizle.",
    "unsafe-yaml": "{file} satır {line} civarındaki yaml.load çağrısını yaml.safe_load ile"
    " değiştir.",
    "unsafe-deserialize": "{file} satır {line} civarındaki güvensiz deserializasyonu güvenli bir"
    " biçimle (ör. json) değiştir ya da girdinin güvenilir olduğunu doğrula.",
    "secret": "{file} satır {line} civarındaki gizli değeri koddan çıkar; ortam değişkeninden"
    " oku ve eksikse anlaşılır bir hata ver.",
    "large-file": "{file} dosyasını sorumluluklarına göre daha küçük modüllere ayırmak için"
    " bir plan çıkar.",
    "import-cycle": "Şu dosyalar arasındaki döngüsel içe aktarmayı kır: {detail}. Ortak parçayı"
    " ayrı bir modüle taşı.",
    "todo-density": "{file} içindeki TODO/FIXME notlarını gözden geçir; küçük olanları tamamla.",
}
PLAN_RULES = {"large-file", "import-cycle", "todo-density"}  # better as a plan than one edit


def task_for(finding: dict) -> dict:
    """What to ask the coding agent for this finding: a request, its files and the intent."""
    data = finding.get("data") or {}
    template = _TASKS.get(finding["rule"], "{file}: {title}. Bunu düzelt.")
    request = template.format(
        file=finding["file"],
        line=finding.get("line", 0),
        title=finding["title"],
        detail=finding.get("detail", ""),
        function=data.get("function", ""),
    )
    files = data.get("files") or [finding["file"]]
    intent = "plan" if finding["rule"] in PLAN_RULES else "change"
    return {"finding_id": finding["id"], "request": request, "files": files[:3], "intent": intent}


def verify_fixed(settings: Settings, finding: dict) -> bool:
    """Re-run the analyzers and say whether this finding is gone. Records a new scan."""
    scan(settings)
    with HealthStore(health_path(settings)) as store:
        current = store.get(finding["id"])
    return current is not None and current["status"] == "resolved"
