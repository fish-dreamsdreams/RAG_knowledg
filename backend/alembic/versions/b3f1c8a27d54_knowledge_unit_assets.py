"""knowledge_unit_assets

Revision ID: b3f1c8a27d54
Revises: e745c0612b4f
Create Date: 2026-09-15 12:20:00.000000

图片资产表（TECH_SPEC §5.2 / §8.0）：一行对应对象存储
`kb/units/{unit_id}/assets/{name}` 里的一个对象，`rel_path` 是 chunk 正文引用的相对 key。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3f1c8a27d54"
down_revision: Union[str, None] = "e745c0612b4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_unit_assets",
        sa.Column("unit_id", sa.UUID(), nullable=False),
        sa.Column("rel_path", sa.String(length=512), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"], ["knowledge_units.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "unit_id", "rel_path", name="uq_knowledge_unit_assets_rel_path"
        ),
    )
    op.create_index(
        "idx_knowledge_unit_assets_unit_id",
        "knowledge_unit_assets",
        ["unit_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_knowledge_unit_assets_unit_id", table_name="knowledge_unit_assets"
    )
    op.drop_table("knowledge_unit_assets")
