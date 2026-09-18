"""场景 B 演示日志加载器（任务 15.2 的数据部分）。

把 `data/seeds/qa_demo_logs.json` 的问答簇展开为 `qa_audit_logs` 行，并按簇聚合出
`knowledge_gaps`，供两个演示链路消费：

- **FAQ 挖掘（任务 11.4）**：读 `answer_status='answered'` 且 `faq_hit=false` 的问题做语义归并，
  频次 ≥ `faq_cluster_min_freq` 时写 `faq_candidates`。四个「退款/无理由」簇就是它的输入。
- **知识缺口（任务 13.1）**：`answer_status='gap'` 的问题聚合为 `knowledge_gaps`，
  清关三个簇即 PRD 界面稿里的缺口列表，供「转建导入」演示。

运行：在 backend/ 目录执行 `python -m app.services.demo_logs`
幂等：先删除 `trace_id` 以 `demo-` 开头的旧演示行与同名缺口，再重建。

注意：本加载器直接造审计行，不写 `chat_sessions`/`chat_messages`（会话历史由真实问答产生）；
`allowed_unit_ids`/`citation_ids` 按文档标题反查知识单元，未导入时留空，导入后重跑即可回填。
"""

import asyncio
import json
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.db import SessionLocal
from app.models import KnowledgeGap, KnowledgeUnit, QaAuditLog, User

logger = logging.getLogger(__name__)

DEMO_LOG_FILE = "qa_demo_logs.json"
DEMO_TRACE_PREFIX = "demo-"
# 固定种子：同一份数据每次展开出的时间与相似度完全一致，便于截图与回归
RANDOM_SEED = 20260914
DEMO_USERNAME = "cs01"


def _load() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "data" / "seeds" / DEMO_LOG_FILE
    return json.loads(path.read_text(encoding="utf-8"))


def _created_at(rng: random.Random, now: datetime, days_ago: float, *, hour_range: tuple[int, int] = (9, 19)) -> datetime:
    moment = now - timedelta(days=days_ago)
    return moment.replace(
        hour=rng.randint(*hour_range), minute=rng.randint(0, 59), second=0, microsecond=0
    )


def demo_gap_questions() -> list[str]:
    """演示种子的缺口代表问句。

    清场工具拿它当白名单：这些问句的缺口行是 `demo_logs` 按种子聚出来的，留着；其余都属于
    真跑出来的痕迹。共用一处读取，免得种子格式变了而白名单没跟上。
    """

    return [cluster["representative"] for cluster in _load()["gap_clusters"]]


async def _demo_user(session: AsyncSession) -> User:
    user = (
        await session.execute(select(User).where(User.username == DEMO_USERNAME))
    ).scalar_one_or_none()
    if user is None:
        raise RuntimeError(f"未找到演示账号 {DEMO_USERNAME}，请先执行 python -m app.services.seed")
    return user


async def _unit_ids_by_title(session: AsyncSession, titles: set[str]) -> dict[str, str]:
    if not titles:
        return {}
    rows = await session.execute(
        select(KnowledgeUnit.title, KnowledgeUnit.id).where(KnowledgeUnit.title.in_(titles))
    )
    return {title: str(unit_id) for title, unit_id in rows.all()}


async def run_demo_logs() -> dict[str, int]:
    data = _load()
    answered_clusters: list[dict[str, Any]] = data["answered_clusters"]
    gap_clusters: list[dict[str, Any]] = data["gap_clusters"]

    rng = random.Random(RANDOM_SEED)
    now = datetime.now(UTC)

    async with SessionLocal() as session:
        user = await _demo_user(session)
        titles = {t for cluster in answered_clusters for t in cluster["unit_titles"]}
        title_to_id = await _unit_ids_by_title(session, titles)

        # 幂等：清掉上一次的演示行与缺口
        await session.execute(
            delete(QaAuditLog).where(QaAuditLog.trace_id.like(f"{DEMO_TRACE_PREFIX}%"))
        )
        await session.execute(
            delete(KnowledgeGap).where(
                KnowledgeGap.question_text.in_([c["representative"] for c in gap_clusters])
            )
        )

        sequence = 0
        answered_logs = 0
        gap_logs = 0

        def next_trace_id() -> str:
            nonlocal sequence
            sequence += 1
            return f"{DEMO_TRACE_PREFIX}{sequence:04d}"

        # 1) 有知识命中、但未走 FAQ 的问答簇
        for cluster in answered_clusters:
            unit_ids = [title_to_id[t] for t in cluster["unit_titles"] if t in title_to_id] or None
            low, high = cluster["max_similarity_range"]
            for question in cluster["questions"]:
                session.add(
                    QaAuditLog(
                        trace_id=next_trace_id(),
                        user_id=user.id,
                        question=question,
                        faq_hit=False,
                        allowed_unit_ids=unit_ids,
                        citation_ids=unit_ids,
                        denied_count=0,
                        answer_status="answered",
                        max_similarity=round(rng.uniform(low, high), 3),
                        prompt_tokens=rng.randint(700, 1400),
                        completion_tokens=rng.randint(120, 320),
                        latency_ms=rng.randint(1600, 3800),
                        created_at=_created_at(rng, now, rng.uniform(0, cluster["window_days"])),
                    )
                )
                answered_logs += 1

        # 2) 未命中任何知识的缺口簇，同时聚合出 knowledge_gaps
        for cluster in gap_clusters:
            low, high = cluster["max_similarity_range"]
            similarities: list[float] = []
            asked_at: list[datetime] = []
            for offset, question in enumerate(cluster["questions"]):
                created_at = _created_at(rng, now, cluster["last_asked_days_ago"] + offset)
                similarity = round(rng.uniform(low, high), 3)
                similarities.append(similarity)
                asked_at.append(created_at)
                session.add(
                    QaAuditLog(
                        trace_id=next_trace_id(),
                        user_id=user.id,
                        question=question,
                        faq_hit=False,
                        allowed_unit_ids=None,
                        citation_ids=None,
                        denied_count=0,
                        answer_status="gap",
                        max_similarity=similarity,
                        prompt_tokens=0,
                        completion_tokens=0,
                        latency_ms=rng.randint(400, 1200),
                        created_at=created_at,
                    )
                )
                gap_logs += 1

            session.add(
                KnowledgeGap(
                    question_text=cluster["representative"],
                    department_id=user.department_id,
                    freq=len(cluster["questions"]),
                    max_similarity=max(similarities),
                    last_asked_at=max(asked_at),
                    status="open",
                )
            )

        await session.commit()

    summary = {
        "answered_logs": answered_logs,
        "gap_logs": gap_logs,
        "total_logs": answered_logs + gap_logs,
        "gaps": len(gap_clusters),
        "units_linked": len(title_to_id),
    }
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    summary = asyncio.run(run_demo_logs())
    logger.info("演示日志加载完成: %s", summary)
    print(summary)


if __name__ == "__main__":
    main()
