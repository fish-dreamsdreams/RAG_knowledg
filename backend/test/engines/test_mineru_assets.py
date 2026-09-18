"""图片解出与校验的单测（tasklist 17.2）：纯逻辑，不连 MinIO。

覆盖路径穿越、伪造扩展名、svg 排除、三类上限、重名与损坏 ZIP。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.engines.mineru.assets import extract_assets

# 各类型的合法魔数头
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
GIF = b"GIF89a" + b"0" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"0" * 32
BMP = b"BM" + b"0" * 32


def _zip(tmp_path: Path, entries: dict[str, bytes]) -> Path:
    path = tmp_path / "result.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def test_extracts_supported_images_in_order(tmp_path: Path) -> None:
    path = _zip(
        tmp_path, {"images/b.png": PNG, "images/a.jpg": JPEG, "full.md": b"# x"}
    )

    assets = extract_assets(path)

    assert [asset.name for asset in assets] == ["a.jpg", "b.png"]
    assert [asset.ordinal for asset in assets] == [0, 1]
    assert assets[0].media_type == "image/jpeg"
    assert assets[0].content == JPEG


def test_all_whitelisted_types_are_accepted(tmp_path: Path) -> None:
    path = _zip(
        tmp_path,
        {"a.png": PNG, "b.jpg": JPEG, "c.gif": GIF, "d.webp": WEBP, "e.bmp": BMP},
    )

    assert [asset.name for asset in extract_assets(path)] == [
        "a.png",
        "b.jpg",
        "c.gif",
        "d.webp",
        "e.bmp",
    ]


def test_svg_is_excluded(tmp_path: Path) -> None:
    path = _zip(tmp_path, {"images/x.svg": b"<svg onload=alert(1)></svg>"})

    assert extract_assets(path) == []


def test_fake_extension_is_rejected(tmp_path: Path) -> None:
    path = _zip(tmp_path, {"images/fake.png": b"<html>not an image</html>"})

    assert extract_assets(path) == []


def test_webp_requires_riff_container(tmp_path: Path) -> None:
    path = _zip(tmp_path, {"fake.webp": b"RIFF\x00\x00\x00\x00XXXX" + b"0" * 8})

    assert extract_assets(path) == []


def test_path_traversal_entry_is_neutralized(tmp_path: Path) -> None:
    path = _zip(tmp_path, {"../../etc/evil.png": PNG})

    assert [asset.name for asset in extract_assets(path)] == ["evil.png"]


def test_unusable_names_are_skipped(tmp_path: Path) -> None:
    path = _zip(tmp_path, {"..": PNG, "CON.png": PNG, "images/ok.png": PNG})

    assert [asset.name for asset in extract_assets(path)] == ["ok.png"]


def test_duplicate_names_get_suffix(tmp_path: Path) -> None:
    path = _zip(tmp_path, {"a/logo.png": PNG, "b/logo.png": PNG})

    assert [asset.name for asset in extract_assets(path)] == ["logo.png", "logo_2.png"]


def test_per_file_limit_skips_big_image(tmp_path: Path) -> None:
    path = _zip(tmp_path, {"big.png": PNG + b"0" * 100, "small.png": PNG})

    assert [asset.name for asset in extract_assets(path, max_file_bytes=64)] == [
        "small.png"
    ]


def test_total_limit_stops_later_images(tmp_path: Path) -> None:
    path = _zip(tmp_path, {"a.png": PNG, "b.png": PNG, "c.png": PNG})

    assert [
        asset.name
        for asset in extract_assets(path, max_total_bytes=len(PNG) + 1)
    ] == ["a.png"]


def test_file_count_limit(tmp_path: Path) -> None:
    path = _zip(tmp_path, {f"{index}.png": PNG for index in range(5)})

    assert len(extract_assets(path, max_files=2)) == 2


def test_broken_zip_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(b"not a zip at all")

    assert extract_assets(path) == []


def test_missing_zip_returns_empty(tmp_path: Path) -> None:
    assert extract_assets(tmp_path / "nope.zip") == []
