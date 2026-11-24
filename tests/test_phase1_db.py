import pytest
import os
import json
from app.config import settings
from app.db import init_db_pool, close_db_pool, get_db_pool

# Use local test socket if running in sandbox
if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
    settings.postgres_socket_dir = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
    settings.postgres_port = 54329


@pytest.mark.asyncio
async def test_database_connection_and_schema():
    """Verify that connection pool initializes and schema tables/indices are created."""
    pool = await init_db_pool()
    assert pool is not None

    async with pool.acquire() as conn:
        # Check inspection_items table
        items_table = await conn.fetchval(
            "SELECT to_regclass('public.inspection_items');"
        )
        assert items_table == "inspection_items"

        # Check audit_events table
        audit_table = await conn.fetchval(
            "SELECT to_regclass('public.audit_events');"
        )
        assert audit_table == "audit_events"

        # Check partial index for pending items
        index_name = await conn.fetchval(
            "SELECT indexname FROM pg_indexes WHERE indexname = 'idx_inspection_items_pending';"
        )
        assert index_name == "idx_inspection_items_pending"

    await close_db_pool()


@pytest.mark.asyncio
async def test_atomic_skip_locked_query():
    """Verify that SELECT ... FOR UPDATE SKIP LOCKED works correctly on pending items."""
    pool = await init_db_pool()
    test_item_id = "test_item_p1_001"

    async with pool.acquire() as conn:
        # Insert test item
        await conn.execute(
            """
            INSERT INTO inspection_items (id, priority, status, payload)
            VALUES ($1, $2, 'PENDING', $3)
            ON CONFLICT (id) DO UPDATE SET status = 'PENDING';
            """,
            test_item_id,
            10,
            json.dumps({"part": "P-100", "defect": "Crack detected"})
        )

        # Execute atomic claim query
        claimed = await conn.fetchrow(
            """
            WITH next_item AS (
                SELECT id FROM inspection_items
                WHERE status = 'PENDING'
                ORDER BY priority DESC, arrival_time ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE inspection_items
            SET status = 'CLAIMED',
                claimed_by = 'reviewer_test_1',
                claimed_at = NOW(),
                lease_expires_at = NOW() + INTERVAL '8 seconds'
            FROM next_item
            WHERE inspection_items.id = next_item.id
            RETURNING inspection_items.id, inspection_items.priority, inspection_items.status, inspection_items.claimed_by;
            """
        )

        assert claimed is not None
        assert claimed["id"] == test_item_id
        assert claimed["status"] == "CLAIMED"
        assert claimed["claimed_by"] == "reviewer_test_1"

        # Try claiming again concurrently - should return None because the item is now CLAIMED
        second_claim = await conn.fetchrow(
            """
            WITH next_item AS (
                SELECT id FROM inspection_items
                WHERE status = 'PENDING'
                ORDER BY priority DESC, arrival_time ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE inspection_items
            SET status = 'CLAIMED',
                claimed_by = 'reviewer_test_2',
                claimed_at = NOW(),
                lease_expires_at = NOW() + INTERVAL '8 seconds'
            FROM next_item
            WHERE inspection_items.id = next_item.id
            RETURNING inspection_items.id;
            """
        )
        assert second_claim is None

        # Clean up test row
        await conn.execute("DELETE FROM inspection_items WHERE id = $1;", test_item_id)

    await close_db_pool()
