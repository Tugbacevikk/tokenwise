import ast, os, py_compile, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import source

py_compile.compile("web/app.py", doraise=True)
fn = next(
    n
    for n in ast.walk(ast.parse(source("web/app.py")))
    if isinstance(n, ast.FunctionDef) and n.name == "api_users_add"
)
body = ast.unparse(fn)
assert "len(password) < 8" in body.replace(" ", " ") or "len(password)<8" in body.replace(" ", "")
