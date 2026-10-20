#!/bin/sh
# Build a throwaway repo for the Phase 0 trial run: a tiny SQLite job queue
# with three small tasks (one per dial setting). Stdlib Python only.
#
#   scripts/sandbox.sh [dir]     default: ~/projects/rep-sandbox
set -eu
DIR="${1:-$HOME/projects/rep-sandbox}"
if [ -e "$DIR" ]; then
  echo "$DIR already exists; remove it or pass another path" >&2
  exit 1
fi
mkdir -p "$DIR/jobq" "$DIR/tests"
cd "$DIR"

cat > jobq/__init__.py <<'EOF'
EOF

cat > jobq/queue.py <<'EOF'
"""A tiny SQLite job queue."""
import sqlite3
import time

SCHEMA = """
create table if not exists jobs (
    id integer primary key,
    payload text not null,
    status text not null default 'pending',
    worker text,
    attempts integer not null default 0,
    run_at real not null default 0
)
"""


def connect(path, factory=sqlite3.Connection):
    conn = sqlite3.connect(path, timeout=5, isolation_level=None, factory=factory)
    conn.execute(SCHEMA)
    return conn


def enqueue(conn, payload):
    return conn.execute("insert into jobs (payload) values (?)", (payload,)).lastrowid


def claim(conn, worker, now=None):
    """Hand one runnable job to `worker`, or return None when there is none."""
    now = time.time() if now is None else now
    row = conn.execute(
        "select id, payload from jobs where status = 'pending' and run_at <= ? order by id limit 1", (now,)
    ).fetchone()
    if row is None:
        return None
    conn.execute("update jobs set status = 'running', worker = ? where id = ?", (worker, row[0]))
    return {"id": row[0], "payload": row[1]}


def complete(conn, job_id):
    conn.execute("update jobs set status = 'done' where id = ?", (job_id,))


def fail(conn, job_id):
    conn.execute("update jobs set status = 'failed', attempts = attempts + 1 where id = ?", (job_id,))


def status(conn, job_id):
    row = conn.execute("select status from jobs where id = ?", (job_id,)).fetchone()
    return row[0] if row else None
EOF

cat > tests/__init__.py <<'EOF'
EOF

cat > tests/test_queue.py <<'EOF'
import os
import tempfile
import unittest

from jobq import queue


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "jobs.db")
        self.conn = queue.connect(self.path)

    def test_claim_hands_out_jobs_in_order(self):
        first = queue.enqueue(self.conn, "a")
        queue.enqueue(self.conn, "b")
        self.assertEqual(queue.claim(self.conn, "w1")["id"], first)

    def test_claimed_job_is_not_handed_out_again(self):
        queue.enqueue(self.conn, "a")
        queue.claim(self.conn, "w1")
        self.assertIsNone(queue.claim(self.conn, "w2"))

    def test_complete_and_fail_set_status(self):
        a, b = queue.enqueue(self.conn, "a"), queue.enqueue(self.conn, "b")
        queue.complete(self.conn, a)
        queue.fail(self.conn, b)
        self.assertEqual((queue.status(self.conn, a), queue.status(self.conn, b)), ("done", "failed"))


if __name__ == "__main__":
    unittest.main()
EOF

cat > README.md <<'EOF'
# jobq: rep trial repo

Throwaway. Run the tests with `python3 -m unittest -q`.

Three small pieces of work. Start Claude Code here and try them in order.

1. `/shape Under load, some jobs run twice. Make claiming a job atomic.`
   Pick an approach, `/approve` with a pre-mortem, let it work, then `/review`.
2. `/shape A failed job should be retried with exponential backoff, at most 5 attempts, then marked dead.`
   Approve a tight budget and watch what happens when the work outgrows it.
3. No brief: "Add a stats(conn) function that returns the number of jobs per status."
   This is ordinary Claude Code; rep stays out of the way.

Afterwards, `rep report` shows how your estimates compared with what happened.
EOF

printf '__pycache__/\n*.db\n' > .gitignore

git init -q
git add -A
git -c user.name=rep -c user.email=rep@localhost commit -qm "jobq: trial repo for rep"
echo "sandbox ready at $DIR"
