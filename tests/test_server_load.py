"""The hosted queue under load and through failures, against a real PostgreSQL: many
workers at once, a webhook storm, a worker process killed mid-job, and the database
connection dropping under a running worker.

Needs TOKENWISE_TEST_DATABASE_URL like tests/test_server.py; skipped without it."""

import os
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tests.test_server import (  # noqa: E402, F401 - fixtures
    DATABASE_URL,
    INSTALLATION,
    conn,
    gh,
    install,
    send_as,
    server,
    setup,
)
from tokenwise.server import db, queue  # noqa: E402
from tokenwise.server.worker import Worker  # noqa: E402

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="set TOKENWISE_TEST_DATABASE_URL")


def _worker(server, name: str) -> Worker:
    return Worker(db.connect(DATABASE_URL), github=None, server=server, name=name,
                  connect=lambda: db.connect(DATABASE_URL))  # fmt: skip


def _skip_retry_delays(conn) -> int:
    """Time travel for retries: queued jobs waiting for their delay may run now."""
    return conn.execute(
        "UPDATE jobs SET run_after = now() WHERE status = 'queued' AND run_after > now()"
    ).rowcount


def _status(conn, job_id: int) -> dict:
    return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


# ---------------------------------------------------------------- many workers


def test_eight_workers_run_every_job_once_and_never_one_key_twice_at_a_time(conn, server):
    """300 events over 25 keys (a key is one repository's scan or one PR's report) arrive
    while 8 workers run, and every 7th job fails on its first try. Every job must end done,
    having succeeded once (or been superseded by a newer job with the same key, which then
    did the work), and two jobs with the same key must never run at the same moment."""
    lock = threading.Lock()
    running: Counter = Counter()
    overlaps: list[str] = []
    succeeded: Counter = Counter()
    tries: Counter = Counter()

    def work_on(worker: Worker):
        def handler(payload: dict) -> None:
            job = worker.job
            key = job["dedupe_key"]
            with lock:
                running[key] += 1
                if running[key] > 1:
                    overlaps.append(key)
                tries[job["id"]] += 1
                first = tries[job["id"]] == 1
            try:
                time.sleep(0.002)
                if job["id"] % 7 == 0 and first:
                    raise RuntimeError("flaky")
                with lock:
                    succeeded[job["id"]] += 1
            finally:
                with lock:
                    running[key] -= 1

        return handler

    workers = [_worker(server, f"w{i}") for i in range(8)]
    for w in workers:
        w.handlers["work"] = work_on(w)
    producing = threading.Event()
    producing.set()

    def produce() -> None:
        mine = db.connect(DATABASE_URL)
        for n in range(300):
            queue.enqueue(mine, "work", {"n": n}, dedupe_key=f"repo:{n % 25}")
            if n % 10 == 0:
                time.sleep(0.005)
        mine.close()
        producing.clear()

    def drain(w: Worker) -> None:
        while w.run_once() is not None or producing.is_set():
            pass

    producer = threading.Thread(target=produce)
    producer.start()
    for _ in range(6):  # rounds: retries wait for their delay, which the test skips
        threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        producer.join()
        if not _skip_retry_delays(conn):
            break
    for w in workers:
        w.conn.close()

    rows = conn.execute("SELECT id, status, error, attempts FROM jobs").fetchall()
    assert overlaps == []
    assert {r["status"] for r in rows} == {"done"}
    superseded = {r["id"] for r in rows if (r["error"] or "").startswith("superseded")}
    assert set(succeeded) | superseded == {r["id"] for r in rows}
    assert set(succeeded.values()) == {1} and not set(succeeded) & superseded
    assert all(r["attempts"] == 2 for r in rows if r["id"] % 7 == 0 and r["id"] in succeeded)
    assert len(rows) >= 25  # merged while queued, so fewer jobs than events


@pytest.mark.parametrize("how", ["fail", "defer"])
def test_a_job_queued_while_one_with_its_key_is_put_back_takes_over(conn, monkeypatch, how):
    """The race the load test above found: a push queues a new scan between the failing
    scan's "is a newer one queued?" check and its own return to the queue. Only one job per
    key may wait, so that return used to crash the worker; now the new job takes over."""
    queue.enqueue(conn, "scan", {"ref": "a"}, dedupe_key="scan:1")
    job = queue.claim(conn, "w")
    real = queue._supersede
    arrived: list[int] = []

    def racing(c, j, error):
        if not arrived:  # the check finds nothing; the push lands right after it
            arrived.append(queue.enqueue(conn, "scan", {"ref": "b"}, dedupe_key="scan:1"))
            return False
        return real(c, j, error)

    monkeypatch.setattr(queue, "_supersede", racing)
    if how == "fail":
        assert queue.fail(conn, job, "boom") == "superseded"
    else:
        assert queue.defer(conn, job, 60, "rate limited") is True
    row = _status(conn, job["id"])
    assert row["status"] == "done" and row["error"].startswith(f"superseded by job {arrived[0]}")
    assert _status(conn, arrived[0])["status"] == "queued"


# ---------------------------------------------------------------- webhook storm


def test_a_webhook_storm_for_one_branch_leaves_one_scan_with_the_newest_commit(setup, conn):
    """40 pushes to one branch in quick succession, from 8 threads, plus GitHub redelivering
    one of them 10 times: every request is answered, each delivery counted once, and one
    scan of the newest commit waits in the queue."""
    app, _, _ = setup
    install(TestClient(app))

    def push(n: int) -> dict:
        return {"ref": "refs/heads/main", "after": f"{n:040x}", "installation": {"id": INSTALLATION},
                "repository": {"id": 555, "full_name": "acme/app", "default_branch": "main",
                               "pushed_at": 1763380000 + n}}  # fmt: skip

    answers: list[int] = []
    lock = threading.Lock()

    def send(batch: list[tuple[int, str]]) -> None:
        client = TestClient(app)
        for n, delivery in batch:
            code = send_as(client, "push", push(n), delivery).status_code
            with lock:
                answers.append(code)

    sends = [(n, f"d-{n}") for n in range(40)] + [(7, "d-7")] * 10
    threads = [threading.Thread(target=send, args=(sends[i::8],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(answers) == 50 and set(answers) == {200}
    deliveries = conn.execute(
        "SELECT COUNT(*) AS n FROM webhook_deliveries WHERE delivery_id LIKE 'd-%%'"
    ).fetchone()["n"]
    assert deliveries == 40  # the redeliveries of d-7 were not counted again
    scans = conn.execute(
        "SELECT payload FROM jobs WHERE kind = 'scan' AND status = 'queued'"
    ).fetchall()
    assert len(scans) == 1
    assert scans[0]["payload"]["ref"] == f"{39:040x}"  # the newest commit, whatever the order


# ---------------------------------------------------------------- crashes

CRASHING_WORKER = """
import os, sys
from tokenwise.server import db, queue
conn = db.connect(sys.argv[1])
job = queue.claim(conn, "doomed")
print(job["id"], flush=True)
os._exit(9)  # killed mid-job: no finish, no fail, no heartbeat ever again
"""


def test_a_worker_process_killed_mid_job_loses_it_to_another_worker(conn, server):
    job_id = queue.enqueue(conn, "ping", {})
    done = subprocess.run([sys.executable, "-c", CRASHING_WORKER, DATABASE_URL],
                          capture_output=True, text=True, timeout=60)  # fmt: skip
    assert done.returncode == 9 and done.stdout.strip() == str(job_id)
    assert _status(conn, job_id)["status"] == "running"

    survivor = _worker(server, "survivor")
    assert survivor.run_once() is None  # its heartbeat is still fresh: not taken yet
    conn.execute(  # STALE_MINUTES without a heartbeat
        "UPDATE jobs SET locked_at = now() - make_interval(mins => %s) WHERE id = %s",
        (queue.STALE_MINUTES + 1, job_id),
    )
    assert survivor.run_once() is None  # noticed and put back, after the retry delay
    row = _status(conn, job_id)
    assert row["status"] == "queued" and "doomed stopped answering" in row["error"]
    _skip_retry_delays(conn)
    assert survivor.run_once()["status"] == "done"
    row = _status(conn, job_id)
    assert (row["status"], row["attempts"], row["locked_by"]) == ("done", 2, "survivor")
    survivor.conn.close()


def _kill(admin, worker: Worker) -> None:
    pid = worker.conn.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
    admin.execute("SELECT pg_terminate_backend(%s)", (pid,))


def _until(check, seconds: float = 20) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_the_worker_outlives_a_dropped_connection_between_jobs(conn, server):
    worker = _worker(server, "steady")
    stop = threading.Event()
    loop = threading.Thread(target=worker.run_forever, kwargs={"idle": 0.05, "stop": stop})
    loop.start()
    try:
        first = queue.enqueue(conn, "ping", {})
        assert _until(lambda: _status(conn, first)["status"] == "done")
        _kill(conn, worker)  # a database restart, a network blip
        second = queue.enqueue(conn, "ping", {})
        assert _until(lambda: _status(conn, second)["status"] == "done")
        assert loop.is_alive()
    finally:
        stop.set()
        loop.join(timeout=10)
    assert not loop.is_alive()
    worker.conn.close()


def test_a_job_cut_off_by_a_dropped_connection_goes_back_at_once(conn, server):
    """Without the reconnect the job would sit as `running` for STALE_MINUTES."""
    worker = _worker(server, "cut")
    runs: list[int] = []

    def handler(payload: dict) -> None:
        runs.append(worker.job["id"])
        if len(runs) == 1:
            _kill(conn, worker)
            worker.conn.execute("SELECT 1")  # the handler's next query finds it gone

    worker.handlers["work"] = handler
    job_id = queue.enqueue(conn, "work", {})
    stop = threading.Event()
    loop = threading.Thread(target=worker.run_forever, kwargs={"idle": 0.05, "stop": stop})
    loop.start()
    try:
        assert _until(lambda: (_status(conn, job_id)["error"] or "").startswith("the worker"))
        row = _status(conn, job_id)
        assert row["status"] == "queued" and row["locked_by"] is None
        assert "database connection dropped" in row["error"] and row["attempts"] == 1
        _skip_retry_delays(conn)
        assert _until(lambda: _status(conn, job_id)["status"] == "done")
    finally:
        stop.set()
        loop.join(timeout=10)
    assert runs == [job_id, job_id]
    worker.conn.close()


def test_the_worker_waits_out_a_database_that_is_down(conn, server, monkeypatch):
    """While the database refuses connections the worker keeps trying, each wait longer
    than the last (up to a minute), and goes on once it is back."""
    worker = _worker(server, "patient")
    worker.conn.close()
    attempts: list[float] = []
    real = worker.connect

    def refusing():
        attempts.append(time.monotonic())
        if len(attempts) < 4:
            raise db.psycopg.OperationalError("connection refused")
        return real()

    worker.connect = refusing
    waits: list[float] = []
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda t=None: waits.append(t) or stop.is_set())
    job_id = queue.enqueue(conn, "ping", {})
    worker.handlers["ping"] = lambda payload: stop.set()  # one job, then stop
    worker.run_forever(idle=0.5, stop=stop)
    assert len(attempts) == 4 and waits[:3] == [0.5, 1.0, 2.0]
    assert _status(conn, job_id)["status"] == "done"
    worker.conn.close()


def test_retries_spread_out_and_end_in_failed(conn, server):
    """A job that always fails: retried MAX_ATTEMPTS times with a growing delay, then
    left as failed with its last error; the worker goes on with the next job."""
    worker = _worker(server, "w")
    worker.handlers["bad"] = lambda payload: (_ for _ in ()).throw(ValueError("nope"))
    bad = queue.enqueue(conn, "bad", {})
    delays = []
    for _ in range(queue.MAX_ATTEMPTS):
        assert worker.run_once()["id"] == bad
        row = _status(conn, bad)
        if row["status"] == "queued":
            delays.append(conn.execute("SELECT EXTRACT(EPOCH FROM run_after - now())::int AS s"
                                       " FROM jobs WHERE id = %s", (bad,)).fetchone()["s"])  # fmt: skip
            _skip_retry_delays(conn)
    assert len(delays) == queue.MAX_ATTEMPTS - 1 and delays == sorted(delays)
    row = _status(conn, bad)
    assert (row["status"], row["attempts"]) == ("failed", queue.MAX_ATTEMPTS)
    assert "ValueError: nope" in row["error"]
    good = queue.enqueue(conn, "ping", {})
    assert worker.run_once()["id"] == good and _status(conn, good)["status"] == "done"
    worker.conn.close()


def test_many_claims_at_once_do_not_deadlock(conn, server):
    """Workers polling an empty queue and claiming together must not lock each other up:
    16 workers, 2000 claims of an almost empty queue, a few seconds at most."""
    for n in range(20):
        queue.enqueue(conn, "ping", {"n": n})
    workers = [_worker(server, f"p{i}") for i in range(16)]
    errors: list[str] = []
    outcomes: defaultdict = defaultdict(int)

    def poll(w: Worker) -> None:
        try:
            for _ in range(125):
                job = w.run_once()
                outcomes["job" if job else "empty"] += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    started = time.monotonic()
    threads = [threading.Thread(target=poll, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    took = time.monotonic() - started
    for w in workers:
        w.conn.close()
    assert errors == [] and not any(t.is_alive() for t in threads)
    assert outcomes["job"] == 20 and outcomes["job"] + outcomes["empty"] == 2000
    assert took < 30, f"2000 claims took {took:.1f} s"


@pytest.mark.skipif(os.name == "nt", reason="kill signals")
def test_worker_command_restarts_cleanly_after_sigkill(conn, server):
    """The real worker loop in its own process, killed with SIGKILL while idle, then started
    again: nothing is left half-done and the new one takes the next job."""
    script = (
        "import sys, threading\n"
        "from tokenwise.server import db\n"
        "from tokenwise.server.config import ServerConfig\n"
        "from tokenwise.server.worker import Worker\n"
        "url = sys.argv[1]\n"
        "w = Worker(db.connect(url), github=None, server=ServerConfig.__new__(ServerConfig),\n"
        "           name=sys.argv[2], connect=lambda: db.connect(url))\n"
        "w.run_forever(idle=0.05)\n"
    )
    first = subprocess.Popen([sys.executable, "-c", script, DATABASE_URL, "first"])
    try:
        a = queue.enqueue(conn, "ping", {})
        assert _until(lambda: _status(conn, a)["status"] == "done")
    finally:
        first.kill()
        first.wait(timeout=10)
    b = queue.enqueue(conn, "ping", {})
    second = subprocess.Popen([sys.executable, "-c", script, DATABASE_URL, "second"])
    try:
        assert _until(lambda: _status(conn, b)["status"] == "done")
        assert _status(conn, b)["locked_by"] == "second"
    finally:
        second.kill()
        second.wait(timeout=10)
    assert queue.counts(conn) == {"done": 2}
