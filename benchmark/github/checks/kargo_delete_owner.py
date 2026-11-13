import os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import source

assert subprocess.run(["php", "-l", "kargo_sil.php"], capture_output=True).returncode == 0
code = source("kargo_sil.php")
delete = re.search(r'prepare\("DELETE FROM kargolar WHERE ([^"]+)"\)', code)
assert delete and "kullanici_id" in delete.group(1), "DELETE is not limited to the owner"
assert re.search(r'\$sil->bind_param\("ii",\s*\$kargo_id,\s*\$kullanici_id\)', code)
