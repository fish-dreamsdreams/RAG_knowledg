"""对象存储（MinIO）封装（tasklist 17.1，TECH_SPEC §8.0）。

单元资产在桶内按固定前缀分组，同一单元的三类对象互不混淆：

    kb/units/{unit_id}/source/{原文件名}      原件
    kb/units/{unit_id}/parsed/{stem}.zip      MinerU 原始结果
    kb/units/{unit_id}/assets/{name}          从结果 ZIP 解出的图片

约定：

- key 一律由本模块拼接，调用方只给 `unit_id` 与文件名；文件名先过 `safe_name()`
  （basename 化 + 拒绝控制字符、Windows 非法字符与保留名），最终 key 必然落在单元前缀内。
- 桶**私有**：禁止匿名读。前端不直连对象存储，图片走后端代理接口（TECH_SPEC §8.0）。
- `minio` SDK 是同步阻塞的，这里统一用 `asyncio.to_thread` 包装，避免阻塞事件循环
  （与 `engines/embed`、`milvus_store` 同一处理方式）。
- 首次使用时惰性建桶（幂等）：不要求部署脚本预先建桶，也不要求在启动期连上存储。
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote, urlparse
from uuid import UUID

from minio import Minio
from minio.error import S3Error

from app.common.config import settings

logger = logging.getLogger(__name__)

# 桶内固定前缀：同一单元的三类对象
UNIT_PREFIX = "kb/units"
SOURCE_DIR = "source"
PARSED_DIR = "parsed"
ASSETS_DIR = "assets"

# 单段 key 的长度上限（对象存储本身允许更长，这里只为防御异常文件名）
MAX_NAME_LENGTH = 200

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{index}" for prefix in ("com", "lpt") for index in range(1, 10)
}
_ILLEGAL_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

# 扩展名 → media_type（回吐图片时的 Content-Type 白名单来源）
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

_client: Minio | None = None
_bucket_ready = False
_lock = asyncio.Lock()


class ObjectStoreError(RuntimeError):
    """对象存储链路失败。message 面向日志与 `parse_error`，不含密钥。"""


def _endpoint_and_secure() -> tuple[str, bool]:
    """把 `MINIO_ENDPOINT` 收敛成 minio SDK 要的 `host:port` 与 secure 标志。

    SDK 不接受带 scheme 的 endpoint，而配置里写成完整 URL 更直观（与 MILVUS_URI 一致），
    所以在这里解析；`https` 或 `MINIO_SECURE=true` 都视为开启 TLS。
    """

    raw = settings.minio_endpoint.strip()
    parsed = urlparse(raw if "//" in raw else f"//{raw}")
    host = parsed.netloc or parsed.path
    if not host:
        raise ObjectStoreError("MINIO_ENDPOINT 未配置")
    return host, bool(settings.minio_secure or parsed.scheme == "https")


def get_client() -> Minio:
    """进程内单例客户端（不建连接池以外的状态，可安全跨线程复用）。"""

    global _client
    if _client is None:
        host, secure = _endpoint_and_secure()
        _client = Minio(
            host,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=secure,
        )
    return _client


def _ensure_bucket_sync() -> None:
    global _bucket_ready
    if _bucket_ready:
        return
    client = get_client()
    try:
        if not client.bucket_exists(settings.minio_bucket):
            client.make_bucket(settings.minio_bucket)
            logger.info("已创建对象存储桶 %s", settings.minio_bucket)
    except S3Error as exc:
        # 并发启动时可能被另一个进程抢先创建；这种冲突是成功语义
        if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise ObjectStoreError(f"对象存储桶不可用：{exc.code}") from exc
    _bucket_ready = True


async def ensure_bucket() -> None:
    """幂等确保桶存在；失败抛 `ObjectStoreError`。"""

    try:
        await asyncio.to_thread(_ensure_bucket_sync)
    except ObjectStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - 网络/鉴权等错误统一收敛
        raise ObjectStoreError(f"对象存储不可用：{type(exc).__name__}") from exc


def safe_name(name: str) -> str | None:
    """把外部来源的文件名收敛成安全的单段 key；不可用时返回 None。

    ZIP 内条目名不可信（可能带目录、`..`、控制字符），一律只取最后一段再校验。
    """

    candidate = name.replace("\\", "/").split("/")[-1].strip()
    if not candidate or candidate in {".", ".."}:
        return None
    if len(candidate) > MAX_NAME_LENGTH:
        return None
    if _ILLEGAL_CHARS.search(candidate):
        return None
    # Windows 保留名（含带扩展名的形式，如 `CON.txt`）在下载到本地时无法落盘
    if candidate.split(".")[0].lower() in _WINDOWS_RESERVED:
        return None
    return candidate


def media_type_for(name: str) -> str | None:
    """扩展名 → media_type；不在白名单内返回 None。"""

    lowered = name.lower()
    for suffix, media_type in MEDIA_TYPES.items():
        if lowered.endswith(suffix):
            return media_type
    return None


def unit_prefix(unit_id: UUID | str) -> str:
    return f"{UNIT_PREFIX}/{unit_id}"


def assets_prefix(unit_id: UUID | str) -> str:
    """该单元图片对象的前缀（带结尾 `/`，可直接喂给 `delete_prefix`）。"""

    return f"{unit_prefix(unit_id)}/{ASSETS_DIR}/"


def source_key(unit_id: UUID | str, filename: str) -> str | None:
    safe = safe_name(filename)
    return f"{unit_prefix(unit_id)}/{SOURCE_DIR}/{safe}" if safe else None


def parsed_key(unit_id: UUID | str, stem: str) -> str | None:
    """解析产物 key。先校验 stem 再拼 `.zip`，避免 `..` 这类 stem 被拼成合法名。"""

    safe = safe_name(stem)
    return f"{unit_prefix(unit_id)}/{PARSED_DIR}/{safe}.zip" if safe else None


# 图片的前端可见地址：与 `asset_key` 是同一张图的两个坐标——前者给浏览器，后者给桶
ASSET_PROXY_PATH = "/api/v1/knowledge-units/{unit_id}/assets/{name}"


def asset_proxy_path(unit_id: UUID | str, name: str) -> str:
    """图片的后端代理地址（相对路径，tasklist 17.5）。

    **刻意不拼 `?access_token=`**：那会把调用方的 JWT 写进引用数据，而引用会存进
    `chat_messages.citations`，也会留在浏览器历史与各级日志里；Token 一换，历史消息里的图
    还会集体失效。Token 由前端渲染时自己附加——接口同时接受查询参数（`<img>` 带不了
    Authorization 头），两种拼法都能取到图。
    """

    return ASSET_PROXY_PATH.format(unit_id=unit_id, name=quote(name))


def asset_key(unit_id: UUID | str, name: str) -> str | None:
    safe = safe_name(name)
    return f"{unit_prefix(unit_id)}/{ASSETS_DIR}/{safe}" if safe else None


async def put_bytes(key: str, data: bytes, *, content_type: str | None = None) -> None:
    """写入一个对象（覆盖语义，重导时幂等）。"""

    await ensure_bucket()
    client = get_client()
    try:
        await asyncio.to_thread(
            client.put_object,
            settings.minio_bucket,
            key,
            _BytesReader(data),
            len(data),
            content_type=content_type or "application/octet-stream",
        )
    except Exception as exc:  # noqa: BLE001 - 统一收敛为对象存储错误
        raise ObjectStoreError(f"写入对象失败：{type(exc).__name__}") from exc


async def get_bytes(key: str) -> bytes:
    """读取对象全部字节；不存在抛 `ObjectStoreError`。"""

    await ensure_bucket()
    client = get_client()
    response = None
    try:
        response = await asyncio.to_thread(
            client.get_object, settings.minio_bucket, key
        )
        return await asyncio.to_thread(response.read)
    except S3Error as exc:
        raise ObjectStoreError(f"读取对象失败：{exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ObjectStoreError(f"读取对象失败：{type(exc).__name__}") from exc
    finally:
        if response is not None:
            response.close()
            response.release_conn()


async def exists(key: str) -> bool:
    await ensure_bucket()
    client = get_client()
    try:
        await asyncio.to_thread(client.stat_object, settings.minio_bucket, key)
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject"}:
            return False
        raise ObjectStoreError(f"查询对象失败：{exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ObjectStoreError(f"查询对象失败：{type(exc).__name__}") from exc
    return True


async def list_keys(prefix: str) -> list[str]:
    """列出前缀下的对象 key（排序，便于排查与测试断言）。"""

    await ensure_bucket()
    client = get_client()

    def _list() -> list[str]:
        return sorted(
            item.object_name
            for item in client.list_objects(
                settings.minio_bucket, prefix=prefix, recursive=True
            )
        )

    try:
        return await asyncio.to_thread(_list)
    except Exception as exc:  # noqa: BLE001
        raise ObjectStoreError(f"列举对象失败：{type(exc).__name__}") from exc


async def delete(key: str) -> None:
    """删除单个对象；对象不存在视为成功（幂等）。"""

    await ensure_bucket()
    client = get_client()
    try:
        await asyncio.to_thread(client.remove_object, settings.minio_bucket, key)
    except Exception as exc:  # noqa: BLE001
        raise ObjectStoreError(f"删除对象失败：{type(exc).__name__}") from exc


async def delete_prefix(prefix: str) -> int:
    """删除该前缀下的全部对象，返回删除数量；无对象时返回 0（幂等）。"""

    await ensure_bucket()
    client = get_client()

    def _remove_all() -> int:
        removed = 0
        for item in client.list_objects(
            settings.minio_bucket, prefix=prefix, recursive=True
        ):
            client.remove_object(settings.minio_bucket, item.object_name)
            removed += 1
        return removed

    try:
        return await asyncio.to_thread(_remove_all)
    except Exception as exc:  # noqa: BLE001
        raise ObjectStoreError(f"清理对象失败：{type(exc).__name__}") from exc


async def delete_unit_prefix(unit_id: UUID | str) -> int:
    """删除该单元下的全部对象（原件 / 解析产物 / 图片，P10）。"""

    return await delete_prefix(f"{unit_prefix(unit_id)}/")


class _BytesReader:
    """给 `put_object` 用的最小可读流：minio SDK 只要求 `.read(size)`。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._offset :]
            self._offset = len(self._data)
        else:
            chunk = self._data[self._offset : self._offset + size]
            self._offset += len(chunk)
        return chunk

    def tell(self) -> int:
        return self._offset
