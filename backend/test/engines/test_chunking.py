"""切块属性测试（tasklist 5.5）。

覆盖：子块长度不超上限、任意句子都能在某个子块中完整出现（覆盖率）、
序号连续、确定性（同样的输入必须切出同样的块）。
"""

import random

import pytest

from app.engines.chunking import (
    CHILD_MAX_CHARS,
    CHILD_TARGET_CHARS,
    PARENT_MAX_CHARS,
    Chunker,
    content_hash,
)

chunker = Chunker()

BOUNDARIES = "。！？；"


def sentences_of(text: str) -> list[str]:
    """测试自带的句子抽取（与实现独立），用于校验覆盖率。"""

    found: list[str] = []
    current = ""
    for char in text:
        current += char
        if char in BOUNDARIES or char == "\n":
            stripped = current.strip()
            if stripped:
                found.append(stripped)
            current = ""
    if current.strip():
        found.append(current.strip())
    return found


def make_markdown(seed: int, sections: int = 4, paragraphs: int = 6) -> str:
    """生成确定性 Markdown（不引入 hypothesis 依赖）。"""

    rng = random.Random(seed)
    words = ["报销", "差旅", "审批", "上限", "流程", "部门", "制度", "额度", "申请", "凭证"]
    lines: list[str] = []
    for section in range(sections):
        lines.append(f"## 第 {section + 1} 节 制度说明")
        lines.append("")
        for _ in range(paragraphs):
            sentence_count = rng.randint(1, 4)
            paragraph = "".join(
                "".join(rng.choice(words) for _ in range(rng.randint(3, 12))) + "。"
                for _ in range(sentence_count)
            )
            lines.append(paragraph)
            lines.append("")
    return "\n".join(lines)


# --- 基本行为 -------------------------------------------------------------


@pytest.mark.parametrize("markdown", ["", "   ", "\n\n", "\t"])
def test_blank_input_yields_no_chunks(markdown: str) -> None:
    assert chunker.split(markdown) == []


def test_single_short_paragraph_yields_one_parent_one_child() -> None:
    parents = chunker.split("这是一条很短的制度说明。")

    assert len(parents) == 1
    assert parents[0].ordinal == 0
    assert len(parents[0].children) == 1
    assert parents[0].children[0].content == "这是一条很短的制度说明。"
    assert parents[0].children[0].ordinal == 0


def test_char_count_and_hash_match_content() -> None:
    parents = chunker.split(make_markdown(1))

    for parent in parents:
        assert parent.char_count == len(parent.content)
        assert parent.content_hash == content_hash(parent.content)
        for child in parent.children:
            assert child.char_count == len(child.content)
            assert child.content_hash == content_hash(child.content)


def test_chunking_is_deterministic() -> None:
    markdown = make_markdown(2)

    first = chunker.split(markdown)
    second = chunker.split(markdown)

    assert first == second


def test_ordinals_are_contiguous() -> None:
    parents = chunker.split(make_markdown(3))

    assert [parent.ordinal for parent in parents] == list(range(len(parents)))
    for parent in parents:
        assert [child.ordinal for child in parent.children] == list(
            range(len(parent.children))
        )


# --- 属性：长度与覆盖 -----------------------------------------------------


@pytest.mark.parametrize("seed", range(1, 9))
def test_child_length_never_exceeds_limit(seed: int) -> None:
    parents = chunker.split(make_markdown(seed))

    assert parents, "生成的数据不应切出空结果"
    for parent in parents:
        for child in parent.children:
            assert child.char_count <= CHILD_MAX_CHARS
            assert len(child.content) <= CHILD_MAX_CHARS


@pytest.mark.parametrize("seed", range(1, 9))
def test_parent_length_never_exceeds_limit(seed: int) -> None:
    parents = chunker.split(make_markdown(seed))

    for parent in parents:
        assert parent.char_count <= PARENT_MAX_CHARS


@pytest.mark.parametrize("seed", range(1, 9))
def test_every_sentence_survives_in_some_child(seed: int) -> None:
    """覆盖率：原文任意句子（不超子上限）必须完整出现在某个子块里。"""

    markdown = make_markdown(seed)
    parents = chunker.split(markdown)
    children = [child.content for parent in parents for child in parent.children]

    checked = 0
    for sentence in sentences_of(markdown):
        if len(sentence) > CHILD_TARGET_CHARS:
            continue  # 超长句按约定在上限处硬切，不要求整句保留
        if sentence.startswith("#"):
            continue  # 标题可能因重叠落在父块开头之外
        checked += 1
        assert any(sentence in child for child in children), f"句子丢失: {sentence[:30]}"
    assert checked > 20, "样本太小，覆盖断言没有意义"


def test_children_are_contained_in_their_parent() -> None:
    """子块来自父块切片：内容必须是父块子串（用于回溯父块）。"""

    for parent in chunker.split(make_markdown(4)):
        for child in parent.children:
            assert child.content in parent.content


def test_long_document_is_split_into_many_parents() -> None:
    markdown = make_markdown(5, sections=8, paragraphs=15)

    parents = chunker.split(markdown)

    # 每块不超上限 → 块数至少是「总长度 / 上限」（父块有重叠，实际只会更多）
    assert len(parents) >= len(markdown) // PARENT_MAX_CHARS
    assert len(parents) >= 3
    assert sum(len(parent.children) for parent in parents) >= len(parents)


def test_overlapping_parents_share_text() -> None:
    """相邻父块应有重叠，避免边界语义被切断。"""

    parents = chunker.split(make_markdown(6, sections=8, paragraphs=15))
    assert len(parents) >= 3

    overlaps = 0
    for previous, current in zip(parents, parents[1:]):
        tail = previous.content[-30:]
        if tail and tail in current.content:
            overlaps += 1
    assert overlaps >= 1, "父块之间没有任何重叠"


# --- 边界情形 -------------------------------------------------------------


def test_very_long_line_without_punctuation_is_hard_cut() -> None:
    line = "甲" * (CHILD_MAX_CHARS * 3)

    parents = chunker.split(line)

    children = [child for parent in parents for child in parent.children]
    assert children, "无标点长行也必须切出子块"
    assert all(child.char_count <= CHILD_MAX_CHARS for child in children)
    assert all(parent.char_count <= PARENT_MAX_CHARS for parent in parents)
    # 硬切仍应覆盖原文全部内容
    assert sum(child.char_count for child in children) >= len(line)


def test_heading_starts_a_new_parent_block() -> None:
    markdown = "# 标题一\n" + "甲。" * 30 + "\n\n# 标题二\n" + "乙。" * 30

    parents = chunker.split(markdown)
    combined = "\n".join(parent.content for parent in parents)

    assert "# 标题一" in combined
    assert "# 标题二" in combined
    assert combined.count("# 标题一") == 1


def test_mixed_language_text_keeps_word_boundaries() -> None:
    """英文句末空白不得被吃掉（按区间切片而非拼接）。"""

    markdown = "Reimbursement policy. Travel limit is 2000 CNY. Approval is required."

    parents = chunker.split(markdown)

    children = [child.content for parent in parents for child in parent.children]
    assert any("Travel limit is 2000 CNY." in child for child in children)
    assert not any("policy.Travel" in child for child in children)


def test_custom_limits_are_honoured() -> None:
    small = Chunker(parent_target=200, child_target=80, child_max=120, parent_max=300)

    parents = small.split(make_markdown(7))

    assert all(parent.char_count <= 300 for parent in parents)
    assert all(
        child.char_count <= 120 for parent in parents for child in parent.children
    )


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        Chunker(child_target=400, child_max=100)
    with pytest.raises(ValueError):
        Chunker(parent_target=1200, parent_max=500)
