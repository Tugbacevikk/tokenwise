from tokenwise.selector import parse_paths, select_files


def test_parse_paths_reads_json_inside_text():
    text = 'Here you go:\n```json\n["./app/auth.py", "app\\\\util.py", "app/auth.py"]\n```'
    assert parse_paths(text) == ["app/auth.py", "app/util.py"]


def test_parse_paths_drops_unsafe_and_bad_json():
    assert parse_paths('["/etc/passwd", "../x.py", "ok.py"]') == ["ok.py"]
    assert parse_paths("no list here") == []
    assert parse_paths("[not json]") == []


def test_select_files_uses_cheap_model_and_map(settings, fake):
    llm = fake('["app/auth.py"]')
    chosen, reply = select_files("add a length check", "MAP TEXT", settings, llm)
    assert chosen == ["app/auth.py"]
    assert reply.role == "selector"
    assert llm.calls[0]["model"] == settings.selector_model
    assert "MAP TEXT" in llm.calls[0]["system"]


def test_select_files_respects_max_files(settings, fake):
    settings.max_files = 1
    chosen, _ = select_files("x", "", settings, fake('["a.py", "b.py"]'))
    assert chosen == ["a.py"]
