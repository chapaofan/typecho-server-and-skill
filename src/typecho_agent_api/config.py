"""
集中加载配置。所有模块都从这里读配置，禁止直接读 os.environ。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

_logger = logging.getLogger(__name__)


def _find_env_file() -> Optional[Path]:
    """
    定位 .env，按优先级依次尝试：

    1. 环境变量 ENV_FILE 显式指定（最保险，宝塔里推荐直接设这个）
    2. 源码布局（src/typecho_agent_api/config.py 往上两级 = 项目根）
    3. 包被安装进 site-packages 时（pip install . 而非 -e .）再往上找
    4. 当前工作目录

    不能只靠 load_dotenv() 默认的向上查找：gunicorn / supervisor / 宝塔启动
    时的工作目录不确定，一旦找错文件，API_KEYS 和数据库密码就是空的，
    表现是「进程还在跑但所有接口 503」，非常难排查。
    """
    explicit = os.getenv("ENV_FILE")
    if explicit:
        return Path(explicit)

    here = Path(__file__).resolve()
    candidates = []
    for up in (2, 3):
        if len(here.parents) > up:
            candidates.append(here.parents[up] / ".env")
    candidates.append(Path.cwd() / ".env")

    for c in candidates:
        if c.is_file():
            return c
    return None


_ENV_FILE = _find_env_file()
if _ENV_FILE:
    load_dotenv(_ENV_FILE, override=False)
else:
    _logger.warning(
        "no .env found (looked next to the package and in cwd). "
        "Set ENV_FILE=/abs/path/.env if your .env lives elsewhere."
    )


def _get(name: str, default: str = "") -> str:
    val = os.getenv(name)
    return val if val is not None and val != "" else default


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _get_list(name: str, default: List[str] = None) -> List[str]:
    raw = _get(name)
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class TypechoConfig:
    host: str = "localhost"
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = "typecho"
    charset: str = "utf8mb4"
    table_prefix: str = "typecho_"
    default_author_id: int = 1
    default_category_id: int = 1
    # 连接/读写超时（秒）。不设的话 MySQL 或网络一卡住，工作线程会永久挂起，
    # 线程耗尽后整个服务「假死」（进程还在，但不再响应任何请求）。
    connect_timeout: int = 5
    read_timeout: int = 15
    write_timeout: int = 15
    # 连接池上限。Flask/gunicorn 每个 worker 一份池，注意 上限 * worker数 * 进程数
    # 不能超过 MySQL 的 max_connections。
    pool_max_connections: int = 10
    # 池满时最多等多久（秒）。DBUtils 的 blocking=True 是**无限等待**，
    # 一旦有连接泄漏就会把整个服务拖成无响应，所以这里自己实现带超时的等待。
    pool_wait_timeout: float = 5.0

    def table(self, name: str) -> str:
        """返回带前缀的表名，例如 table('contents') -> 'typecho_contents'。"""
        return f"{self.table_prefix}{name}"


@dataclass
class CosConfig:
    secret_id: str = ""
    secret_key: str = ""
    region: str = "ap-guangzhou"
    bucket: str = ""
    scheme: str = "https"
    key_prefix: str = "blog/"
    cdn_domain: str = ""
    # COS HTTP 超时（秒）。SDK 不设就是无超时，网络抖动时会把线程永久卡住。
    timeout: int = 20


@dataclass
class AppConfig:
    api_keys: List[str] = field(default_factory=list)
    allowed_origins: List[str] = field(default_factory=list)
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    http_debug: bool = False
    # 服务端是否允许 upload_image_from_file 从服务端文件系统读文件上传。
    # 默认 false：产线应该用 upload_image_from_base64；本地开发时改为 true。
    allow_local_upload: bool = False
    # 单个请求体 / 单张上传图片的最大字节数。不做限制的话一个几十 MB 的请求
    # 就能把内存打满（Flask 默认不限制请求体大小）。
    max_upload_bytes: int = 20 * 1024 * 1024
    typecho: TypechoConfig = field(default_factory=TypechoConfig)
    cos: CosConfig = field(default_factory=CosConfig)


def load_config() -> AppConfig:
    return AppConfig(
        api_keys=_get_list("API_KEYS", []),
        allowed_origins=_get_list("ALLOWED_ORIGINS", ["*"]),
        http_host=_get("HTTP_HOST", "0.0.0.0"),
        http_port=_get_int("HTTP_PORT", 8000),
        http_debug=_get("HTTP_DEBUG", "false").lower() in ("1", "true", "yes"),
        allow_local_upload=_get("ALLOW_LOCAL_UPLOAD", "false").lower() in ("1", "true", "yes"),
        max_upload_bytes=_get_int("MAX_UPLOAD_MB", 20) * 1024 * 1024,
        typecho=TypechoConfig(
            host=_get("TYPECHO_DB_HOST", "localhost"),
            port=_get_int("TYPECHO_DB_PORT", 3306),
            user=_get("TYPECHO_DB_USER"),
            password=_get("TYPECHO_DB_PASSWORD"),
            database=_get("TYPECHO_DB_NAME", "typecho"),
            charset=_get("TYPECHO_DB_CHARSET", "utf8mb4"),
            table_prefix=_get("TYPECHO_TABLE_PREFIX", "typecho_"),
            default_author_id=_get_int("TYPECHO_DEFAULT_AUTHOR_ID", 1),
            default_category_id=_get_int("TYPECHO_DEFAULT_CATEGORY_ID", 1),
            connect_timeout=_get_int("TYPECHO_DB_CONNECT_TIMEOUT", 5),
            read_timeout=_get_int("TYPECHO_DB_READ_TIMEOUT", 15),
            write_timeout=_get_int("TYPECHO_DB_WRITE_TIMEOUT", 15),
            pool_max_connections=_get_int("TYPECHO_DB_POOL_MAX", 10),
            pool_wait_timeout=_get_float("TYPECHO_DB_POOL_WAIT_TIMEOUT", 5.0),
        ),
        cos=CosConfig(
            secret_id=_get("COS_SECRET_ID"),
            secret_key=_get("COS_SECRET_KEY"),
            region=_get("COS_REGION", "ap-guangzhou"),
            bucket=_get("COS_BUCKET"),
            scheme=_get("COS_SCHEME", "https"),
            key_prefix=_get("COS_KEY_PREFIX", "blog/"),
            cdn_domain=_get("COS_CDN_DOMAIN"),
            timeout=_get_int("COS_TIMEOUT", 20),
        ),
    )


CONFIG: AppConfig = load_config()
