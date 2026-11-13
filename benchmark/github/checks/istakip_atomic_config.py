import ast, os, py_compile, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import source

py_compile.compile("web/app.py", doraise=True)
fn = next(
    n
    for n in ast.walk(ast.parse(source("web/app.py")))
    if isinstance(n, ast.FunctionDef) and n.name == "save_config"
)
body = ast.unparse(fn)
assert "os.replace" in body or ".replace(CONFIG_PATH" in body
