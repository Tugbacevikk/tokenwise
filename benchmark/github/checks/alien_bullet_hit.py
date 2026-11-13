import os, subprocess, sys, textwrap

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import source

code = source("js/game.js")
start = code.index("function isColliding")
end = code.index("// === ENGEL")
harness = (
    textwrap.dedent("""
    let score = 0, bullets = [], enemies = [];
    const canvas = { width: 800 };
    function playCollisionSound() {}
""")
    + code[start:end]
    + textwrap.dedent("""
    bullets = [{ x: 300, y: 300, width: 10, height: 10, speed: 10 }, { x: 100, y: 10, width: 10, height: 10, speed: 10 }];
    enemies = [{ x: 105, y: 5, width: 20, height: 20 }];
    updateBullets();
    if (enemies.length !== 0) throw new Error("enemy not removed");
    if (bullets.length !== 1 || bullets[0].x !== 310) throw new Error("hit bullet still flying: " + JSON.stringify(bullets));
""")
)
r = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
