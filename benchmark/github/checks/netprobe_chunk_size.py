import os, sys, tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import load_file, raises

p = load_file("protocol.py")
with tempfile.NamedTemporaryFile(delete=False) as f:
    f.write(b"abcdef")
assert p.split_file(f.name, 4) == [b"abcd", b"ef"]
assert raises(ValueError, p.split_file, f.name, 0)
assert raises(ValueError, p.split_file, f.name, -1)
