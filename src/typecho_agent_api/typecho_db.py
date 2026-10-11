"""
数据库连接 & 通用 CRUD 工具。

Typecho 的表结构要点（见 install/Mysql.sql）：
- typecho_contents : 文章/页面/附件主表
- typecho_metas    : 分类(category) + 标签(tag) 表
- typecho_relationships : contents <-> metas 多对多
- typecho_users    : 用户
- typecho_fields   : 文章自定义字段
- typecho_options  : 站点设置

由于我们要直接接管 PHP 后台的“新增/编辑/删除文章”逻辑，
最简单可靠的方式是直连 MySQL，按照 typecho 的规则写入。
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pymysql
from dbutils.pooled_db import PooledDB, TooManyConnections

from .config import CONFIG

logger = logging.getLogger(__name__)

# 全局连接池
_POOL: Optional[PooledDB] = None

# 这些异常出现时 DBUtils 会自动丢弃坏连接并重连重试
_DB_FAILURES = (
    pymysql.OperationalError,
    pymysql.InterfaceError,
    pymysql.InternalError,
)


def get_pool() -> PooledDB:
    global _POOL
    if _POOL is None:
        cfg = CONFIG.typecho
        _POOL = PooledDB(
            creator=pymysql,
            mincached=1,
            maxcached=5,
            maxconnections=max(1, cfg.pool_max_connections),
            # 不用 blocking=True：那是**无限等待**，一旦有连接没归还，
            # 所有工作线程都会卡在这里，服务表现成「假死」。
            # 改成非阻塞 + 在 _borrow() 里自己实现带超时的等待。
            blocking=False,
            ping=1,            # 每次从池取出时 ping，自动绕开 MySQL wait_timeout 断连
            failures=_DB_FAILURES,
            reset=True,
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            charset=cfg.charset,
            autocommit=False,
            # 没有这三个超时，MySQL 或网络一卡住线程就永久挂起
            connect_timeout=cfg.connect_timeout,
            read_timeout=cfg.read_timeout,
            write_timeout=cfg.write_timeout,
            cursorclass=pymysql.cursors.DictCursor,
        )
        logger.info("MySQL pool initialized for %s@%s/%s", cfg.user, cfg.host, cfg.database)
    return _POOL


def _borrow():
    """从池中取连接，最多等 pool_wait_timeout 秒，超时直接报错而不是无限等。"""
    deadline = time.monotonic() + max(0.0, CONFIG.typecho.pool_wait_timeout)
    while True:
        try:
            return get_pool().connection()
        except TooManyConnections:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "database connection pool exhausted "
                    f"(max={CONFIG.typecho.pool_max_connections})"
                ) from None
            time.sleep(0.05)


@contextmanager
def get_conn(conn=None):
    """
    获取连接，with 退出时自动 commit/rollback。

    传入已有的 conn 时（嵌套调用场景）会**复用**它：既不重复占连接，
    也不在中间层提前 commit，让整个请求落在同一个事务里。
    事务的提交/回滚仍然由最外层负责。
    """
    if conn is not None:
        yield conn
        return

    conn = _borrow()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败也别盖掉原始异常
            logger.exception("rollback failed")
        raise
    finally:
        conn.close()  # 实际是归还到池


# ---------- 通用查询工具 ----------
#
# ⚠️ update / delete / select_* 的 `where` 参数是**直接字符串拼接**进 SQL 的，
# 不是参数化。**调用方必须传硬编码字符串**，绝对不要把用户输入拼进去。
# 用户输入的值请走 where_args 走 `%s` 占位符。
# 例如：  select_one(conn, "contents", "cid=%s", (cid,))   ← 安全
#       select_one(conn, "contents", f"cid={cid}")           ← 注入！
# 现有所有调用方都遵守了这个约定。
# ---------- 通用查询工具 ----------

def _format_in(seq: Sequence[Any]) -> str:
    return ",".join(["%s"] * len(seq))


def _q(name: str) -> str:
    """表名加反引号，例如 _q('contents') -> `typecho_contents`。"""
    return f"`{CONFIG.typecho.table(name)}`"


def insert(conn, table: str, rows: Dict[str, Any]) -> int:
    """INSERT 一行，返回自增 id。"""
    cols = list(rows.keys())
    sql = f"INSERT INTO {_q(table)} ({','.join('`'+c+'`' for c in cols)}) VALUES ({_format_in(cols)})"
    with conn.cursor() as cur:
        cur.execute(sql, [rows[c] for c in cols])
        return cur.lastrowid


def update(conn, table: str, rows: Dict[str, Any], where: str, where_args: Sequence[Any]) -> int:
    """UPDATE，返回受影响行数。"""
    if not rows:
        return 0
    set_sql = ",".join(f"`{k}`=%s" for k in rows.keys())
    sql = f"UPDATE {_q(table)} SET {set_sql} WHERE {where}"
    with conn.cursor() as cur:
        return cur.execute(sql, list(rows.values()) + list(where_args))


def delete(conn, table: str, where: str, where_args: Sequence[Any]) -> int:
    sql = f"DELETE FROM {_q(table)} WHERE {where}"
    with conn.cursor() as cur:
        return cur.execute(sql, list(where_args))


def select_one(conn, table: str, where: str, where_args: Sequence[Any]) -> Optional[Dict[str, Any]]:
    sql = f"SELECT * FROM {_q(table)} WHERE {where} LIMIT 1"
    with conn.cursor() as cur:
        cur.execute(sql, list(where_args))
        return cur.fetchone()


def select_all(conn, table: str, where: str = "1=1", where_args: Sequence[Any] = (),
               order_by: str = "", limit: int = 0, offset: int = 0,
               columns: str = "*") -> List[Dict[str, Any]]:
    sql = f"SELECT {columns} FROM {_q(table)} WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit > 0:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    with conn.cursor() as cur:
        cur.execute(sql, list(where_args))
        return list(cur.fetchall())


def count(conn, table: str, where: str = "1=1", where_args: Sequence[Any] = ()) -> int:
    """
    COUNT(*)。分页一定要用这个，不要把整表读进内存再 len()——
    typecho_contents.text 是 LONGTEXT，文章一多那会把内存打满。
    """
    sql = f"SELECT COUNT(*) AS n FROM {_q(table)} WHERE {where}"
    with conn.cursor() as cur:
        cur.execute(sql, list(where_args))
        row = cur.fetchone()
        return int(row["n"]) if row else 0


def now_ts() -> int:
    return int(time.time())
