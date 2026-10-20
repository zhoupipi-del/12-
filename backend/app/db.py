from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):  # noqa: ANN001
            dbapi_conn.isolation_level = None  # 由下面的 begin 事件显式开启事务
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        @event.listens_for(eng, "begin")
        def _begin_immediate(conn):  # noqa: ANN001
            # SQLite 没有行锁：每个事务一开始就拿写锁，让“先查后改”的业务规则在演示/测试库上也是串行的。
            # 正式环境用 MySQL（InnoDB 行锁 + SELECT ... FOR UPDATE）。
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    return eng


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
