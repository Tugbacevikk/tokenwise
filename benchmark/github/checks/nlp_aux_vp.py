import os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import load_file

m = load_file("convert_to_bio.py")
assert m.get_chunk_tag("AUX", "aux") == "VP"
assert m.get_chunk_tag("VERB", "root") == "VP" and m.get_chunk_tag("NOUN", "obj") == "NP"
