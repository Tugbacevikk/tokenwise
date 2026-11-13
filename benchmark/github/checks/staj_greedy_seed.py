import os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import load_file

g = load_file("greedy.py")
students = [{"gno": 3.0 + i / 10, "durum": "Yerlesmedi", "tercihler": ["A", "B"]} for i in range(6)]
firms = {"A": 2, "B": 2, "C": 2}
a = g.greedy_ata(students, firms, 0.3, seed=7)
b = g.greedy_ata(students, firms, 0.3, seed=7)
assert [s["yerlestigi_firma"] for s in a["liste"]] == [s["yerlestigi_firma"] for s in b["liste"]]
assert a["tur"] == b["tur"]
