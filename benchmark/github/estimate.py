"""Token and cost estimate for the 20 GitHub tasks, without calling any model.

Both methods run through tokenwise's real code (map, selector prompt, router, focused views,
edit parsing, baseline prompt), but the model is replaced by the reference answers in
reference.toml: the ideal file choice and the smallest correct edit. So the numbers show what
each method sends and receives when the model gets it right on the first try. They do not show
how often a real model gets it right, how many retries it needs, or how long it writes; for
that, run `tokenwise benchmark benchmark/github/tasks.toml` with a real model.

Tokens are estimated as characters / 4 for both methods alike (no tokenizer is available
offline). Each task is also verified: its check must fail on the original code and pass after
the reference change, for both methods.

    python benchmark/github/estimate.py            # writes benchmark/github/estimate.md
"""

from __future__ import annotations

import json
import sys
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path

from tokenwise.benchmark import baseline, baseline_model, load_tasks
from tokenwise.checker import copy_project, run_command
from tokenwise.config import Settings
from tokenwise.editor import apply_changes
from tokenwise.llm import Reply, Usage
from tokenwise.pipeline import run
from tokenwise.pricing import price_for, usage_cost
from tokenwise.repo_map import estimate_tokens, list_source_files, source_tokens

HERE = Path(__file__).resolve().parent
CONTEXT_WARN = 200_000


class ScriptedModel:
    """Answers with prepared text; counts tokens the same way for every call."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> Reply:
        text = self.answers.pop(0)
        usage = Usage(estimate_tokens(system) + estimate_tokens(prompt), estimate_tokens(text))
        self.calls.append(
            {"model": model, "input": usage.input_tokens, "output": usage.output_tokens}
        )
        return Reply(text=text, model=model, usage=usage)


def cost(calls: list[dict]) -> float:
    return sum(usage_cost(Usage(c["input"], c["output"]), price_for(c["model"])) for c in calls)


def check(task, root: Path) -> bool:
    if not task.check:
        return True
    return run_command(task.check, root, 300, HERE / "checks").passed


def measure(task, ref: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="tw-est-") as tmp:
        tmp = Path(tmp)
        original = tmp / "original"
        copy_project(task.project, original)
        settings = Settings(root=original, cache=False)
        files = list_source_files(settings)
        row = {
            "task": task.name,
            "files": len(files),
            "project_tokens": source_tokens(settings, files),
            "fails_before": not check(task, original),
        }

        # tokenwise: selector gets the map, editor gets the chosen (maybe focused) files
        tw_root = tmp / "tokenwise"
        copy_project(task.project, tw_root)
        tw_model = ScriptedModel(json.dumps(ref["targets"]), ref["edits"])
        result = run(task.request, replace(settings, root=tw_root), tw_model)
        if not result.ok:
            raise SystemExit(f"{task.name}: reference edit did not apply: {result.error}")
        apply_changes(replace(settings, root=tw_root), result.changes)
        row["tokenwise"] = {
            "calls": tw_model.calls,
            "difficulty": result.level,
            "model": result.model,
            "lines": sum(result.lines),
            "passes": check(task, tw_root),
            "focused": "(shown in part)" in json.dumps(ref["targets"])
            or "::" in "".join(ref["targets"]),
        }

        # baseline: the whole project in, whole changed files out
        bl_root = tmp / "baseline"
        copy_project(task.project, bl_root)
        whole = "".join(
            f"=== FILE: {c.path} ===\n{c.new}{'' if c.new.endswith(chr(10)) else chr(10)}=== END FILE ===\n"
            for c in result.changes
        )
        bl_model = ScriptedModel(whole)
        bl_settings = replace(settings, root=bl_root)
        bl = baseline(task.request, bl_settings, bl_model, baseline_model(bl_settings))
        if not bl.ok:
            raise SystemExit(f"{task.name}: baseline did not apply: {bl.error}")
        apply_changes(bl_settings, bl.changes)
        row["baseline"] = {
            "calls": bl_model.calls,
            "model": bl.model,
            "lines": sum(bl.lines),
            "passes": check(task, bl_root),
        }
    return row


def totals(rows: list[dict], method: str) -> dict:
    calls = [c for r in rows for c in r[method]["calls"]]
    return {
        "input": sum(c["input"] for c in calls),
        "output": sum(c["output"] for c in calls),
        "cost": cost(calls),
    }


def pct(before: float, after: float) -> str:
    return f"{100 * (after - before) / before:+.0f}%" if before else "–"


def markdown(rows: list[dict]) -> str:
    out = [
        "# GitHub projelerinde tahmini token ve maliyet",
        "",
        "Model çağrılmadan, tokenwise'ın kendi koduyla ve her görevin referans çözümüyle hesaplandı "
        "(bkz. `estimate.py`). Model ilk denemede doğru dosyayı seçip en küçük doğru değişikliği "
        "yazarsa iki yöntemin ne gönderip ne alacağını gösterir. Token = karakter / 4 (iki yöntem "
        "için aynı). Fiyatlar: Claude Haiku/Sonnet/Opus 5.5 liste fiyatları, önbelleksiz.",
        "",
        "| Görev | Proje (token) | Baseline giriş | tokenwise giriş | Baseline çıkış | tokenwise çıkış "
        "| Baseline $ | tokenwise $ | tokenwise modeli | Kontrol |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | :---: |",
    ]
    for r in rows:
        b, t = r["baseline"], r["tokenwise"]
        b_in, t_in = sum(c["input"] for c in b["calls"]), sum(c["input"] for c in t["calls"])
        b_out, t_out = sum(c["output"] for c in b["calls"]), sum(c["output"] for c in t["calls"])
        warn = " ⚠" if b_in > CONTEXT_WARN else ""
        ok = "✅" if r["fails_before"] and b["passes"] and t["passes"] else "❌"
        model = t["model"].replace("claude-", "").replace("-5-5", "")
        out.append(
            f"| {r['task']} | {r['project_tokens']:,} | {b_in:,}{warn} | {t_in:,} | {b_out:,} | "
            f"{t_out:,} | {cost(b['calls']):.4f} | {cost(t['calls']):.4f} | {model} ({t['difficulty']}) "
            f"| {ok} |"
        )
    b, t = totals(rows, "baseline"), totals(rows, "tokenwise")
    out += [
        "",
        "| Toplam (20 görev) | Baseline | tokenwise | Değişim |",
        "| --- | ---: | ---: | ---: |",
        f"| Giriş tokeni | {b['input']:,} | {t['input']:,} | {pct(b['input'], t['input'])} |",
        f"| Çıkış tokeni | {b['output']:,} | {t['output']:,} | {pct(b['output'], t['output'])} |",
        f"| Maliyet ($) | {b['cost']:.2f} | {t['cost']:.2f} | {pct(b['cost'], t['cost'])} |",
        "",
        "⚠ = baseline girişi 200 bin tokeni aşıyor; bu boyutta tüm projeyi göndermek pratikte "
        "mümkün olmayabilir (model bağlam sınırı ve fiyat kademesi).",
        "",
        "✅ = kontrol orijinal kodda başarısız, iki yöntemin değişikliğinden sonra başarılı.",
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    tasks = load_tasks(HERE / "tasks.toml")
    refs = tomllib.loads((HERE / "reference.toml").read_text("utf-8"))
    rows = []
    for task in tasks:
        row = measure(task, refs[task.name])
        status = (
            "ok"
            if row["fails_before"] and row["baseline"]["passes"] and row["tokenwise"]["passes"]
            else "CHECK PROBLEM"
        )
        print(f"{task.name:26} {status}", file=sys.stderr)
        rows.append(row)
    (HERE / "estimate.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), "utf-8")
    (HERE / "estimate.md").write_text(markdown(rows), "utf-8")
    print(markdown(rows))


if __name__ == "__main__":
    main()
