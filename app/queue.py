import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Callable, Tuple
import asyncpg
from app.config import settings

logger = logging.getLogger("review_queue.engine")


class QueueError(Exception):
    """Base exception for queue operations."""
    pass


class ItemNotClaimedError(QueueError):
    """Raised when an operation targets an item not claimed by the requesting reviewer."""
    pass


class ReviewerBusyError(QueueError):
    """Raised when a reviewer already holds an active claim."""
    pass


class QueueEngine:
    """
    Core Queue Engine managing atomic claims, decisions, and queue metrics.
    
    Guarantees:
    - Atomicity: Handout fetch and state transition occur within a single SQL statement.
    - Exactly-Once: PostgreSQL SKIP LOCKED eliminates row-lock contention and double-handouts.
    - Reviewer Integrity: Reviewers can hold at most one active item at a time.
    - Stale Guard: Decisions/releases are rejected if the reviewer's lease expired.
    """

    def __init__(self, pool: Optional[asyncpg.Pool] = None, lease_duration: float = settings.lease_duration_seconds):
        self.pool = pool
        self.lease_duration = lease_duration
        self.listeners: List[Callable[[str, Dict[str, Any]], Any]] = []

    def set_pool(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    def register_listener(self, listener: Callable[[str, Dict[str, Any]], Any]) -> None:
        """Register callback for queue state changes (claim, judge, release)."""
        self.listeners.append(listener)

    async def _notify_listeners(self, event_type: str, data: Dict[str, Any]) -> None:
        for listener in self.listeners:
            try:
                res = listener(event_type, data)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Error in queue engine listener ({event_type}): {e}")

    async def get_active_claim_for_reviewer(self, reviewer_id: str, conn: Optional[asyncpg.Connection] = None) -> Optional[Dict[str, Any]]:
        """
        Checks if the reviewer already holds an active, unexpired claim.
        Returns the item dict if found, None otherwise.
        """
        query = """
        SELECT id, priority, status, payload, arrival_time, claimed_by, claimed_at,
               lease_expires_at,
               ROUND(EXTRACT(EPOCH FROM (lease_expires_at - NOW()))::numeric, 2) AS remaining_lease_seconds
        FROM inspection_items
        WHERE claimed_by = $1 AND status = 'CLAIMED' AND lease_expires_at > NOW()
        ORDER BY claimed_at DESC
        LIMIT 1;
        """
        if conn:
            row = await conn.fetchrow(query, reviewer_id)
        else:
            if not self.pool:
                raise RuntimeError("Database pool not initialized.")
            async with self.pool.acquire() as c:
                row = await c.fetchrow(query, reviewer_id)

        if row:
            item = dict(row)
            if isinstance(item.get("payload"), str):
                item["payload"] = json.loads(item["payload"])
            return item
        return None

    async def peek_next_item(self, conn: Optional[asyncpg.Connection] = None) -> Optional[Dict[str, Any]]:
        """
        Peeks at the highest-priority pending item without locking or claiming it.
        Used to display 'next item preview' on live screens.
        """
        query = """
        SELECT id, priority, status, payload, arrival_time
        FROM inspection_items
        WHERE status = 'PENDING'
        ORDER BY priority DESC, arrival_time ASC
        LIMIT 1;
        """
        if conn:
            row = await conn.fetchrow(query)
        else:
            if not self.pool:
                raise RuntimeError("Database pool not initialized.")
            async with self.pool.acquire() as c:
                row = await c.fetchrow(query)

        if row:
            item = dict(row)
            if isinstance(item.get("payload"), str):
                item["payload"] = json.loads(item["payload"])
            return item
        return None

    async def claim_next_item(self, reviewer_id: str) -> Tuple[Optional[Dict[str, Any]], bool]:
        """
        Atomically claims the highest priority pending item for the reviewer.
        
        Guarantees:
        - Exactly-once handout via SELECT ... FOR UPDATE SKIP LOCKED.
        - Strict single-active-item per reviewer: Immune to TOCTOU race conditions
          even if the same reviewer makes simultaneous concurrent claim requests.
        
        Returns:
            Tuple[item_dict, already_had_active_claim]
            - If reviewer already holds an active item, returns (active_item, True).
            - If new claim succeeds, returns (claimed_item, False).
            - If queue is empty, returns (None, False).
        """
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")

        reviewer_id = reviewer_id.strip()
        if not reviewer_id:
            raise ValueError("Reviewer ID cannot be empty.")

        async with self.pool.acquire() as conn:
            # 1. Fast pre-check: if reviewer already holds an active claim, return it
            active = await self.get_active_claim_for_reviewer(reviewer_id, conn=conn)
            if active:
                logger.info(f"Reviewer {reviewer_id} already holds active claim {active['id']}.")
                return active, True

            # 2. Atomic claim via SELECT ... FOR UPDATE SKIP LOCKED CTE
            # Includes reviewer_active CTE guard so simultaneous requests by the same reviewer
            # never assign two items.
            claim_query = """
            WITH reviewer_active AS (
                SELECT id FROM inspection_items
                WHERE claimed_by = $1 AND status = 'CLAIMED' AND lease_expires_at > NOW()
                LIMIT 1
            ),
            next_item AS (
                SELECT id FROM inspection_items
                WHERE status = 'PENDING'
                  AND NOT EXISTS (SELECT 1 FROM reviewer_active)
                ORDER BY priority DESC, arrival_time ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            ),
            updated_item AS (
                UPDATE inspection_items
                SET status = 'CLAIMED',
                    claimed_by = $1,
                    claimed_at = NOW(),
                    lease_expires_at = NOW() + ($2 || ' seconds')::INTERVAL
                FROM next_item
                WHERE inspection_items.id = next_item.id
                RETURNING inspection_items.id, inspection_items.priority, inspection_items.status,
                          inspection_items.payload, inspection_items.arrival_time,
                          inspection_items.claimed_by, inspection_items.claimed_at,
                          inspection_items.lease_expires_at
            ),
            logged_audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id, metadata)
                SELECT id, 'CLAIMED', $1, json_build_object(
                    'priority', priority,
                    'lease_duration', $2::text
                )
                FROM updated_item
                RETURNING id
            )
            SELECT *, ROUND(EXTRACT(EPOCH FROM (lease_expires_at - NOW()))::numeric, 2) AS remaining_lease_seconds
            FROM updated_item;
            """
            try:
                row = await conn.fetchrow(claim_query, reviewer_id, str(self.lease_duration))
            except asyncpg.exceptions.UniqueViolationError:
                # Concurrent claim from the same reviewer committed at the exact same instant
                active = await self.get_active_claim_for_reviewer(reviewer_id, conn=conn)
                if active:
                    return active, True
                return None, False

            if not row:
                # Either queue is empty, or reviewer already had an active claim committed concurrently
                active = await self.get_active_claim_for_reviewer(reviewer_id, conn=conn)
                if active:
                    return active, True
                return None, False

            item = dict(row)
            if isinstance(item.get("payload"), str):
                item["payload"] = json.loads(item["payload"])

            logger.info(f"Item {item['id']} (Priority {item['priority']}) atomically claimed by {reviewer_id}.")
            await self._notify_listeners("CLAIM", {"item": item, "reviewer_id": reviewer_id})
            return item, False

    async def record_decision(
        self,
        item_id: str,
        reviewer_id: str,
        decision: str,
        notes: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Records a PASS or FAIL judgement on an actively claimed item.
        Guarantees that only the reviewer currently holding the active lease can record the decision.
        """
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")

        decision = decision.upper().strip()
        if decision not in ("PASS", "FAIL"):
            raise ValueError(f"Invalid decision '{decision}'. Must be PASS or FAIL.")

        status_value = "PASSED" if decision == "PASS" else "FAILED"

        async with self.pool.acquire() as conn:
            decision_query = """
            WITH target_item AS (
                SELECT id FROM inspection_items
                WHERE id = $1 AND status = 'CLAIMED' AND claimed_by = $2 AND lease_expires_at > NOW()
                FOR UPDATE
            ),
            updated_item AS (
                UPDATE inspection_items
                SET status = $3,
                    decision = $4,
                    notes = $5,
                    decided_at = NOW(),
                    lease_expires_at = NULL
                FROM target_item
                WHERE inspection_items.id = target_item.id
                RETURNING inspection_items.*
            ),
            logged_audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id, metadata)
                SELECT id, $3, $2, json_build_object('decision', $4::text, 'notes', $5::text)
                FROM updated_item
                RETURNING id
            )
            SELECT * FROM updated_item;
            """
            row = await conn.fetchrow(decision_query, item_id, reviewer_id, status_value, decision, notes)

            if not row:
                # Determine reason for failure (stale lease, wrong reviewer, or already decided)
                current = await conn.fetchrow("SELECT status, claimed_by, lease_expires_at FROM inspection_items WHERE id = $1;", item_id)
                if not current:
                    raise ItemNotClaimedError(f"Item {item_id} does not exist.")
                if current["status"] != "CLAIMED":
                    raise ItemNotClaimedError(f"Item {item_id} is no longer claimed (current status: {current['status']}).")
                if current["claimed_by"] != reviewer_id:
                    raise ItemNotClaimedError(f"Item {item_id} is claimed by {current['claimed_by']}, not {reviewer_id}.")
                if current["lease_expires_at"] and current["lease_expires_at"] <= datetime.now(timezone.utc):
                    raise ItemNotClaimedError(f"Lease for item {item_id} expired before decision was submitted.")
                raise ItemNotClaimedError(f"Cannot record decision on item {item_id}.")

            item = dict(row)
            if isinstance(item.get("payload"), str):
                item["payload"] = json.loads(item["payload"])

            logger.info(f"Item {item_id} judged {decision} by {reviewer_id}.")
            await self._notify_listeners("DECISION", {"item": item, "reviewer_id": reviewer_id, "decision": decision})
            return item

    async def release_item(self, item_id: str, reviewer_id: str, reason: str = "voluntary_release") -> Dict[str, Any]:
        """
        Voluntarily releases a claimed item back to the queue (status returns to PENDING).
        Guarantees that only the reviewer currently holding the active lease can release it.
        """
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")

        async with self.pool.acquire() as conn:
            release_query = """
            WITH target_item AS (
                SELECT id FROM inspection_items
                WHERE id = $1 AND status = 'CLAIMED' AND claimed_by = $2 AND lease_expires_at > NOW()
                FOR UPDATE
            ),
            updated_item AS (
                UPDATE inspection_items
                SET status = 'PENDING',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    lease_expires_at = NULL
                FROM target_item
                WHERE inspection_items.id = target_item.id
                RETURNING inspection_items.*
            ),
            logged_audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id, metadata)
                SELECT id, 'RELEASED', $2, json_build_object('reason', $3::text)
                FROM updated_item
                RETURNING id
            )
            SELECT * FROM updated_item;
            """
            row = await conn.fetchrow(release_query, item_id, reviewer_id, reason)

            if not row:
                current = await conn.fetchrow("SELECT status, claimed_by, lease_expires_at FROM inspection_items WHERE id = $1;", item_id)
                if not current:
                    raise ItemNotClaimedError(f"Item {item_id} does not exist.")
                if current["status"] != "CLAIMED":
                    raise ItemNotClaimedError(f"Item {item_id} is not claimed (current status: {current['status']}).")
                if current["claimed_by"] != reviewer_id:
                    raise ItemNotClaimedError(f"Item {item_id} is claimed by {current['claimed_by']}, not {reviewer_id}.")
                if current["lease_expires_at"] and current["lease_expires_at"] <= datetime.now(timezone.utc):
                    raise ItemNotClaimedError(f"Cannot release item {item_id}: lease has expired.")
                raise ItemNotClaimedError(f"Cannot release item {item_id}.")

            item = dict(row)
            if isinstance(item.get("payload"), str):
                item["payload"] = json.loads(item["payload"])

            logger.info(f"Item {item_id} voluntarily released by {reviewer_id}. Returned to PENDING queue.")
            await self._notify_listeners("RELEASE", {"item": item, "reviewer_id": reviewer_id})
            return item

    async def get_queue_stats(self) -> Dict[str, Any]:
        """
        Returns full queue telemetry:
        - pending_count: items waiting for review
        - claimed_count: items currently with reviewers
        - passed_count: items judged PASS
        - failed_count: items judged FAIL
        - total_items: sum of all items in table
        - next_preview: preview of top pending item
        - active_reviewers: list of reviewers currently holding an active lease
        """
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")

        async with self.pool.acquire() as conn:
            counts = await conn.fetchrow(
                """
                SELECT 
                    COUNT(*) FILTER (WHERE status = 'PENDING') AS pending,
                    COUNT(*) FILTER (WHERE status = 'CLAIMED' AND lease_expires_at > NOW()) AS claimed,
                    COUNT(*) FILTER (WHERE status = 'PASSED') AS passed,
                    COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
                    COUNT(*) AS total
                FROM inspection_items;
                """
            )

            active_revs = await conn.fetch(
                """
                SELECT DISTINCT claimed_by, COUNT(*) as items_held
                FROM inspection_items
                WHERE status = 'CLAIMED' AND lease_expires_at > NOW() AND claimed_by IS NOT NULL
                GROUP BY claimed_by;
                """
            )

            preview = await self.peek_next_item(conn=conn)

            return {
                "pending_count": counts["pending"] if counts else 0,
                "claimed_count": counts["claimed"] if counts else 0,
                "passed_count": counts["passed"] if counts else 0,
                "failed_count": counts["failed"] if counts else 0,
                "total_items": counts["total"] if counts else 0,
                "active_reviewers": [dict(r) for r in active_revs],
                "next_preview": preview
            }


# Singleton engine instance
queue_engine = QueueEngine()
