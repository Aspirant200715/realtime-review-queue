import pytest
import os
import json
import asyncio
import time
from typing import Dict, List, Any
from app.config import settings
from app.db import init_db_pool, close_db_pool, get_db_pool

# Use local test socket if running in sandbox
if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
    settings.postgres_socket_dir = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
    settings.postgres_port = 54329


@pytest.mark.asyncio
async def test_database_connection_and_schema():
    """Verify that connection pool initializes and schema tables/indices exist."""
    pool = await init_db_pool()
    assert pool is not None

    async with pool.acquire() as conn:
        items_table = await conn.fetchval("SELECT to_regclass('public.inspection_items');")
        assert items_table == "inspection_items"

        audit_table = await conn.fetchval("SELECT to_regclass('public.audit_events');")
        assert audit_table == "audit_events"

        index_name = await conn.fetchval(
            "SELECT indexname FROM pg_indexes WHERE indexname = 'idx_inspection_items_pending';"
        )
        assert index_name == "idx_inspection_items_pending"

    await close_db_pool()


@pytest.mark.asyncio
async def test_partial_index_explain_analyze():
    """Verify that EXPLAIN ANALYZE actively uses the partial index idx_inspection_items_pending."""
    pool = await init_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items;")

        # Seed 500 items: 300 PENDING, 100 PASSED, 100 CLAIMED
        records = [
            (f"item_idx_{i:04d}", (i % 10) + 1, 'PENDING', json.dumps({"part": f"P-{i}"}))
            for i in range(300)
        ]
        records += [
            (f"item_idx_pass_{i:04d}", 5, 'PASSED', json.dumps({"part": f"P-pass-{i}"}))
            for i in range(100)
        ]
        records += [
            (f"item_idx_claim_{i:04d}", 8, 'CLAIMED', json.dumps({"part": f"P-claim-{i}"}))
            for i in range(100)
        ]

        await conn.executemany(
            """
            INSERT INTO inspection_items (id, priority, status, payload, arrival_time)
            VALUES ($1, $2, $3, $4::jsonb, NOW() - (INTERVAL '1 millisecond' * (500 - $2)));
            """,
            records
        )

        plan_json_str = await conn.fetchval(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            WITH next_item AS (
                SELECT id FROM inspection_items
                WHERE status = 'PENDING'
                ORDER BY priority DESC, arrival_time ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE inspection_items
            SET status = 'CLAIMED',
                claimed_by = 'reviewer_bench',
                claimed_at = NOW(),
                lease_expires_at = NOW() + INTERVAL '8 seconds'
            FROM next_item
            WHERE inspection_items.id = next_item.id
            RETURNING inspection_items.id;
            """
        )
        plan_str = json.dumps(json.loads(plan_json_str), indent=2)
        assert "idx_inspection_items_pending" in plan_str, "PostgreSQL should use idx_inspection_items_pending"

        # Clean up
        await conn.execute("DELETE FROM inspection_items;")

    await close_db_pool()


@pytest.mark.asyncio
async def test_concurrent_claiming_zero_double_assignments():
    """Verify concurrent claims across multiple connections guarantee exactly-once handout."""
    pool = await init_db_pool()

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items;")
        # Seed 60 items with priorities 1 to 10
        records = [
            (f"item_race_{i:03d}", (i % 10) + 1, 'PENDING', json.dumps({"id": i}))
            for i in range(60)
        ]
        await conn.executemany(
            """
            INSERT INTO inspection_items (id, priority, status, payload, arrival_time)
            VALUES ($1, $2, $3, $4::jsonb, NOW() - (INTERVAL '1 millisecond' * (60 - $2)));
            """,
            records
        )

    claim_query = """
    WITH next_item AS (
        SELECT id FROM inspection_items
        WHERE status = 'PENDING'
        ORDER BY priority DESC, arrival_time ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE inspection_items
    SET status = 'CLAIMED',
        claimed_by = $1,
        claimed_at = NOW(),
        lease_expires_at = NOW() + INTERVAL '8 seconds'
    FROM next_item
    WHERE inspection_items.id = next_item.id
    RETURNING inspection_items.id, inspection_items.priority;
    """

    claimed_ids: List[str] = []
    lock = asyncio.Lock()

    async def worker(worker_id: str):
        while True:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(claim_query, worker_id)
                if not row:
                    break
                async with lock:
                    claimed_ids.append(row["id"])
                await asyncio.sleep(0.002)

    # 6 concurrent workers competing
    workers = [worker(f"reviewer_{i+1}") for i in range(6)]
    await asyncio.gather(*workers)

    assert len(claimed_ids) == 60, f"Expected 60 items claimed, got {len(claimed_ids)}"
    assert len(set(claimed_ids)) == 60, "Detected duplicate claims! Invariant violated."

    # Clean up
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items;")

    await close_db_pool()


@pytest.mark.asyncio
async def test_released_state_semantics_and_audit():
    """Verify that releasing an item resets it to PENDING and logs audit trail."""
    pool = await init_db_pool()

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

        item_id = "test_item_rel_01"
        await conn.execute(
            """
            INSERT INTO inspection_items (id, priority, status, payload)
            VALUES ($1, 10, 'PENDING', '{"part": "Weld"}');
            """,
            item_id
        )

        # Claim item
        claim_row = await conn.fetchrow(
            """
            WITH next_item AS (
                SELECT id FROM inspection_items
                WHERE status = 'PENDING'
                ORDER BY priority DESC, arrival_time ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            ),
            updated AS (
                UPDATE inspection_items
                SET status = 'CLAIMED',
                    claimed_by = 'rev_A',
                    claimed_at = NOW(),
                    lease_expires_at = NOW() + INTERVAL '8 seconds'
                FROM next_item
                WHERE inspection_items.id = next_item.id
                RETURNING inspection_items.*
            ),
            audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id)
                SELECT id, 'CLAIMED', 'rev_A' FROM updated
                RETURNING id
            )
            SELECT * FROM updated;
            """
        )
        assert claim_row is not None
        assert claim_row["status"] == "CLAIMED"
        assert claim_row["claimed_by"] == "rev_A"

        # Voluntary Release: status MUST return to PENDING with claimed_by = NULL
        rel_row = await conn.fetchrow(
            """
            WITH updated AS (
                UPDATE inspection_items
                SET status = 'PENDING',
                    claimed_by = NULL,
                    lease_expires_at = NULL
                WHERE id = $1 AND status = 'CLAIMED'
                RETURNING *
            ),
            audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id, metadata)
                SELECT id, 'RELEASED', 'rev_A', json_build_object('reason', 'manual') FROM updated
                RETURNING id
            )
            SELECT * FROM updated;
            """,
            item_id
        )
        assert rel_row is not None
        assert rel_row["status"] == "PENDING"
        assert rel_row["claimed_by"] is None

        # Another reviewer claims the released item
        reclaim_row = await conn.fetchrow(
            """
            WITH next_item AS (
                SELECT id FROM inspection_items
                WHERE status = 'PENDING'
                ORDER BY priority DESC, arrival_time ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            ),
            updated AS (
                UPDATE inspection_items
                SET status = 'CLAIMED',
                    claimed_by = 'rev_B',
                    claimed_at = NOW(),
                    lease_expires_at = NOW() + INTERVAL '8 seconds'
                FROM next_item
                WHERE inspection_items.id = next_item.id
                RETURNING inspection_items.*
            ),
            audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id)
                SELECT id, 'CLAIMED', 'rev_B' FROM updated
                RETURNING id
            )
            SELECT * FROM updated;
            """
        )
        assert reclaim_row is not None
        assert reclaim_row["id"] == item_id
        assert reclaim_row["claimed_by"] == "rev_B"

        # Audit events check
        events = await conn.fetch(
            "SELECT event_type, reviewer_id FROM audit_events WHERE item_id = $1 ORDER BY id ASC;",
            item_id
        )
        assert [e["event_type"] for e in events] == ["CLAIMED", "RELEASED", "CLAIMED"]

        # Clean up
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    await close_db_pool()
