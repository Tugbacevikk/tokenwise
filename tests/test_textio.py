import pytest

from tokenwise.editor import EditError, apply_changes, parse_edits, plan_changes, undo_last
from tokenwise.textio import BOM, NotTextError, Style, decode, detect_style, read_source

EDIT = """app/auth.py
<<<<<<< SEARCH
def login(user, password):
=======
def login(user, password):
    assert password
>>>>>>> REPLACE
"""


def test_detect_style():
    assert detect_style(b"a\r\nb\r\n") == Style("\r\n", False)
    assert detect_style(b"a\nb\n") == Style("\n", False)
    assert detect_style(BOM + b"a\r\nb\nc\r\n") == Style("\r\n", True)


def test_decode_normalises_and_rejects_binary():
    assert decode(BOM + b"a\r\nb\rc\n") == "a\nb\nc\n"
    with pytest.raises(NotTextError, match="not UTF-8"):
        decode(b"\xff\xfe\x00", "x.bin")


def test_crlf_file_keeps_crlf_after_edit_and_undo(settings):
    path = settings.root / "app/auth.py"
    original = path.read_bytes().replace(b"\n", b"\r\n")
    path.write_bytes(BOM + original)
    changes = plan_changes(settings.root, parse_edits(EDIT))
    assert "\r" not in changes[0].new  # the model and the diff only ever see LF
    assert changes[0].stats == (1, 0)

    apply_changes(settings, changes)
    written = path.read_bytes()
    assert written.startswith(BOM)
    assert b"    assert password\r\n" in written
    assert written.count(b"\n") == written.count(b"\r\n")  # no stray LF

    undo_last(settings)
    assert path.read_bytes() == BOM + original


def test_new_file_follows_crlf_of_its_siblings(settings):
    path = settings.root / "app/auth.py"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    edits = parse_edits(EDIT + "\napp/new.py\n<<<<<<< SEARCH\n=======\nX = 1\n>>>>>>> REPLACE\n")
    apply_changes(settings, plan_changes(settings.root, edits))
    assert (settings.root / "app/new.py").read_bytes() == b"X = 1\r\n"


def test_lf_file_stays_lf(settings):
    apply_changes(settings, plan_changes(settings.root, parse_edits(EDIT)))
    assert b"\r" not in (settings.root / "app/auth.py").read_bytes()


def test_non_utf8_file_gives_a_clear_error(settings):
    (settings.root / "app/auth.py").write_bytes(b"def login(user, password):\n    x = '\xe7'\n")
    with pytest.raises(EditError, match="not UTF-8"):
        plan_changes(settings.root, parse_edits(EDIT))
    assert "def login" in read_source(settings.root / "app/auth.py", strict=False)


def test_undo_file_from_older_version(settings):
    import json

    settings.state_dir.mkdir()
    (settings.state_dir / "last_change.json").write_text(json.dumps({"app/util.py": "OLD\n"}))
    assert undo_last(settings) == ["app/util.py"]
    assert (settings.root / "app/util.py").read_text() == "OLD\n"
