"""Engine and session factory.

SQLite concurrency strategy: every transaction starts with BEGIN IMMEDIATE, which takes
SQLite's single write lock up front. Writers therefore serialize (waiting up to
busy_timeout) and a read-then-write sequence such as "sum reservations, compare, insert"
cannot interleave with another writer. This holds across threads AND across processes
that share the same database file on one machine. It is NOT a distributed lock: it gives
no protection across hosts, and SQLite over network filesystems is unsafe.
"""

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from vmlm.db.models import Base

BUSY_TIMEOUT_MS = 30_000


def make_engine(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    connect_args = {"timeout": BUSY_TIMEOUT_MS / 1000} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args)
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
        def _begin_immediate(conn: Any) -> None:
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
