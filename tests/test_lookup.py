from tokenwise.config import Settings
from tokenwise.lookup import definitions, mentioned_targets, merge_selection, resolve_path
from tokenwise.pipeline import run


def test_definitions_with_owner_class():
    src = "class Store:\n    def save_config(self):\n        pass\n\ndef number_to_words(n):\n    pass\n"
    assert definitions("a.py", src) == {
        "Store": "Store",
        "save_config": "Store.save_config",
        "number_to_words": "number_to_words",
    }
    ts = "export class Cart {\n  add(item: string) {\n  }\n}\nexport function total(x) {}\n"
    assert definitions("a.ts", ts) == {"Cart": "Cart", "add": "Cart.add", "total": "total"}


def test_mentioned_targets(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web/app.py").write_text(
        "def save_config(cfg):\n    pass\n\ndef load_config():\n    pass\n"
    )
    (tmp_path / "isci_takip.py").write_text("def main():\n    pass\n")
    for i in range(4):  # a name defined in many files is too common to be a hint
        (tmp_path / f"m{i}.py").write_text("def main():\n    pass\n")
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.py"))
    settings = Settings(root=tmp_path)
    assert mentioned_targets("save_config önce geçici dosyaya yazsın; main", settings, files) == [
        ("web/app.py", "save_config")
    ]
    assert mentioned_targets("add a log line", settings, files) == []


def test_resolve_path():
    known = ["predict.py", "isci_takip.py", "a/util.py", "b/util.py"]
    assert resolve_path("path/to/predict.py", known) == "predict.py"
    assert resolve_path("core/isci_takip.py", known) == "isci_takip.py"
    assert resolve_path("x/util.py", known) == "x/util.py"  # ambiguous: left alone
    assert resolve_path("new_module.py", known) == "new_module.py"  # a new file


def test_merge_selection():
    files = ["web/app.py", "isci_takip.py", "core/search.py", "libs/normalizer.py"]
    paths, focus = merge_selection(
        ["core/isci_takip.py"], {}, [("web/app.py", "save_config")], files, 8
    )
    assert paths == ["web/app.py", "isci_takip.py"] and focus == {"web/app.py": ["save_config"]}
    # the selector chose the wrong file; the named definition goes first, focused
    paths, focus = merge_selection(
        ["libs/normalizer.py"], {}, [("core/search.py", "number_to_words")], files, 8
    )
    assert paths[0] == "core/search.py" and focus == {"core/search.py": ["number_to_words"]}
    # even when the selector names the whole file, the named function is the focus
    paths, focus = merge_selection(["web/app.py"], {}, [("web/app.py", "save_config")], files, 8)
    assert focus == {"web/app.py": ["save_config"]}


def test_pipeline_rescues_wrong_selection_and_placeholder_path(tmp_path, fake):
    (tmp_path / "core").mkdir()
    (tmp_path / "libs").mkdir()
    body = "".join(f"def helper_{i}(x):\n    return x + {i}\n\n\n" for i in range(120))
    (tmp_path / "core/search.py").write_text(
        body
        + "def number_to_words(n):\n    if n == 0:\n        return 'sıfır'\n    return str(n)\n"
    )
    (tmp_path / "libs/normalizer.py").write_text("def fold(text):\n    return text\n")
    edit = "path/to/search.py\n<<<<<<< SEARCH\n    if n == 0:\n=======\n    if n < 0:\n        return 'eksi ' + number_to_words(-n)\n    if n == 0:\n>>>>>>> REPLACE"
    llm = fake('["libs/normalizer.py"]', edit)
    result = run(
        "number_to_words negatif sayıları 'eksi' ile okusun",
        Settings(root=tmp_path),
        llm,
        model="m",
    )
    assert result.ok and result.changes[0].path == "core/search.py"
    assert result.files[0] == "core/search.py"
    assert "core/search.py (shown in part)" in llm.calls[1]["prompt"]  # only the named function
