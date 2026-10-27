"""Engine and session factory.

SQLite concurrency strategy
---------------------------
Every *write* transaction starts with BEGIN IMMEDIATE, which takes SQLite's single write lock
up front. Writers therefore serialize (waiting up to busy_timeout) and a read-then-write
sequence such as "sum reservations on host H, compare, insert" cannot interleave with
another writer. This holds across threads AND across processes that share the same
database file on one machine. It is NOT a distributed lock: it gives no protection across
hosts, and SQLite over network filesystems is unsafe.

Read-only units of work use a deferred BEGIN instead (domain.core.read_tx). Under WAL they
see a consistent snapshot and run concurrently with the single writer. Rule: a read_tx must
never write; anything that reads-then-writes uses tx() so it holds the lock from the start.

Limitations of SQLite under concurrency: one writer at a time for the whole file, so write
throughput is bounded by commit latency; long provider calls must never run inside a
transaction (the worker never does). Measured effect of the read/write split: see
docs/design-decisions.md (load test before/after).

PostgreSQL
----------
The same code runs on PostgreSQL (`VMLM_DATABASE_URL=postgresql+psycopg://...`). There the
scheduler relies on `SELECT ... FOR UPDATE` on host rows (see Scheduler.place), which SQLite
ignores. PostgreSQL has not been exercised by the automated test suite in this repository.
"""

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from vmlm.db.models import Base

BUSY_TIMEOUT_MS = 30_000
READ_ONLY_OPTION = "vmlm_read_only"


def make_engine(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    connect_args = {"timeout": BUSY_TIMEOUT_MS / 1000} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=not is_sqlite)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _configure(dbapi_conn: Any, _record: Any) -> None:
            dbapi_conn.isolation_level = None  # we emit BEGIN ourselves
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        @event.listens_for(engine, "begin")
        def _begin(conn: Any) -> None:
            # Read-only units (see domain.core.read_tx) use a deferred BEGIN: under WAL they
            # read a consistent snapshot and neither wait for nor block the writer.
            if conn.get_execution_options().get(READ_ONLY_OPTION):
                conn.exec_driver_sql("BEGIN")
            else:
                conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
