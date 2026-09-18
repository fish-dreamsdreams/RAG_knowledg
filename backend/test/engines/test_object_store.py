"""对象存储 key 拼接与文件名收敛的单测（tasklist 17.1，纯函数，不连 MinIO）。

安全性靠两条断言守住：外来源文件名只取最后一段，且拼出的 key 必然落在单元前缀内。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.engines.storage import object_store

UNIT_ID = uuid4()
UNIT_ROOT = f"{object_store.UNIT_PREFIX}/{UNIT_ID}/"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a.png", "a.png"),
        ("images/a.png", "a.png"),
        ("..\\..\\a.png", "a.png"),
        ("./a.png", "a.png"),
        ("  spaced .png ", "spaced .png"),
        ("制度.pdf", "制度.pdf"),
    ],
)
def test_safe_name_keeps_only_last_segment(raw: str, expected: str) -> None:
    assert object_store.safe_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".",
        "..",
        "CON",
        "con.txt",
        "LPT1.png",
        "nul",
        "a:b.png",
        "a*b.png",
        "a\x01b.png",
        "x" * (object_store.MAX_NAME_LENGTH + 1),
    ],
)
def test_safe_name_rejects_unsafe(raw: str) -> None:
    assert object_store.safe_name(raw) is None


def test_keys_stay_inside_unit_prefix() -> None:
    """即使文件名带穿越片段，key 也只会落在本单元固定目录下的单段文件名里。"""

    cases = [
        (object_store.source_key(UNIT_ID, "../../etc/passwd"), object_store.SOURCE_DIR),
        (object_store.asset_key(UNIT_ID, "..\\..\\secret.png"), object_store.ASSETS_DIR),
        (object_store.parsed_key(UNIT_ID, "../evil"), object_store.PARSED_DIR),
    ]
    for key, directory in cases:
        assert key is not None
        root = f"{UNIT_ROOT}{directory}/"
        assert key.startswith(root), key
        # 目录之后只剩一段，不会出现二级路径
        assert "/" not in key.removeprefix(root), key


def test_unusable_filename_yields_no_key() -> None:
    assert object_store.source_key(UNIT_ID, "..") is None
    assert object_store.asset_key(UNIT_ID, "CON.png") is None
    assert object_store.parsed_key(UNIT_ID, "..") is None


def test_media_type_whitelist_excludes_svg() -> None:
    assert object_store.media_type_for("a.PNG") == "image/png"
    assert object_store.media_type_for("a.jpeg") == "image/jpeg"
    assert object_store.media_type_for("a.webp") == "image/webp"
    for name in ("a.svg", "a.pdf", "a.exe", ""):
        assert object_store.media_type_for(name) is None


def test_assets_prefix_is_deletable_prefix() -> None:
    """图片前缀以 `/` 结尾，可直接喂给 `delete_prefix`，不会误删同类前缀。"""

    assert object_store.assets_prefix(UNIT_ID) == f"{UNIT_ROOT}{object_store.ASSETS_DIR}/"
