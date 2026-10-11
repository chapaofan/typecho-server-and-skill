"""
Typecho 文章（contents）的增删改查。

字段对照（typecho_contents）：
- cid            INT  PRIMARY
- title          VARCHAR(200)
- slug           VARCHAR(200)   -- URL 缩略名
- created        INT            -- 发布时间（unix ts）
- modified       INT            -- 修改时间
- text           LONGTEXT       -- 正文（HTML 或 markdown）
- order          INT
- authorId       INT            -- typecho_users.uid
- template       VARCHAR(32)
- type           VARCHAR(16)    -- 'post' / 'page' / 'post_draft' / 'attachment'
- status         VARCHAR(16)    -- 'publish' / 'hidden' / 'private' / 'waiting'
- password       VARCHAR(32)
- commentsNum    INT
- allowComment   CHAR(1)        -- '0' / '1'
- allowPing      CHAR(1)
- allowFeed      CHAR(1)
- parent         INT

visibility 取值：'publish'(公开) / 'hidden'(隐藏) / 'password'(密码) / 'private'(私密) / 'waiting'(待审核)
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator

from .config import CONFIG
from .metas import (
    _slugify,
    get_post_categories,
    get_post_tags,
    set_post_categories,
    set_post_tags,
)
from .typecho_db import (
    count,
    delete,
    get_conn,
    insert,
    now_ts,
    select_all,
    select_one,
    update,
)

logger = logging.getLogger(__name__)

# ---------- 状态常量 ----------
VALID_VISIBILITY = ("publish", "hidden", "password", "private", "waiting")
VALID_STATUS = ("publish", "hidden", "private", "waiting")  # 不含 password，password 仅是 visibility 的一种

# ---------- Pydantic 模型（用于接收 Agent 参数）----------


class PostCreate(BaseModel):
    """新增文章的参数。"""

    title: str = Field(..., min_length=1, max_length=200, description="文章标题")
    content: str = Field(..., description="正文（HTML 或 markdown）")
    slug: Optional[str] = Field(default=None, max_length=200, description="URL 缩略名，留空自动生成")
    markdown: bool = Field(default=False, description="是否 markdown 内容；若是会在正文前加 <!--markdown--> 标记")
    status: str = Field(
        default="publish",
        description=f"状态，可选：{','.join(VALID_VISIBILITY)}",
    )
    password: Optional[str] = Field(default=None, max_length=32, description="visibility=password 时必填")
    created_at: Optional[int] = Field(
        default=None, description="发布时间（unix 秒），不传则为当前时间"
    )
    tags: List[str] = Field(default_factory=list, description="标签列表，按英文逗号分隔的字符串也可")
    category_ids: List[int] = Field(default_factory=list, description="分类 mid 列表")
    allow_comment: bool = Field(default=True, description="允许评论")
    allow_ping: bool = Field(default=True, description="允许引用")
    allow_feed: bool = Field(default=True, description="允许聚合")
    author_id: Optional[int] = Field(default=None, description="作者 uid，不传则用 .env 中的默认值")
    fields: Dict[str, Union[str, int, float]] = Field(default_factory=dict, description="自定义字段")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in VALID_VISIBILITY:
            raise ValueError(f"status must be one of {VALID_VISIBILITY}")
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def _split_tags(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [t.strip() for t in re.split(r"[,,，]", v) if t.strip()]
        if isinstance(v, list):
            return [str(t).strip() for t in v if str(t).strip()]
        return v


class PostUpdate(BaseModel):
    """编辑文章的参数。所有字段都可选，未传则保留原值。"""

    title: Optional[str] = Field(default=None, max_length=200)
    content: Optional[str] = Field(default=None)
    slug: Optional[str] = Field(default=None, max_length=200)
    markdown: Optional[bool] = Field(default=None)
    status: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None, max_length=32)
    created_at: Optional[int] = Field(default=None)
    tags: Optional[List[str]] = Field(default=None)
    category_ids: Optional[List[int]] = Field(default=None)
    allow_comment: Optional[bool] = Field(default=None)
    allow_ping: Optional[bool] = Field(default=None)
    allow_feed: Optional[bool] = Field(default=None)
    fields: Optional[Dict[str, Union[str, int, float]]] = Field(default=None)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v):
        if v is None:
            return v
        if v not in VALID_VISIBILITY:
            raise ValueError(f"status must be one of {VALID_VISIBILITY}")
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def _split_tags(cls, v):
        if v is None or isinstance(v, list):
            return v
        if isinstance(v, str):
            return [t.strip() for t in re.split(r"[,,，]", v) if t.strip()]
        return v


# ---------- 内部工具 ----------

def _visibility_to_status(visibility: str, password: Optional[str]) -> tuple[str, str]:
    """
    把 visibility + password 转换成 (status, password) 写入数据库。
    复刻 Typecho EditTrait::checkStatus 的逻辑。
    """
    if visibility == "password":
        return "publish", (password or "")
    return visibility, ""


def _make_slug(conn, p: PostCreate) -> str:
    """
    确定 slug：没传就按标题生成，重名则补 -2/-3 后缀。

    （原来这里直接写空串，和「留空自动生成」的说明对不上，
    前台会拿到一堆 slug 为空、只能靠 cid 访问的文章。）
    """
    if p.slug:
        return p.slug
    base = _slugify(p.title) or f"post-{now_ts()}"
    slug, i = base, 2
    while select_one(conn, "contents", "slug=%s AND type='post'", (slug,)):
        slug = f"{base}-{i}"
        i += 1
    return slug


def _build_content_row(p: PostCreate, slug: str) -> Dict[str, Any]:
    status, password = _visibility_to_status(p.status, p.password)
    text = p.content
    if p.markdown:
        text = "<!--markdown-->" + text
    return {
        "title": p.title,
        "slug": slug,
        "created": p.created_at if p.created_at is not None else now_ts(),
        "modified": now_ts(),
        "text": text,
        "order": 0,
        "authorId": p.author_id or CONFIG.typecho.default_author_id,
        "template": None,
        "type": "post",
        "status": status,
        "password": password,
        "commentsNum": 0,
        "allowComment": "1" if p.allow_comment else "0",
        "allowPing": "1" if p.allow_ping else "0",
        "allowFeed": "1" if p.allow_feed else "0",
        "parent": 0,
    }


def _apply_fields(conn, cid: int, fields: Dict[str, Union[str, int, float]]) -> None:
    """
    写入 typecho_fields。type 自动推断。
    - 整数 -> int
    - 浮点 -> float
    - 其它 -> str
    """
    import json
    for name, value in fields.items():
        if not re.match(r"^[_a-zA-Z][_a-zA-Z0-9]*$", name):
            logger.warning("skip invalid field name: %s", name)
            continue
        if isinstance(value, bool):
            ttype, v = "int", int(value)
        elif isinstance(value, int):
            ttype, v = "int", value
        elif isinstance(value, float):
            ttype, v = "float", value
        elif isinstance(value, (dict, list)):
            ttype, v = "str", json.dumps(value, ensure_ascii=False)
        else:
            ttype, v = "str", str(value)

        existing = select_one(conn, "fields", "cid=%s AND name=%s", (cid, name))
        rows = {
            "type": ttype,
            "str_value": v if ttype == "str" else None,
            "int_value": int(v) if ttype == "int" else 0,
            "float_value": float(v) if ttype == "float" else 0.0,
        }
        if existing:
            update(conn, "fields", rows, "cid=%s AND name=%s", (cid, name))
        else:
            rows["cid"] = cid
            rows["name"] = name
            insert(conn, "fields", rows)


def _select_post(cid: int, conn=None) -> Optional[Dict[str, Any]]:
    with get_conn(conn) as c:
        row = select_one(c, "contents", "cid=%s AND type='post'", (cid,))
        if not row:
            return None
        row["categories"] = get_post_categories(cid, conn=c)
        row["tags"] = get_post_tags(cid, conn=c)
        # 自定义字段
        fields = select_all(c, "fields", "cid=%s", (cid,))
        row["custom_fields"] = {f["name"]: f for f in fields}
        return row


# ---------- 公共 API ----------


def create_post(p: PostCreate) -> Dict[str, Any]:
    """新增一篇文章，返回包含 cid 的字典。"""
    # 整篇文章（主表 + 分类 + 标签 + 自定义字段）走同一个连接、同一个事务：
    # 原来每一段各开一个连接各自提交，中途失败会留下「文章建了但没分类」的脏数据，
    # 而且一次请求要占 3 个连接，并发稍高就把池子吃光。
    with get_conn() as conn:
        slug = _make_slug(conn, p)
        cid = insert(conn, "contents", _build_content_row(p, slug))
        # 分类
        if not p.category_ids:
            p.category_ids = [CONFIG.typecho.default_category_id]
        if p.category_ids:
            set_post_categories(cid, p.category_ids, conn=conn)
        # 标签
        if p.tags:
            set_post_tags(cid, p.tags, conn=conn)
        # 自定义字段
        if p.fields:
            _apply_fields(conn, cid, p.fields)
    return get_post(cid)


def update_post(cid: int, p: PostUpdate) -> Dict[str, Any]:
    """更新一篇文章。"""
    with get_conn() as conn:
        old = select_one(conn, "contents", "cid=%s AND type='post'", (cid,))
        if not old:
            raise ValueError(f"post not found: cid={cid}")

        rows: Dict[str, Any] = {"modified": now_ts()}
        if p.title is not None:
            rows["title"] = p.title
        if p.slug is not None:
            rows["slug"] = p.slug
        if p.content is not None:
            text = p.content
            if p.markdown is True:
                text = "<!--markdown-->" + text
            elif p.markdown is False and old.get("text", "").startswith("<!--markdown-->"):
                # 显式关闭 markdown，保留原文
                pass
            rows["text"] = text
        if p.created_at is not None:
            rows["created"] = p.created_at
        if p.status is not None:
            status, password = _visibility_to_status(p.status, p.password)
            rows["status"] = status
            rows["password"] = password
        if p.password is not None and p.status is None:
            rows["password"] = p.password
        if p.allow_comment is not None:
            rows["allowComment"] = "1" if p.allow_comment else "0"
        if p.allow_ping is not None:
            rows["allowPing"] = "1" if p.allow_ping else "0"
        if p.allow_feed is not None:
            rows["allowFeed"] = "1" if p.allow_feed else "0"

        update(conn, "contents", rows, "cid=%s", (cid,))

        if p.category_ids is not None:
            ids = p.category_ids if p.category_ids else [CONFIG.typecho.default_category_id]
            set_post_categories(cid, ids, conn=conn)
        if p.tags is not None:
            set_post_tags(cid, p.tags, conn=conn)
        if p.fields is not None:
            _apply_fields(conn, cid, p.fields)
    return get_post(cid)


def delete_post(cid: int) -> bool:
    """
    删除一篇文章。同步删除其评论、关联、字段。
    模仿 typecho EditTrait::deletePost 的行为。
    """
    with get_conn() as conn:
        old = select_one(conn, "contents", "cid=%s AND type='post'", (cid,))
        if not old:
            return False

        was_published = old["status"] == "publish"

        # 评论
        delete(conn, "comments", "cid=%s", (cid,))

        # 关联（按 typecho 的实现，删 cid 对应的 relationships，分类/标签计数会减 1）
        rels = select_all(conn, "relationships", "cid=%s", (cid,))
        for r in rels:
            meta = select_one(conn, "metas", "mid=%s", (r["mid"],))
            if meta and was_published and meta["type"] in ("category", "tag"):
                # count - 1
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE `{CONFIG.typecho.table('metas')}` "
                        f"SET `count`=GREATEST(`count`-1,0) WHERE mid=%s",
                        (r["mid"],),
                    )
        delete(conn, "relationships", "cid=%s", (cid,))

        # 附件：取消关联
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE `{CONFIG.typecho.table('contents')}` "
                f"SET parent=0, status='publish' WHERE parent=%s AND type='attachment'",
                (cid,),
            )

        # 草稿：Typecho 存的是 type='post_draft'（不是 'revision'），
        # 写错的话草稿永远清不掉，会一直堆在 contents 表里。
        draft = select_one(conn, "contents",
                           "parent=%s AND type='post_draft'", (cid,))
        if draft:
            delete(conn, "contents", "cid=%s", (draft["cid"],))
            delete(conn, "fields", "cid=%s", (draft["cid"],))

        # 自定义字段
        delete(conn, "fields", "cid=%s", (cid,))

        # 最后删主表
        affected = delete(conn, "contents", "cid=%s", (cid,))
        return affected > 0


def get_post(cid: int) -> Dict[str, Any]:
    """获取一篇文章的完整信息。"""
    row = _select_post(cid)
    if not row:
        raise ValueError(f"post not found: cid={cid}")
    return _serialize_post(row)


def list_posts(
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    order_by: str = "created DESC",
) -> Dict[str, Any]:
    """分页列出文章。"""
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    where_parts = ["type='post'"]
    args: List[Any] = []
    if status:
        where_parts.append("status=%s")
        args.append(status)
    if keyword:
        where_parts.append("(title LIKE %s OR text LIKE %s)")
        args.extend([f"%{keyword}%", f"%{keyword}%"])
    where = " AND ".join(where_parts)
    # 只取列表要用的列：text 是 LONGTEXT，SELECT * 会把每篇正文都拉回来。
    list_cols = "cid,title,slug,status,type,created,modified,authorId,commentsNum"
    with get_conn() as conn:
        total = count(conn, "contents", where, args)
        rows = select_all(conn, "contents", where, args, columns=list_cols,
                          order_by=order_by, limit=page_size,
                          offset=(page - 1) * page_size)
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "items": [_serialize_post_simple(r) for r in rows],
        }


def find_post_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = select_one(conn, "contents", "slug=%s AND type='post'", (slug,))
        if not row:
            return None
        row["categories"] = get_post_categories(row["cid"], conn=conn)
        row["tags"] = get_post_tags(row["cid"], conn=conn)
        return _serialize_post(row)


# ---------- 序列化 ----------

def _serialize_post_simple(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cid": row["cid"],
        "title": row["title"],
        "slug": row.get("slug"),
        "status": row.get("status"),
        "type": row.get("type"),
        "created": row.get("created"),
        "modified": row.get("modified"),
        "author_id": row.get("authorId"),
        "comments_num": row.get("commentsNum"),
    }


def _serialize_post(row: Dict[str, Any]) -> Dict[str, Any]:
    base = _serialize_post_simple(row)
    base["content"] = row.get("text", "")
    base["password"] = row.get("password")
    base["allow_comment"] = (row.get("allowComment") == "1")
    base["allow_ping"] = (row.get("allowPing") == "1")
    base["allow_feed"] = (row.get("allowFeed") == "1")
    base["categories"] = [
        {"mid": c["mid"], "name": c["name"], "slug": c["slug"]} for c in row.get("categories", [])
    ]
    base["tags"] = [
        {"mid": t["mid"], "name": t["name"], "slug": t["slug"]} for t in row.get("tags", [])
    ]
    base["custom_fields"] = {
        name: f.get("str_value") or f.get("int_value") or f.get("float_value")
        for name, f in (row.get("custom_fields") or {}).items()
    }
    return base
