import os, shutil, subprocess, tempfile

out = tempfile.mkdtemp()
assert subprocess.run(["javac", "-d", out, "TuglangLexer.java"]).returncode == 0
probe = os.path.join(out, "Probe.java")
with open(probe, "w") as f:
    f.write(
        "public class Probe { public static void main(String[] a) {"
        ' System.out.print(TuglangLexer.tokenize("degilse { yaz x; }").get(0).tip); } }'
    )
assert subprocess.run(["javac", "-cp", out, "-d", out, probe]).returncode == 0
r = subprocess.run(["java", "-cp", out, "Probe"], capture_output=True, text=True)
assert r.stdout.strip() == "ANAHTAR_KELIME", r.stdout
shutil.rmtree(out)
