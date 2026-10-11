"""
腾讯云 COS 图片上传。

主要方法：
- upload_file(local_path, key=None)              : 上传本地文件
- upload_bytes(data: bytes, key, content_type)   : 上传字节流
- upload_base64(b64: str, key, content_type)     : 上传 base64 编码（Agent 常用）
- delete(key)                                    : 删除对象
- build_url(key)                                 : 构造可访问的 URL
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from qcloud_cos import CosConfig, CosS3Client  # 来自 cos-python-sdk-v5
from qcloud_cos.cos_exception import CosServiceError

from .config import CONFIG

logger = logging.getLogger(__name__)

# 文件后缀 -> Content-Type
CONTENT_TYPE_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".pdf": "application/pdf", ".zip": "application/zip",
}

# 允许的图片后缀（Agent 上传图片时白名单）
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _client() -> CosS3Client:
    """
    每次新建 client。

    这里刻意不做全局单例：CosS3Client 内部是 requests.Session，
    而 Session **不是线程安全的**；Flask/gunicorn 多线程 worker 下共用会串请求。
    新建的代价只有一次 TCP/TLS 握手，真正要防的是下面这个 Timeout ——
    SDK 默认不设超时，网络一抖这个线程就永久卡死，几个请求就能把所有线程耗光。
    """
    cfg = CONFIG.cos
    if not cfg.secret_id or not cfg.secret_key or not cfg.bucket:
        raise RuntimeError("COS config is incomplete: SECRET_ID/SECRET_KEY/BUCKET required")
    cos_cfg = CosConfig(
        Region=cfg.region,
        SecretId=cfg.secret_id,
        SecretKey=cfg.secret_key,
        Scheme=cfg.scheme,
        Timeout=cfg.timeout,
    )
    return CosS3Client(cos_cfg)


def _detect_content_type(key: str, fallback: str = "application/octet-stream") -> str:
    ext = os.path.splitext(key)[1].lower()
    return CONTENT_TYPE_MAP.get(ext, fallback)


def _build_key(filename: str) -> str:
    """生成最终在 COS 上的 Key：<prefix>YYYY/MM/<uuid>__<filename>"""
    cfg = CONFIG.cos
    name = os.path.basename(filename).strip() or f"upload-{uuid.uuid4().hex}"
    # 去掉路径分隔符
    name = name.replace("/", "_").replace("\\", "_")
    # 把空格替换成下划线
    name = name.replace(" ", "_")
    # 中文/特殊字符保留即可，COS Key 支持 UTF-8
    today = datetime.now(timezone.utc)   # utcnow() 在 Python 3.12+ 已弃用
    return f"{cfg.key_prefix}{today.strftime('%Y/%m')}/{uuid.uuid4().hex[:8]}__{name}"


def _check_size(data_or_size) -> None:
    """上传大小上限。不设的话一个几百 MB 的请求就能把进程内存打满。"""
    size = data_or_size if isinstance(data_or_size, int) else len(data_or_size)
    limit = CONFIG.max_upload_bytes
    if size > limit:
        raise ValueError(f"file too large: {size} bytes > limit {limit} bytes")


def _public_url(key: str) -> str:
    """构造可访问的 URL。如果配置了 COS_CDN_DOMAIN 则用 CDN 域名。"""
    cfg = CONFIG.cos
    if cfg.cdn_domain:
        base = cfg.cdn_domain.rstrip("/")
        if not base.startswith("http"):
            base = f"{cfg.scheme}://{base}"
        return f"{base}/{key}"
    # 默认 COS 域名：<Bucket>-<AppId>.cos.<Region>.myqcloud.com/<key>
    return f"{cfg.scheme}://{cfg.bucket}.cos.{cfg.region}.myqcloud.com/{key}"


# ---------- 公开 API ----------


def upload_file(local_path: str, key: Optional[str] = None) -> dict:
    """
    上传本地文件到 COS。
    返回 {"key": ..., "url": ..., "size": ..., "etag": ...}

    ⚠️ 这个函数会让调用方读取服务端机器上的任意文件并上传到 COS。
    生产环境必须把 .env 里 ALLOW_LOCAL_UPLOAD 设为 false。
    任意 agent 调这个工具都相当于一次任意文件读取 + 外传 —— 哪怕只开 API Key 鉴权也不安全。
    本地开发自己玩的时候再开 true。
    """
    if not CONFIG.allow_local_upload:
        raise PermissionError(
            "upload_image_from_file is disabled. Set ALLOW_LOCAL_UPLOAD=true in .env to enable."
        )
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    _check_size(os.path.getsize(local_path))
    final_key = key or _build_key(os.path.basename(local_path))
    content_type = _detect_content_type(final_key)
    logger.info("COS upload: local=%s key=%s ct=%s", local_path, final_key, content_type)
    response = _client().put_object_from_local_file(
        Bucket=CONFIG.cos.bucket,
        LocalFilePath=local_path,
        Key=final_key,
        ContentType=content_type,
    )
    return {
        "key": final_key,
        "url": _public_url(final_key),
        "size": os.path.getsize(local_path),
        "etag": response.get("ETag", "").strip('"'),
        "content_type": content_type,
    }


def upload_bytes(data: bytes, filename: str, content_type: Optional[str] = None,
                 image_only: bool = True) -> dict:
    """
    上传内存中的字节流。
    - filename: 决定 key 的扩展名
    - image_only: True 时只允许图片类型
    """
    if image_only:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_IMAGE_EXT:
            raise ValueError(f"image_only=True but extension {ext} is not allowed. "
                             f"Allowed: {sorted(ALLOWED_IMAGE_EXT)}")
    _check_size(data)
    final_key = _build_key(filename)
    ct = content_type or _detect_content_type(final_key)
    response = _client().put_object(
        Bucket=CONFIG.cos.bucket,
        Key=final_key,
        Body=io.BytesIO(data),
        ContentType=ct,
        ContentLength=str(len(data)),
    )
    return {
        "key": final_key,
        "url": _public_url(final_key),
        "size": len(data),
        "etag": response.get("ETag", "").strip('"'),
        "content_type": ct,
    }


def upload_base64(b64: str, filename: str = "image.png",
                  content_type: Optional[str] = None, image_only: bool = True) -> dict:
    """
    上传 base64 编码的二进制。常用于 LLM 工具调用时直接拿到截图 / 生成图。
    b64 可以带也可以不带 "data:image/png;base64," 前缀。
    """
    if "," in b64 and b64.lstrip().startswith("data:"):
        head, b64 = b64.split(",", 1)
        if content_type is None and ";" in head:
            content_type = head[5:].split(";")[0]
    b64 = b64.strip()
    if not b64:
        raise ValueError("base64_data is empty")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"invalid base64: {e}") from e
    return upload_bytes(data, filename=filename, content_type=content_type, image_only=image_only)


def delete(key: str) -> bool:
    """
    删除 COS 对象。

    只允许删 COS_KEY_PREFIX 前缀下的 key —— 否则一个带 API Key 的调用方
    就能顺着这个接口把整个桶清空（包括跟博客无关的东西）。
    """
    prefix = CONFIG.cos.key_prefix
    if prefix and not key.startswith(prefix):
        raise ValueError(f"refuse to delete object outside COS_KEY_PREFIX: {key!r}")
    try:
        _client().delete_object(Bucket=CONFIG.cos.bucket, Key=key)
        return True
    except CosServiceError as e:
        logger.warning("COS delete failed: %s", e)
        return False


def build_url(key: str) -> str:
    return _public_url(key)
