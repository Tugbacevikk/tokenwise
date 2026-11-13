import os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import raises, stub_missing, use_src

use_src()
stub_missing("numpy")
from viet_asr.infrastructure.chunker import EnergyChunker

assert raises(ValueError, EnergyChunker, max_len=5.0, min_len=5.0, native=False)
assert raises(ValueError, EnergyChunker, max_len=4.0, min_len=5.0, native=False)
assert EnergyChunker(max_len=15.0, min_len=5.0, native=False).max_len == 15.0
