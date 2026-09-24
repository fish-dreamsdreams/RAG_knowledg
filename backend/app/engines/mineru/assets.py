"""MinerU 结果 ZIP 里的图片解出与校验（tasklist 17.2，TECH_SPEC §8.0）。

只做纯逻辑：从本地 ZIP 解出候选图片并做安全收敛，返回可直接写入对象存储的字节。
不接触对象存储与数据库（落盘与建行由 `services/ingest.py` 负责），因此可脱离 MinIO 单测。

安全约束：

- 扩展名白名单 `png/jpg/jpeg/webp/gif/bmp`，**排除 `svg`**（可含脚本，回吐有 XSS 面）。
- 校验**魔数**，不信任扩展名：伪造扩展名的条目直接丢弃。
- 条目名一律 basename 化并过 `object_store.safe_name`（拒绝 `../`、控制字符、Windows 保留名）。
- 单图、单单元总量、张数三类上限；超限跳过并告警，**不阻断导入**。
- 重名按出现顺序加序号后缀，保证对象 key 唯一。
- 不落盘解压：ZIP 条目名不可信，这里只在内存里读字节。
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.common.config import settings
from app.engines.storage import object_store

logger = logging.getLogger(__name__)

# 允许解出的图片类型（排除 svg）
SUPPORTED_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

# 扩展名 → 允许的魔数前缀；webp 是 RIFF 容器，单独判断
_MAGIC_PREFIXES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
}


@dataclass(frozen=True)
class ExtractedAsset:
    """一个可写入对象存储的图片。"""

    # 安全化后的单段文件名（重名已加序号）
    name: str
    # ZIP 内原始条目名（仅用于日志排查）
    source: str
    media_type: str
    content: bytes
    # 在文档里的出现顺序
    ordinal: int


def _matches_magic(suffix: str, content: bytes) -> bool:
    """魔数与扩展名是否一致。"""

    if suffix == ".webp":
        return (
            len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"
        )
    return content.startswith(_MAGIC_PREFIXES[suffix])


def _unique_name(name: str, used: set[str]) -> str:
    """重名时加序号后缀（`img_1.png` → `img_1_2.png`）。"""

    if name not in used:
        used.add(name)
        return name

    stem, dot, suffix = name.rpartition(".")
    base = stem if dot else name
    extension = f".{suffix}" if dot else ""
    index = 2
    while f"{base}_{index}{extension}" in used:
        index += 1
    unique = f"{base}_{index}{extension}"
    used.add(unique)
    return unique


def extract_assets(
    zip_path: Path,
    *,
    max_files: int | None = None,
    max_file_bytes: int | None = None,
    max_total_bytes: int | None = None,
) -> list[ExtractedAsset]:
    """解出 ZIP 内的图片；ZIP 不可读时返回空列表（调用方只告警，不阻断导入）。"""

    file_limit = settings.asset_max_files if max_files is None else max_files
    size_limit = (
        settings.asset_max_file_bytes if max_file_bytes is None else max_file_bytes
    )
    total_limit = (
        settings.asset_max_total_bytes if max_total_bytes is None else max_total_bytes
    )

    assets: list[ExtractedAsset] = []
    used: set[str] = set()
    total_bytes = 0
    skipped = 0

    try:
        with zipfile.ZipFile(zip_path) as archive:
            # 按条目名排序，保证同一份 ZIP 每次解出的顺序与 ordinal 一致
            entries = sorted(
                (info for info in archive.infolist() if not info.is_dir()),
                key=lambda info: info.filename,
            )
            for info in entries:
                if len(assets) >= file_limit:
                    logger.warning(
                        "图片张数超过上限 %s，其余跳过：%s", file_limit, zip_path.name
                    )
                    break

                raw_name = info.filename
                suffix = Path(raw_name).suffix.lower()
                media_type = SUPPORTED_IMAGE_TYPES.get(suffix)
                if media_type is None:
                    continue
                safe = object_store.safe_name(raw_name)
                if safe is None:
                    logger.warning("图片条目名不可用，跳过：%s", raw_name)
                    skipped += 1
                    continue
                if info.file_size > size_limit:
                    logger.warning("单图超过上限，跳过：%s", raw_name)
                    skipped += 1
                    continue

                try:
                    content = archive.read(info)
                except (zipfile.BadZipFile, RuntimeError, OSError):
                    # 单条目损坏只跳过它；RuntimeError 覆盖加密条目
                    logger.warning("图片条目读取失败，跳过：%s", raw_name, exc_info=True)
                    skipped += 1
                    continue

                if len(content) > size_limit:
                    logger.warning("单图超过上限，跳过：%s", raw_name)
                    skipped += 1
                    continue
                if not _matches_magic(suffix, content):
                    logger.warning("图片魔数与扩展名不符，跳过：%s", raw_name)
                    skipped += 1
                    continue
                if total_bytes + len(content) > total_limit:
                    logger.warning(
                        "单元图片总量超过上限，其余跳过：%s", zip_path.name
                    )
                    break

                total_bytes += len(content)
                assets.append(
                    ExtractedAsset(
                        name=_unique_name(safe, used),
                        source=raw_name,
                        media_type=media_type,
                        content=content,
                        ordinal=len(assets),
                    )
                )
    except (OSError, zipfile.BadZipFile):
        logger.warning("解析结果 ZIP 不可读，跳过图片解出：%s", zip_path, exc_info=True)
        return []

    if skipped:
        logger.info("图片解出：可用 %s 张，跳过 %s 张", len(assets), skipped)
    return assets
