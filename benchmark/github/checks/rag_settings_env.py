import os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import load_file, stub_missing

stub_missing("dotenv")
os.environ.update(RAG_CHUNK_SIZE="800", RAG_CHUNK_OVERLAP="50", RAG_TOP_K="6")
m = load_file("config/settings.py", "s1")
assert (m.CHUNK_SIZE, m.CHUNK_OVERLAP, m.TOP_K_RESULTS) == (800, 50, 6)
for k in ("RAG_CHUNK_SIZE", "RAG_CHUNK_OVERLAP", "RAG_TOP_K"):
    del os.environ[k]
m = load_file("config/settings.py", "s2")
assert (m.CHUNK_SIZE, m.CHUNK_OVERLAP, m.TOP_K_RESULTS) == (500, 100, 4)
