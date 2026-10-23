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
    if url.startswith("mysql"):
        # 请求鉴权可能已读库；互斥锁等待后，普通查询必须看到前一事务刚提交的库存/占用。
        # 状态转换仍使用 FOR UPDATE；事务内持有互斥锁直到重算与业务一起提交。
        kwargs["isolation_level"] = "READ COMMITTED"
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
    """只提供会话。写请求的全局互斥锁在【鉴权通过之后】由 core.deps.lock_for_write 获取：
    匿名、令牌无效或无权限的请求不会去排队争锁，也就拖不慢合法写入。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
