import os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import stub_missing, use_src

use_src()
stub_missing("numpy")
from viet_asr.application.quality import word_error_rate

assert word_error_rate("xin chào bạn", "xin chào bạn") == 0.0
assert abs(word_error_rate("xin chào bạn", "xin chao") - 2 / 3) < 1e-9
assert word_error_rate("", "anything") == 0.0
