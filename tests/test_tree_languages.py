"""Go, Java and the Tree-sitter reading of JavaScript/TypeScript: functions, imports and the
JavaScript security rules."""

from __future__ import annotations

import pytest

from tests.test_platform import commit, sh
from tokenwise import syntax
from tokenwise.analyzers import analyze, code_findings
from tokenwise.callgraph import calls, function_sizes
from tokenwise.config import Settings
from tokenwise.graph import build_graph, js_imports
from tokenwise.health import task_for
from tokenwise.release import assess

needs_go = pytest.mark.skipif(not syntax.available(syntax.GO), reason="tree-sitter-go missing")
needs_java = pytest.mark.skipif(not syntax.available(syntax.JAVA), reason="tree-sitter-java")
needs_js = pytest.mark.skipif(not syntax.available(syntax.JAVASCRIPT), reason="tree-sitter")

GO_SERVICE = """package orders

import "fmt"

type Service struct{ store Store }

func (s *Service) Place(qty int, rush bool) error {
	if qty <= 0 || qty > 100 {
		return fmt.Errorf("bad qty")
	}
	switch {
	case rush:
		s.store.Save(qty)
	case qty > 10:
		s.store.Save(qty * 2)
	default:
	}
	go func() {
		if rush {
			notify()
		}
	}()
	return nil
}

func notify() {}
"""

JAVA_ORDER = """package shop.orders;

import java.util.List;

public class OrderService {
    public OrderService() {}

    public int total(List<Integer> prices, boolean member) {
        int sum = 0;
        for (int p : prices) {
            sum += p > 100 ? p - 10 : p;
        }
        switch (sum) {
            case 0: return 0;
            case 1: break;
            default: break;
        }
        Runnable log = () -> { if (member) System.out.println(sum); };
        return member && sum > 50 ? sum - 5 : sum;
    }
}
"""


@needs_go
def test_go_functions_are_named_by_receiver_and_measured():
    sizes = {d.name: score for d, score in function_sizes("orders/service.go", GO_SERVICE)}
    # Place: 1 + if + || + two cases (not default) + the closure's if
    assert sizes == {"Service.Place": 6, "notify": 1}
    assert ("Save", 13) in calls("orders/service.go", GO_SERVICE)


@needs_java
def test_java_methods_constructors_and_lambdas_are_measured():
    found = {d.name: (score, d.nested) for d, score in function_sizes("A.java", JAVA_ORDER)}
    # total: 1 + for + ?: + two cases (not default) + && + ?:
    assert found["OrderService.total"] == (7, False)
    assert found["OrderService.OrderService"] == (1, False)
    assert found["OrderService.total.log"] == (2, True)  # counted on its own


@needs_go
def test_go_complexity_becomes_a_finding():
    branches = "".join(f"\tif a == {i} {{\n\t\treturn {i}\n\t}}\n" for i in range(12))
    source = f"package p\n\nfunc Decide(a int) int {{\n{branches}\treturn 0\n}}\n"
    (finding,) = code_findings("p/decide.go", source)
    assert (finding.rule, finding.key, finding.severity) == ("high-complexity", "Decide", "medium")


def _go_project(tmp_path):
    files = {
        "go.mod": "module example.com/shop\n\ngo 1.22\n",
        "orders/service.go": GO_SERVICE,
        "orders/store.go": "package orders\n\ntype Store interface{ Save(n int) }\n",
        "orders/service_test.go": 'package orders\n\nimport "testing"\n\n'
        "func TestPlace(t *testing.T) {\n\tvar s Service\n\t_ = s.Place(1, false)\n}\n",
        "api/handler.go": 'package api\n\nimport "example.com/shop/orders"\n\n'
        "func Handle(s *orders.Service) error {\n\treturn s.Place(1, true)\n}\n",
        "cmd/tool/main.go": 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println(1) }\n',
    }
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, "utf-8")
    return files


@needs_go
def test_go_graph_follows_module_imports_and_same_package_names(tmp_path):
    _go_project(tmp_path)
    graph = build_graph(Settings(root=tmp_path))
    assert graph.deps["api/handler.go"] == {"orders/service.go", "orders/store.go"}
    assert "orders/store.go" in graph.deps["orders/service.go"]  # uses Store, no import
    assert graph.deps["orders/service_test.go"] == {"orders/service.go"}
    assert not graph.runtime  # never an import-cycle finding
    assert graph.tested("orders/service.go")
    assert not graph.tested("api/handler.go")
    assert graph.fan_in("orders/service.go") == 1  # the handler; the test is not counted


@needs_go
def test_go_change_lists_callers_in_other_packages_and_tests(tmp_path):
    files = _go_project(tmp_path)
    sh(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "start", files)
    sh(tmp_path, "checkout", "-q", "-b", "feature")
    path = tmp_path / "orders" / "service.go"
    path.write_text(path.read_text("utf-8").replace("qty > 100", "qty > 500"), "utf-8")
    report = assess(Settings(root=tmp_path), "main")
    (item,) = report.functions
    assert item["function"] == "Service.Place"
    assert item["callers"] == ["api/handler.go:Handle"]
    assert item["tests"] == ["orders/service_test.go:TestPlace"]


def _java_project(tmp_path):
    files = {
        "src/main/java/shop/orders/OrderService.java": JAVA_ORDER,
        "src/main/java/shop/orders/Discounts.java": "package shop.orders;\n\n"
        "class Discounts {\n    int apply(OrderService s) { return s.total(null, true); }\n}\n",
        "src/main/java/shop/api/Controller.java": "package shop.api;\n\n"
        "import shop.orders.OrderService;\n\npublic class Controller {\n"
        "    int get(OrderService s) { return s.total(null, false); }\n}\n",
        "src/main/java/shop/api/Wild.java": "package shop.api;\n\nimport shop.orders.*;\n\n"
        "class Wild {}\n",
        "src/test/java/shop/orders/OrderServiceTest.java": "package shop.orders;\n\n"
        "class OrderServiceTest {\n    void sums() { new OrderService().total(null, true); }\n}\n",
    }
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, "utf-8")
    return files


@needs_java
def test_java_graph_follows_imports_wildcards_and_same_package_classes(tmp_path):
    _java_project(tmp_path)
    graph = build_graph(Settings(root=tmp_path))
    main = "src/main/java/shop/"
    service = main + "orders/OrderService.java"
    assert graph.deps[main + "api/Controller.java"] == {service}
    assert graph.deps[main + "api/Wild.java"] == {service, main + "orders/Discounts.java"}
    assert graph.deps[main + "orders/Discounts.java"] == {service}  # same package, no import
    assert service in graph.deps["src/test/java/shop/orders/OrderServiceTest.java"]
    assert graph.tested(service)
    assert not graph.runtime


@needs_java
def test_java_change_lists_callers_across_packages(tmp_path):
    files = _java_project(tmp_path)
    sh(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "start", files)
    sh(tmp_path, "checkout", "-q", "-b", "feature")
    path = tmp_path / "src/main/java/shop/orders/OrderService.java"
    path.write_text(path.read_text("utf-8").replace("p - 10", "p - 20"), "utf-8")
    report = assess(Settings(root=tmp_path), "main")
    (item,) = report.functions
    assert item["function"] == "OrderService.total"
    assert item["callers"] == [
        "src/main/java/shop/api/Controller.java:Controller.get",
        "src/main/java/shop/orders/Discounts.java:Discounts.apply",
    ]
    assert item["tests"] == [
        "src/test/java/shop/orders/OrderServiceTest.java:OrderServiceTest.sums"
    ]


# ---------------------------------------------------------------- JavaScript imports

IMPORTS = """
// import x from './commented'
const doc = "require('./in-string')";
import type { A } from './types';
export type { B } from './btypes';
import './side';
export * from './reexport';
export function lazy() { return require('./lazy'); }
const later = () => import('./dyn');
const top = require('./top');
"""


@needs_js
def test_js_imports_skip_comments_and_strings_and_know_what_runs_at_load():
    assert js_imports("a.ts", IMPORTS) == [
        ("./types", False),
        ("./btypes", False),
        ("./side", True),
        ("./reexport", True),
        ("./lazy", False),  # inside a function: the usual way to break a cycle
        ("./dyn", False),
        ("./top", True),
    ]


@needs_js
def test_a_require_inside_a_function_does_not_make_a_cycle(tmp_path):
    (tmp_path / "a.js").write_text("const b = require('./b');\nmodule.exports = 1;\n", "utf-8")
    (tmp_path / "b.js").write_text("function f() { return require('./a'); }\n", "utf-8")
    graph = build_graph(Settings(root=tmp_path))
    assert graph.deps["b.js"] == {"a.js"}
    assert graph.cycles() == []


def test_js_imports_fall_back_to_a_pattern_without_tree_sitter(monkeypatch):
    monkeypatch.setattr(syntax, "parse", lambda path, source: None)
    found = dict(js_imports("a.ts", IMPORTS))
    assert found["./commented"] is True  # the pattern cannot tell a comment apart
    assert found["./types"] is False and found["./dyn"] is False


# ---------------------------------------------------------------- JavaScript security

RISKY = """import * as cp from 'child_process';
import { exec as run, spawn } from 'node:child_process';
const { execSync } = require('child_process');
// eval(userInput) is only a comment
export function handler(req, el) {
  /\\d+/.exec(req.q);
  run(req.cmd);
  cp.exec(req.cmd);
  spawn('ls', [req.dir]);
  spawn('ls', [], { shell: true });
  execSync(req.cmd);
  eval(req.code);
  new Function('a', req.body);
  setTimeout('tick()', 10);
  setTimeout(() => 1, 10);
  el.innerHTML = req.html;
  el.innerHTML = '<b>fixed</b>';
  document.write(req.x);
}
export const View = ({ h }) => <div dangerouslySetInnerHTML={{ __html: h }} />;
"""


@needs_js
def test_js_security_rules_find_risky_calls_and_skip_lookalikes():
    found = [(f.line, f.rule, f.key) for f in code_findings("src/app.jsx", RISKY)
             if f.category == "security"]  # fmt: skip
    assert found == [
        (7, "shell-command", "exec@handler"),
        (8, "shell-command", "exec@handler"),
        (10, "shell-command", "spawn@handler"),
        (11, "shell-command", "execSync@handler"),
        (12, "dynamic-code", "eval@handler"),
        (13, "dynamic-code", "Function@handler"),
        (14, "dynamic-code", "setTimeout@handler"),
        (16, "unsafe-html", "innerHTML@handler"),
        (18, "unsafe-html", "document.write@handler"),
        (20, "unsafe-html", "dangerouslySetInnerHTML@View"),
    ]


@needs_js
def test_exec_without_child_process_is_a_regex_not_a_command():
    source = "const exec = (s) => /a/.exec(s);\nexport function f(x) { return exec(x); }\n"
    assert not [f for f in code_findings("a.js", source) if f.category == "security"]


@needs_js
def test_js_security_findings_reach_the_scan_and_get_a_fix_task(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "view.js").write_text("export function show(el, v) {\n"
                                              "  el.innerHTML = v;\n}\n", "utf-8")  # fmt: skip
    settings = Settings(root=tmp_path)
    (finding,) = [f for f in analyze(settings, build_graph(settings)) if f.rule == "unsafe-html"]
    task = task_for({"id": 1, "rule": finding.rule, "file": finding.file, "line": finding.line,
                     "title": finding.title, "data": finding.data})  # fmt: skip
    assert "textContent" in task["request"] and task["intent"] == "change"


def test_languages_without_a_grammar_are_skipped_quietly(monkeypatch):
    monkeypatch.setattr(syntax, "parse", lambda path, source: None)
    assert code_findings("a.go", GO_SERVICE) == []
    assert function_sizes("A.java", JAVA_ORDER) == []
