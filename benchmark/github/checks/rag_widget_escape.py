import os, subprocess, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import source
from _helpers import require

require("node")

code = source("static/widget.js")
assert subprocess.run(["node", "--check", "static/widget.js"]).returncode == 0
assert "Escape" in code and "keydown" in code
