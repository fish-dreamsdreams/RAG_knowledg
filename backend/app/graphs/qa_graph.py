"""问答图组装与编译（tasklist 12.5，TECH_SPEC §8.1）。

拓扑固定，不得跳过 ACL 节点：

```
START → faq_match ─┬─ faq_hit ──────────────→ respond_faq ──┐
                   └─ 否则 → rewrite → retrieve_hybrid →   │
                      rerank → cutoff → acl_filter ─┬─ allowed 非空 → expand_parent → generate ─┤
                                                   ├─ 仅 denied ─────────────────→ respond_denied ─┤
                                                   └─ 皆空/相似度不足 ────────────→ respond_gap ────┤
                                                                                                   ↓
                                                                                                  audit → END
```

`qa_graph` 是模块级单例：编译一次、进程内复用。图本身无状态，运行期依赖全部经
`config.configurable` 注入（见 `graphs/context.py`），因此同一个图实例可以安全服务并发问答。

**入口注入 `gap_threshold`**：`acl_filter` 从系统配置写入缺口阈值。放在这里而不是让调用方
传入，是因为漏传不会报错、只会让每轮问答都走缺口出口——这种失败模式太安静了。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs import edges
from app.graphs.nodes.access import acl_filter, expand_parent
from app.graphs.nodes.answer import generate, respond_denied, respond_gap
from app.graphs.nodes.audit import audit
from app.graphs.nodes.faq import faq_match, respond_faq
from app.graphs.nodes.rank import cutoff, rerank
from app.graphs.nodes.retrieve import retrieve_hybrid
from app.graphs.nodes.rewrite import rewrite
from app.graphs.state import QAState

# 四条出口，全部必须汇入 audit（P12）
_EXITS = ("respond_faq", "generate", "respond_denied", "respond_gap")


def build_graph():
    """组装并编译问答图。返回 `CompiledStateGraph`，可直接 `ainvoke` / `astream`。"""

    graph = StateGraph(QAState)

    graph.add_node("faq_match", faq_match)
    graph.add_node("rewrite", rewrite)
    graph.add_node("retrieve_hybrid", retrieve_hybrid)
    graph.add_node("rerank", rerank)
    graph.add_node("cutoff", cutoff)
    graph.add_node("acl_filter", acl_filter)
    graph.add_node("expand_parent", expand_parent)
    graph.add_node("generate", generate)
    graph.add_node("respond_faq", respond_faq)
    graph.add_node("respond_denied", respond_denied)
    graph.add_node("respond_gap", respond_gap)
    graph.add_node("audit", audit)

    graph.add_edge(START, "faq_match")
    graph.add_conditional_edges(
        "faq_match",
        edges.after_faq,
        {edges.RESPOND_FAQ: edges.RESPOND_FAQ, edges.REWRITE: edges.REWRITE},
    )
    graph.add_edge("rewrite", "retrieve_hybrid")
    graph.add_edge("retrieve_hybrid", "rerank")
    graph.add_edge("rerank", "cutoff")
    graph.add_edge("cutoff", "acl_filter")
    graph.add_conditional_edges(
        "acl_filter",
        edges.after_acl,
        {
            edges.EXPAND_PARENT: edges.EXPAND_PARENT,
            edges.RESPOND_DENIED: edges.RESPOND_DENIED,
            edges.RESPOND_GAP: edges.RESPOND_GAP,
        },
    )
    graph.add_edge("expand_parent", "generate")

    for node in _EXITS:
        graph.add_edge(node, "audit")
    graph.add_edge("audit", END)

    return graph.compile()


qa_graph = build_graph()

__all__ = ["build_graph", "qa_graph"]
