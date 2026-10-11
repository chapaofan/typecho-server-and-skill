"""
分类(category) 和 标签(tag) 的管理。
对应 typecho_metas 表 + typecho_relationships 表。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from .config import CONFIG
from .typecho_db import delete, get_conn, insert, select_all, select_one


# ---------- slug 生成（与 Typecho 的 Common::slugName 行为一致）----------

def _slugify(name: str) -> str:
    # Typecho 默认实现：把非 ASCII 字母数字替换成 '-'
    slug = re.sub(r"[^a-zA-Z0-9一-龥]+", "-", name).strip("-").lower()
    return slug or ""


# ---------- meta 基础操作 ----------

def list_categories() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        return select_all(conn, "metas", "type=%s", ("category",), order_by="`order` ASC, mid ASC")


def list_tags() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        return select_all(conn, "metas", "type=%s", ("tag",), order_by="`order` ASC, mid ASC")


def get_meta(mid: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        return select_one(conn, "metas", "mid=%s", (mid,))


def get_meta_by_slug(slug: str, mtype: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        return select_one(conn, "metas", "slug=%s AND type=%s", (slug, mtype))


def get_meta_by_name(name: str, mtype: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        return select_one(conn, "metas", "name=%s AND type=%s", (name, mtype))


def create_meta(name: str, mtype: str, slug: Optional[str] = None,
                description: str = "", parent: int = 0) -> int:
    if mtype not in ("category", "tag"):
        raise ValueError(f"unsupported meta type: {mtype}")
    if not slug:
        slug = _slugify(name)
    rows = {
        "name": name,
        "slug": slug,
        "type": mtype,
        "description": description[:200],
        "count": 0,
        "order": 0,
        "parent": parent,
    }
    with get_conn() as conn:
        return insert(conn, "metas", rows)


def delete_meta(mid: int) -> bool:
    with get_conn() as conn:
        # 先解除所有关系
        delete(conn, "relationships", "mid=%s", (mid,))
        affected = delete(conn, "metas", "mid=%s", (mid,))
        return affected > 0


# ---------- 关联到文章 ----------

def _set_categories(conn, cid: int, category_mids: Sequence[int], recount: bool = True) -> None:
    """
    重建 cid 的分类关联。模仿 typecho EditTrait::setCategories 的语义。
    category_mids 是 mid 列表（int）。
    """
    category_mids = list({int(m) for m in category_mids if m})
    metas_t = CONFIG.typecho.table("metas")
    rels_t = CONFIG.typecho.table("relationships")

    # 已有的
    rows = select_all(conn, "metas",
                      f"{metas_t}.mid IN (SELECT mid FROM {rels_t} WHERE cid=%s) AND {metas_t}.type='category'",
                      (cid,))
    exist = {r["mid"] for r in rows}

    # 删除已经不存在的
    for old_mid in exist - set(category_mids):
        delete(conn, "relationships", "cid=%s AND mid=%s", (cid, old_mid))
        if recount:
            _dec_count(conn, old_mid)

    # 加入新的
    valid = {r["mid"] for r in select_all(conn, "metas", "mid IN ({}) AND type='category'".format(
        ",".join(["%s"] * len(category_mids)) if category_mids else "0"
    ), tuple(category_mids))} if category_mids else set()

    for new_mid in set(category_mids) & valid:
        # 已存在则跳过
        if select_one(conn, "relationships", "cid=%s AND mid=%s", (cid, new_mid)):
            continue
        insert(conn, "relationships", {"cid": cid, "mid": new_mid})
        if recount:
            _inc_count(conn, new_mid)


def _set_tags(conn, cid: int, tag_names: Sequence[str], recount: bool = True) -> List[int]:
    """
    重建 cid 的标签关联，自动按 name 扫描/创建。
    返回最终的 mid 列表。
    """
    tag_names = [t.strip() for t in tag_names if t and t.strip()]
    metas_t = CONFIG.typecho.table("metas")
    rels_t = CONFIG.typecho.table("relationships")

    # 已有的标签 mid
    rows = select_all(conn, "metas",
                      f"{metas_t}.mid IN (SELECT mid FROM {rels_t} WHERE cid=%s) AND {metas_t}.type='tag'",
                      (cid,))
    exist = {r["mid"] for r in rows}

    # 旧的删掉
    for old_mid in exist:
        delete(conn, "relationships", "cid=%s AND mid=%s", (cid, old_mid))
        if recount:
            _dec_count(conn, old_mid)

    # 创建/获取新标签
    new_mids: List[int] = []
    for name in tag_names:
        row = select_one(conn, "metas", "name=%s AND type='tag'", (name,))
        if row:
            mid = row["mid"]
        else:
            mid = insert(conn, "metas", {
                "name": name, "slug": _slugify(name), "type": "tag",
                "description": "", "count": 0, "order": 0, "parent": 0,
            })
        # 关联
        if not select_one(conn, "relationships", "cid=%s AND mid=%s", (cid, mid)):
            insert(conn, "relationships", {"cid": cid, "mid": mid})
        if recount:
            _inc_count(conn, mid)
        new_mids.append(mid)

    return new_mids


def _inc_count(conn, mid: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE `{CONFIG.typecho.table('metas')}` SET `count`=`count`+1 WHERE mid=%s",
            (mid,),
        )


def _dec_count(conn, mid: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE `{CONFIG.typecho.table('metas')}` SET `count`=GREATEST(`count`-1, 0) WHERE mid=%s",
            (mid,),
        )


# ---------- 公共 API ----------

def set_post_categories(cid: int, category_mids: Sequence[int], conn=None) -> List[int]:
    """
    重建 cid 的分类关联。

    conn 传入时复用调用方的事务（新增/编辑文章时应传，避免一个请求开好几个
    连接、也避免分类写进去了文章却回滚）。
    """
    with get_conn(conn) as c:
        _set_categories(c, cid, category_mids, recount=True)
        return list(category_mids)


def set_post_tags(cid: int, tag_names: Sequence[str], conn=None) -> List[int]:
    with get_conn(conn) as c:
        return _set_tags(c, cid, tag_names, recount=True)


def get_post_categories(cid: int, conn=None) -> List[Dict[str, Any]]:
    with get_conn(conn) as c:
        return select_all(
            c, "metas",
            "type='category' AND mid IN (SELECT mid FROM %s WHERE cid=%%s)" %
            CONFIG.typecho.table("relationships"),
            (cid,),
        )


def get_post_tags(cid: int, conn=None) -> List[Dict[str, Any]]:
    with get_conn(conn) as c:
        return select_all(
            c, "metas",
            "type='tag' AND mid IN (SELECT mid FROM %s WHERE cid=%%s)" %
            CONFIG.typecho.table("relationships"),
            (cid,),
        )
