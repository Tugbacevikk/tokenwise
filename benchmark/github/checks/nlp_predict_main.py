import ast, os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import source

tree = ast.parse(source("predict.py"))
top_calls = [
    n
    for n in tree.body
    if isinstance(n, ast.Expr)
    and isinstance(n.value, ast.Call)
    and getattr(n.value.func, "id", "") == "predict"
]
assert not top_calls, "predict(...) still runs on import"
guard = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
assert guard and "predict(" in ast.unparse(guard[0])
