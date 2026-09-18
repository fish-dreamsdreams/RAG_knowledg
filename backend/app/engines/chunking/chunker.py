"""父子块切分（tasklist 5.1，TECH_SPEC §8.1 父子块约定）。

约定：父块约 1200 字 / overlap 100，子块约 400 字 / overlap 80，按标题与空行切分。
子块进 Milvus 用于召回，父块只在命中后用于拼上下文（design.md §3.3 `expand_parent`）。

设计要点：

- **区间切片，不拼接字符串**：块内容是原文的连续切片。若按句子拼接，
  英文句末空白会被正则吃掉（'Hello. World.' 变成 'Hello.World.'），污染原文。
- **切点对齐句子边界**：块的起止都落在句子/行的开头，因此不会有句子被拦腰切开。
  「任意句子都能在某个子块中完整出现」这条属性因此可以严格成立（tasklist 5.5）。
- **纯函数、确定性**：同一份 Markdown 必须切出完全相同的块，便于重放与测试。
- **不调模型**：`engines/chunking` 不依赖 torch（design.md §3.1）。
- **图片标记与其图注不可分（P18）**：图注只有紧跟在图片标记同一块里，命中后才能连带
  出图；被拆到相邻块就会出现“图注块没图、图片块没词”的双输局面。
"""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass

# 目标长度（软约束，装箱时尽量不超）
PARENT_TARGET_CHARS = 1200
CHILD_TARGET_CHARS = 400
# 相邻块重叠字符数：约 100 / 80，切点会对齐到句子起点，实际可能略大或略小
PARENT_OVERLAP_CHARS = 100
CHILD_OVERLAP_CHARS = 80
# 硬上限：单个块的长度上限，属性测试断言不得越过
CHILD_MAX_CHARS = 500
PARENT_MAX_CHARS = 1500

_HEADING_RE = re.compile(r"^#{1,6}\s")
# 句子边界：中英文句末标点，以及换行（保证句子不跨行，行首即句子起点）
_BOUNDARY_RE = re.compile(r"[。！？；]|[.!?;](?=\s)|\n")
# 图片标记行（引用对象存储相对 key）与紧跟的图注行：两者必须留在同一块（P18）
_IMAGE_LINE_RE = re.compile(r"^!\[[^\]]*\]\([^)]*assets/[^)]+\)$")
_CAPTION_LINE_RE = re.compile(r"^图注：")
# 正文里任意位置的图片引用：切块只看整行，答案侧要从父块正文里回收全部图片文件名。
# 要求 `assets/` 紧跟在括号后（即相对 key，`./` 前缀宽容掉），否则手写文档里
# `https://cdn.example.com/assets/logo.png` 这类外链会被当成单元内的图片。
_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\((?:\./)?assets/([^)\s/]+)\)")

Span = tuple[int, int]


def _inside(position: int, atomic: list[Span]) -> Span | None:
    """position 落在哪个原子区间内部（严格内部，端点不算）。"""

    for span in atomic:
        if span[0] < position < span[1]:
            return span
    return None


def _atomic_spans(text: str) -> list[Span]:
    """找出“图片标记行 + 紧接的图注行”构成的最小不可分区间。

    中间允许隔空行（Markdown 里图片常独立成段），但图注必须紧接出现；
    找不到图注的图片行不进此列，不影响普通切块。
    """

    lines = text.split("\n")
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1

    spans: list[Span] = []
    index = 0
    while index < len(lines):
        if not _IMAGE_LINE_RE.match(lines[index].strip()):
            index += 1
            continue
        probe = index + 1
        while probe < len(lines) and not lines[probe].strip():
            probe += 1
        if probe >= len(lines) or not _CAPTION_LINE_RE.match(lines[probe].strip()):
            index += 1
            continue
        spans.append((offsets[index], offsets[probe] + len(lines[probe])))
        index = probe + 1
    return spans


def _merge_atomic(spans: list[Span], atomic: list[Span]) -> list[Span]:
    """把落在同一个原子区间内的相邻句子合并成一个不可分单元。"""

    merged: list[Span] = []
    for begin, end in spans:
        if merged:
            region = _inside(merged[-1][1], atomic)
            if region is not None and _inside(begin, atomic) is region:
                merged[-1] = (merged[-1][0], end)
                continue
        merged.append((begin, end))
    return merged


@dataclass(frozen=True)
class Child:
    """子块：进 Milvus 的最小检索单位。"""

    ordinal: int
    content: str
    char_count: int
    content_hash: str


@dataclass(frozen=True)
class Parent:
    """父块：命中子块后用于拼上下文的段落。"""

    ordinal: int
    content: str
    char_count: int
    content_hash: str
    children: tuple[Child, ...]


def content_hash(content: str) -> str:
    """内容指纹（sha256 hex，64 字符），用于切片预览与去重展示。"""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def asset_names(markdown: str) -> list[str]:
    """按出现顺序取出正文引用的图片文件名（去重）。

    图片与正文在存储上解耦：检索命中某块后，从它的正文里就能解析出附带的图，不必反查
    `knowledge_unit_assets`（TECH_SPEC §8.0）。只认 `assets/` 这一层相对 key，取最后一段
    文件名——与 `object_store.asset_key` 的拼接口径一致。
    """

    return list(dict.fromkeys(_IMAGE_REF_RE.findall(markdown)))


def _normalize(markdown: str) -> str:
    return markdown.replace("\r\n", "\n").replace("\r", "\n")


def _sentence_spans(
    text: str, offset: int = 0, atomic: list[Span] | None = None
) -> list[Span]:
    """把 text 切成句子区间，覆盖全部字符（空白可能落在区间首尾）。

    传入 `atomic` 时，落在同一原子区间内的句子会被合并，保证不可分（P18）。
    """

    spans: list[Span] = []
    start = 0
    for match in _BOUNDARY_RE.finditer(text):
        end = match.end()
        if end > start:
            spans.append((start + offset, end + offset))
        start = end
    if start < len(text):
        spans.append((start + offset, len(text)))
    return _merge_atomic(spans, atomic) if atomic else spans


def _block_spans(text: str, atomic: list[Span] | None = None) -> list[Span]:
    """按标题与空行切块；标题另起一块，与其后正文一起成块。

    `atomic` 区间内部一律不切：图片标记与其图注不能被空行拆开（P18）。
    """

    spans: list[Span] = []
    start: int | None = None
    position = 0
    for line in text.split("\n"):
        line_start = position
        position += len(line) + 1  # +1 补回被 split 去掉的换行
        protected = atomic is not None and _inside(line_start, atomic) is not None
        if not line.strip():
            if start is not None and not protected:
                spans.append((start, line_start))
                start = None
            continue
        if _HEADING_RE.match(line) and start is not None and not protected:
            spans.append((start, line_start))
            start = line_start
        if start is None:
            start = line_start
    if start is not None:
        spans.append((start, position))
    return [(begin, end) for begin, end in spans if text[begin:end].strip()]


def _cut_span(text: str, span: Span, max_chars: int) -> list[Span]:
    """把超长区间压到上限内：优先句末标点，其次换行，最后空格，兜底按长度切。"""

    begin, end = span
    if end - begin <= max_chars:
        return [span]

    pieces: list[Span] = []
    cursor = begin
    while end - cursor > max_chars:
        window = text[cursor : cursor + max_chars]
        cut = max(
            window.rfind("。"),
            window.rfind("；"),
            window.rfind("！"),
            window.rfind("？"),
        )
        if cut < max_chars // 2:
            cut = window.rfind("\n")
        if cut < max_chars // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_chars - 1  # 无可用边界：按长度切，必须保证前进
        pieces.append((cursor, cursor + cut + 1))
        cursor += cut + 1
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def _cut_spans(text: str, spans: list[Span], max_chars: int) -> list[Span]:
    result: list[Span] = []
    for span in spans:
        result.extend(_cut_span(text, span, max_chars))
    return result


def _snap_start(valid_starts: list[int], desired: int, floor: int) -> int:
    """把起点回退到最近的合法句子起点，且不早于 floor。"""

    index = bisect.bisect_right(valid_starts, desired) - 1
    if index < 0:
        return max(desired, floor)
    return max(valid_starts[index], floor)


def _pack_spans(
    spans: list[Span], target: int, overlap: int, valid_starts: list[int]
) -> list[Span]:
    """贪心装箱：每组尽量不超过 target，相邻组共享约 overlap 字。

    组起点会回退并对齐到句子起点，因此不会从半句话开始。
    """

    groups: list[Span] = []
    if not spans:
        return groups

    index = 0
    count = len(spans)
    start = spans[0][0]
    while index < count:
        end = spans[index][1]
        cursor = index
        while cursor + 1 < count and spans[cursor + 1][1] - start <= target:
            cursor += 1
            end = spans[cursor][1]
        groups.append((start, end))
        if cursor + 1 >= count:
            break
        start = _snap_start(
            valid_starts, desired=spans[cursor + 1][0] - overlap, floor=start
        )
        index = cursor + 1
    return groups


class Chunker:
    """Markdown → 父子块。

    用法：`Chunker().split(markdown) -> list[Parent]`。
    参数可覆盖，便于测试边界与后续调参。
    """

    def __init__(
        self,
        *,
        parent_target: int = PARENT_TARGET_CHARS,
        parent_overlap: int = PARENT_OVERLAP_CHARS,
        parent_max: int = PARENT_MAX_CHARS,
        child_target: int = CHILD_TARGET_CHARS,
        child_overlap: int = CHILD_OVERLAP_CHARS,
        child_max: int = CHILD_MAX_CHARS,
    ) -> None:
        if child_max < child_target:
            raise ValueError("child_max 必须 >= child_target")
        if parent_max < parent_target:
            raise ValueError("parent_max 必须 >= parent_target")
        self.parent_target = parent_target
        self.parent_overlap = parent_overlap
        self.parent_max = parent_max
        self.child_target = child_target
        self.child_overlap = child_overlap
        self.child_max = child_max

    def split(self, markdown: str) -> list[Parent]:
        if not markdown or not markdown.strip():
            return []

        text = _normalize(markdown)
        atomic = _atomic_spans(text)
        valid_starts = [start for start, _ in _sentence_spans(text, atomic=atomic)]

        # 1) 按标题与空行分块，再把超长块压到父块上限内。
        blocks = _cut_spans(text, _block_spans(text, atomic), self.parent_max)

        # 2) 父块：贪心装箱 + 重叠。
        parents: list[Parent] = []
        for start, end in _pack_spans(
            blocks, self.parent_target, self.parent_overlap, valid_starts
        ):
            parent_text = text[start:end].strip()
            if not parent_text:
                continue
            children = self._split_children(parent_text)
            if not children:
                continue
            parents.append(
                Parent(
                    ordinal=len(parents),
                    content=parent_text,
                    char_count=len(parent_text),
                    content_hash=content_hash(parent_text),
                    children=tuple(children),
                )
            )
        return parents

    def _split_children(self, parent_text: str) -> list[Child]:
        """父块内部切子块：整句装箱 + 重叠，超长句在上限处硬切。"""

        atomic = _atomic_spans(parent_text)
        sentences = _sentence_spans(parent_text, atomic=atomic)
        sentence_starts = [start for start, _ in sentences]
        units = _cut_spans(parent_text, sentences, self.child_max)
        groups = _pack_spans(
            units, self.child_target, self.child_overlap, sentence_starts
        )

        children: list[Child] = []
        for start, end in groups:
            for piece_start, piece_end in _cut_span(
                parent_text, (start, end), self.child_max
            ):
                content = parent_text[piece_start:piece_end].strip()
                if not content:
                    continue
                children.append(
                    Child(
                        ordinal=len(children),
                        content=content,
                        char_count=len(content),
                        content_hash=content_hash(content),
                    )
                )
        return children
