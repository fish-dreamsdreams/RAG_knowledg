"""图片标记与图注的不可分切块（tasklist 17.3，P18）。

图注只有与图片标记落在同一块里才有意义：被拆开就会出现"图注块没图、图片块没词"，
命中图注时展示不出图，命中图片时又没有可检索的词。

后半部分测 `asset_names`：答案侧从命中的正文里回收图片文件名（tasklist 17.5）。
"""

from __future__ import annotations

from app.engines.chunking import Chunker, asset_names

chunker = Chunker()

IMAGE = "![](assets/img_1.jpg)"
CAPTION = "图注：图中为三级审批流程，依次为部门主管、财务复核、总经理签批。"


def children_of(markdown: str) -> list[str]:
    return [
        child.content
        for parent in chunker.split(markdown)
        for child in parent.children
    ]


def test_image_and_caption_are_never_separated() -> None:
    markdown = f"# 审批流程\n\n前文说明。\n\n{IMAGE}\n\n{CAPTION}\n\n后文说明。\n"

    children = children_of(markdown)

    with_image = [text for text in children if IMAGE in text]
    with_caption = [text for text in children if CAPTION in text]
    assert with_image, "用例必须真的切出含图片的子块"
    assert with_image == with_caption, children


def test_pair_survives_in_a_long_document() -> None:
    filler = "这一段用来把文档撑长，制造多个父块与子块边界。" * 40
    markdown = f"# 长文档\n\n{filler}\n\n{IMAGE}\n\n{CAPTION}\n\n{filler}\n"

    with_image = [text for text in children_of(markdown) if IMAGE in text]

    assert with_image
    assert all(CAPTION in text for text in with_image), with_image


def test_caption_without_image_is_not_special() -> None:
    """只有图注、没有图片标记时不是原子区间，应照常参与切块。"""

    markdown = f"# 说明\n\n{CAPTION}\n\n后文。\n"

    children = children_of(markdown)

    assert any(CAPTION in text for text in children)


def test_image_without_caption_is_not_special() -> None:
    """只解出图片、没有图注（视觉关闭或失败）时也不该被强行绑定。"""

    markdown = f"# 说明\n\n{IMAGE}\n\n后文说明文字。\n"

    children = children_of(markdown)

    assert any(IMAGE in text for text in children)


def test_plain_markdown_is_unaffected() -> None:
    markdown = "# 制度\n\n第一段说明。\n\n第二段说明。\n"

    parents = chunker.split(markdown)

    assert parents
    assert all(parent.content.strip() for parent in parents)


# ---------- asset_names：答案侧回收图片文件名 ----------


def test_asset_names_keeps_order_and_dedupes() -> None:
    markdown = f"说明。\n\n{IMAGE}\n\n{CAPTION}\n\n![第二张](assets/img_2.png)\n\n{IMAGE}\n"

    assert asset_names(markdown) == ["img_1.jpg", "img_2.png"]


def test_asset_names_accepts_dot_prefix() -> None:
    assert asset_names("![](./assets/img_9.webp)") == ["img_9.webp"]


def test_asset_names_ignores_external_urls() -> None:
    """手写文档里的外链图片不在本单元对象存储里，不能变成引用附图。"""

    markdown = (
        "![外链](https://cdn.example.com/assets/logo.png)\n\n"
        "![绝对路径](/static/assets/logo.png)\n\n"
        "![普通链接](https://example.com/a.png)\n"
    )

    assert asset_names(markdown) == []


def test_asset_names_ignores_nested_and_traversal_paths() -> None:
    """`assets/` 下面还有子目录或 `..` 的引用一律不算：本单元的 key 只有一层文件名。"""

    assert asset_names("![](assets/sub/img_1.png)") == []
    assert asset_names("![](assets/../source/secret.pdf)") == []


def test_asset_names_on_text_without_images() -> None:
    assert asset_names("# 制度\n\n纯文本说明。\n") == []
