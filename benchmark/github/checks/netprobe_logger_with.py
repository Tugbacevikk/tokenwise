import os, sys, tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import load_file

m = load_file("logger.py")
path = os.path.join(tempfile.mkdtemp(), "log.csv")
with m.TransferLogger(path) as log:
    log.log_success(1, 0, 1.5)
assert log._file.closed
assert "SUCCESS" in open(path, encoding="utf-8").read()
