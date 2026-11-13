import os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(__file__))

assert subprocess.run(["node", "--check", "server.js"]).returncode == 0
proc = subprocess.Popen(
    ["node", "server.js"],
    env={**os.environ, "PORT": "38917"},
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    time.sleep(1.5)
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", 38917, timeout=5)
    conn.request("GET", "/../../../../etc/hostname")  # raw path, not normalised by the client
    status = conn.getresponse().status
    assert status == 403, status
    conn = http.client.HTTPConnection("127.0.0.1", 38917, timeout=5)
    conn.request("GET", "/server.js")
    assert conn.getresponse().status == 200
finally:
    proc.kill()
