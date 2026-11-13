import os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import load_file, stub_missing

stub_missing("cv2")
try:
    import flask  # noqa: F401
except ImportError:
    import ast
    from _helpers import source

    tree = ast.parse(source("app.py"))
    assert any(
        "/api/health" in ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
    )
    sys.exit(0)
stub_missing("flask_cors", "flask_socketio")
m = load_file("app.py")
data = m.app.test_client().get("/api/health").get_json()
assert data["ok"] is True and "time" in data
