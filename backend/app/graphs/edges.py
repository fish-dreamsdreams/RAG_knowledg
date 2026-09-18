"""条件边（tasklist 12.1，TECH_SPEC §8.1）。

条件边都是**纯函数**：读 State，返回下一个节点名。这让「FAQ 命中绝不进检索链」「allowed
为空绝不调生成」这类不变量可以直接单测断言，不必启动整张图。

分支顺序由规范固定，不得重排：

```
faq_match   ├─ faq_hit     → respond_faq
            └─ 否则        → rewrite

acl_filter  ├─ allowed 非空            → expand_parent
            ├─ allowed 空且 denied 非空 → respond_denied
            └─ 皆空或相似度低于阈值     → respond_gap
```

判定 denied 优先于 gap 是有意的：检索到了但无权，与压根没检索到，是两件不同的事，前者应
告知权限而非谎称"没有这方面的知识"。
"""

from __future__ import annotations

from app.graphs.state import QAState

RESPOND_FAQ = "respond_faq"
REWRITE = "rewrite"
EXPAND_PARENT = "expand_parent"
RESPOND_DENIED = "respond_denied"
RESPOND_GAP = "respond_gap"


def after_faq(state: QAState) -> str:
    """FAQ 命中即短路：不进检索、不调改写（P4）。

    命中时省掉的是一次改写调用 + 三路召回 + 重排，这是缓存层存在的全部意义。
    """

    return RESPOND_FAQ if state.get("faq_hit") else REWRITE


def after_acl(state: QAState) -> str:
    """权限分流 + 相关性兜底。顺序是有意的：

    1. **仅 denied 时先判权限**：「检索到了但无权」与「压根没检索到」是两件事，不能把有
       文档却无权的内容谎称为不存在。所以 allowed 为空而 denied 非空时直接拒答。
    2. **再看相似度**：向量召回永远返回 top-k，语料里只要有一份全局可读文档，`allowed`
       就恒定非空——只按「有没有可读命中」分流，缺口出口等于不可达，`knowledge_gaps`
       在生产里不会增长，运营看板的「转建导入」闭环随之失效。因此相似度低于阈值时，
       即便有可读命中也判缺口：命中的都是无关内容，不该拿它去编答案。
    3. 过了阈值才有资格作答。

    阈值标定（design.md §3.3）：实测 83 条真实问答中，相关问法 `max_similarity` ≥ 0.634，
    无关问法 ≤ 0.596，故取 0.62。这个分带很窄——换嵌入模型、换语料规模后必须重新标定，
    否则要么把「弱相关但答得出」的问题说成没有知识，要么让缺口又收不到。
    """

    if not state.get("allowed") and state.get("denied"):
        return RESPOND_DENIED

    if state.get("max_similarity", 0.0) < state.get("gap_threshold", 0.0):
        return RESPOND_GAP

    if state.get("allowed"):
        return EXPAND_PARENT

    # 有召回但既没通过权限也没被拒（例如候选全部失效），仍按缺口处理：
    # 没有可用的上下文就不该让模型凭空作答（P5）
    return RESPOND_GAP


__all__ = [
    "EXPAND_PARENT",
    "RESPOND_DENIED",
    "RESPOND_FAQ",
    "RESPOND_GAP",
    "REWRITE",
    "after_acl",
    "after_faq",
]
