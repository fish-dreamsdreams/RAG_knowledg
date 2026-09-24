"""修复被冻住的 created_at 默认值

Revision ID: 94b89ba34c59
Revises: b3f1c8a27d54
Create Date: 2026-09-15 18:50:57.678198

`chat_messages.created_at` 与 `qa_audit_logs.created_at` 建表时用的是字面量
`DEFAULT 'now()'`，PostgreSQL 把 `'now()'::timestamptz` 当**固定时间输入**在 DDL 时就解析掉，
于是全表时间戳都等于建表那一刻（实测两列都是 `2026-09-14 12:23:08.283965+00`）。
后果不是"时间不准"这么轻：

- 会话回放按时间排序 → 同一会话内顺序变成随机的，问答会被读成"先答后问"；
- 看板 Token 趋势按 `created_at` 聚合 → 所有审计挤在同一个点上。

改成函数默认值 `now()`（每次插入按事务时间求值），与 `TimestampMixin` 的其余表一致。
已写入的行无法还原真实时间，本迁移不动它们：能补救的只有"以后不再错"。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "94b89ba34c59"
down_revision: Union[str, None] = "b3f1c8a27d54"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 受影响的两列；两处写法都一样，逐个改而不是拼接表名
COLUMNS = (("chat_messages", "created_at"), ("qa_audit_logs", "created_at"))


def upgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=sa.text("now()"),
        )


def downgrade() -> None:
    # 回退即还原成"同样错"的字面量（`DEFAULT 'now()'`）：迁移的历史状态是什么样就退回什么样，
    # 免得回退后的库与前一个版本的行为对不上。真要长期停在旧版本，先评估数据后果。
    for table, column in COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=sa.text("'now()'"),
        )
