"""问答状态定义（tasklist 12.1，design.md §3.3 / TECH_SPEC §8.1）。

State 是节点之间传递的**唯一**数据。用 `TypedDict` 而非 dataclass：LangGraph 按 dict 语义
合并节点返回的增量，节点只回自己改动的字段，不必重建整份状态。

字段按链路阶段分四组：改写 → 召回排序 → 权限上下文 → 输出。另有三个由入口节点写入、
供条件边使用的控制字段（`gap_threshold` 等）。

**P3 的关键约定**：`denied` 只保留 `unit_id`，绝不携带标题与正文。把无权单元的正文写进
State 就等于泄露——它会随 `astream` 的节点更新推给前端，也会被后续节点拼进 Prompt。
"事后不输出"做不到安全，只有从一开始就不写进来。
"""

from __future__ import annotations

from typing import NotRequired, TypedDict

from app.engines.retrieve import ParentContext, RecalledChunk


class CitationAsset(TypedDict):
    """引用附带的图片资产（TECH_SPEC §8.1：后端代理地址，不是对象存储地址）。"""

    name: str
    url: str


class Citation(TypedDict):
    unit_id: str
    title: str
    chunk_id: str
    snippet: str
    assets: NotRequired[list[CitationAsset]]


class QAState(TypedDict, total=False):
    """全部字段可选：节点只回填自己负责的部分，未跑到的字段自然缺席。"""

    # --- 输入 ---
    user_id: str
    session_id: str | None
    question: str

    # --- 控制：由入口写入，供条件边使用 ---
    # 缺口阈值与 FAQ 阈值都来自系统配置，节点不查配置表，一律由入口注入
    gap_threshold: float

    # --- FAQ ---
    faq_hit: bool
    faq_answer: str | None

    # --- 改写 ---
    rewritten: str
    keywords: list[str]
    hyde: str | None

    # --- 召回与排序 ---
    dense_hits: list[RecalledChunk]
    sparse_hits: list[RecalledChunk]
    hyde_hits: list[RecalledChunk]
    # 三路融合后的 Top-20
    child_hits: list[RecalledChunk]
    rerank_scores: list[float]
    # 断崖切分后的 Top-10
    reranked: list[RecalledChunk]

    # --- 权限与上下文 ---
    allowed: list[RecalledChunk]
    # 仅 unit_id：标题与正文一律不落 State（P3）
    denied: list[str]
    parent_contexts: list[ParentContext]
    # denied 非空且 allowed 也非空时置真，generate 据此发 acl_notice
    acl_notice: bool

    # --- 输出 ---
    # 只取问句 dense 路的余弦最大值（TECH_SPEC §8.1），不用 HyDE 路分数
    max_similarity: float
    answer: str
    citations: list[Citation]
    # answered | denied | gap；各出口明确写入，审计不比较中文文案推断状态
    answer_status: str
    # 两次 Chat（改写 + 生成）的 usage 累积；FAQ / 固定出口均为 0
    prompt_tokens: int
    completion_tokens: int
    audit_id: str
