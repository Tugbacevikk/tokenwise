import os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import stub_missing, use_src

use_src()
stub_missing("numpy", "rapidfuzz")
from sesli_asistan.gateway.core.search import number_to_words

assert number_to_words(-5) == "eksi beş", number_to_words(-5)
assert number_to_words(-1200) == "eksi bin iki yüz", number_to_words(-1200)
assert number_to_words(5) == "beş" and number_to_words(0) == "sıfır"
