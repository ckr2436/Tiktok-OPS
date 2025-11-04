"""Backfill advertiser-store links from ttb_stores and drop ttb_stores.advertiser_id"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0016_ttb_store_adv_to_link"
down_revision = "0015_ttb_link_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) 调整 ttb_advertiser_store_links 上的索引：
    #    - 原来是单列 advertiser_id / store_id
    #    - 改成带 workspace_id, auth_id 的复合索引，匹配我们查询条件
    with op.batch_alter_table("ttb_advertiser_store_links") as batch_op:
        # 旧索引可能已经存在，先删
        try:
            batch_op.drop_index("idx_ttb_adv_store_link_adv")
        except Exception:
            # 兼容本地测试环境（比如 sqlite）索引不存在的情况
            pass

        try:
            batch_op.drop_index("idx_ttb_adv_store_link_store")
        except Exception:
            pass

        # 新索引：按 scope + advertiser_id / store_id
        batch_op.create_index(
            "idx_ttb_adv_store_link_scope_adv",
            ["workspace_id", "auth_id", "advertiser_id"],
        )
        batch_op.create_index(
            "idx_ttb_adv_store_link_scope_store",
            ["workspace_id", "auth_id", "store_id"],
        )

    # 2) 从 ttb_stores.backfill 到 ttb_advertiser_store_links
    #
    #    只把当前还没有对应 link 记录的 (workspace, auth, advertiser_id, store_id) 插进去，
    #    并打 source = 'backfill.stores.advertiser_id'，方便 downgrade 时识别 & 清理。
    conn = op.get_bind()

    backfill_sql = sa.text(
        """
        INSERT INTO ttb_advertiser_store_links (
            workspace_id,
            auth_id,
            advertiser_id,
            store_id,
            relation_type,
            store_authorized_bc_id,
            bc_id_hint,
            source,
            raw_json
        )
        SELECT
            s.workspace_id,
            s.auth_id,
            s.advertiser_id,
            s.store_id,
            'UNKNOWN' AS relation_type,
            s.store_authorized_bc_id,
            s.bc_id,
            'backfill.stores.advertiser_id' AS source,
            s.raw_json
        FROM ttb_stores AS s
        LEFT JOIN ttb_advertiser_store_links AS l
            ON  l.workspace_id  = s.workspace_id
            AND l.auth_id       = s.auth_id
            AND l.advertiser_id = s.advertiser_id
            AND l.store_id      = s.store_id
        WHERE s.advertiser_id IS NOT NULL
          AND l.id IS NULL
        """
    )
    conn.execute(backfill_sql)

    # 3) 删除 ttb_stores.advertiser_id 字段以及它的索引
    with op.batch_alter_table("ttb_stores") as batch_op:
        try:
            batch_op.drop_index("idx_ttb_store_adv")
        except Exception:
            # 如果本地 schema 没这个索引，直接忽略
            pass

        # 彻底删掉列：后续代码一律通过 ttb_advertiser_store_links 表表达关系
        batch_op.drop_column("advertiser_id")


def downgrade() -> None:
    conn = op.get_bind()

    # 1) 把 advertiser_id 列加回 ttb_stores，并重建原来的索引
    with op.batch_alter_table("ttb_stores") as batch_op:
        batch_op.add_column(
            sa.Column("advertiser_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            "idx_ttb_store_adv",
            ["advertiser_id"],
        )

    # 2) 用 link 表尽量回填 ttb_stores.advertiser_id
    #    规则：每个 (workspace_id, auth_id, store_id) 选一个最小 advertiser_id。
    #    这个语句是标准 SQL，MySQL / SQLite 都能跑。
    refill_sql = sa.text(
        """
        UPDATE ttb_stores AS s
        SET advertiser_id = (
            SELECT l.advertiser_id
            FROM ttb_advertiser_store_links AS l
            WHERE l.workspace_id = s.workspace_id
              AND l.auth_id      = s.auth_id
              AND l.store_id     = s.store_id
            ORDER BY l.advertiser_id
            LIMIT 1
        )
        WHERE advertiser_id IS NULL
        """
    )
    conn.execute(refill_sql)

    # 3) 删除本迁移 backfill 出来的 link 记录（保留真正从 API 写入的）
    cleanup_links_sql = sa.text(
        """
        DELETE FROM ttb_advertiser_store_links
        WHERE source = 'backfill.stores.advertiser_id'
        """
    )
    conn.execute(cleanup_links_sql)

    # 4) 把 ttb_advertiser_store_links 的索引恢复成旧版单列形式
    with op.batch_alter_table("ttb_advertiser_store_links") as batch_op:
        try:
            batch_op.drop_index("idx_ttb_adv_store_link_scope_store")
        except Exception:
            pass
        try:
            batch_op.drop_index("idx_ttb_adv_store_link_scope_adv")
        except Exception:
            pass

        batch_op.create_index(
            "idx_ttb_adv_store_link_store",
            ["store_id"],
        )
        batch_op.create_index(
            "idx_ttb_adv_store_link_adv",
            ["advertiser_id"],
        )

