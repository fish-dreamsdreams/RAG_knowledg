"""系统配置读取与写入（tasklist 13.4）。"""

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelConfig


def column_defaults() -> dict[str, Any]:
    """从列定义取标量默认值。

    直接 `ModelConfig()` 的属性**全是 None**——默认值在 INSERT 时才生效——而接口要吐具体数值，
    所以把默认值显式喂进构造函数。从列上读而不是再抄一份常量：抄一份就会与模型定义漂移。
    """

    return {
        column.name: column.default.arg
        for column in ModelConfig.__table__.columns
        if column.default is not None and column.default.is_scalar
    }


async def load_model_config(session: AsyncSession) -> ModelConfig:
    """取单行模型配置；表为空时返回一个**未入库**的默认实例。

    返回默认实例而不是 `None`：调用方（问答链、看板）一律需要一套可用的阈值与模型名，
    而 `build_chat_model` 对空字段本就回退到 env。这样"还没配过"与"配过但留空"走同一条
    路径，调用方不必到处判空。
    """

    row = (await session.execute(select(ModelConfig).limit(1))).scalar_one_or_none()
    return row if row is not None else ModelConfig(**column_defaults())


async def lock_model_config(session: AsyncSession) -> None:
    """串行化「读—改—写」：这张表只该有一行，并发首次保存会插出两行。

    表上没有可依赖的唯一约束（单行表用不上自增主键约束），因此用事务级 advisory lock，
    与知识缺口聚合（`repositories/faq.py`）同一套做法。
    """

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('model_config', 0))")
    )


async def save_model_config(session: AsyncSession, row: ModelConfig) -> ModelConfig:
    """落库。表为空时 `load_model_config` 给的是未入库实例，这里补 `add`。"""

    if row not in session:
        session.add(row)
    await session.flush()
    return row
