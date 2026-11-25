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
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'PENDING' AND priority >= 9) AS pending_crit,
                    COUNT(*) FILTER (WHERE status = 'PENDING' AND priority >= 6 AND priority < 9) AS pending_high,
                    COUNT(*) FILTER (WHERE status = 'PENDING' AND priority < 6) AS pending_rout
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

            active_claims_rows = await conn.fetch(
                """
                SELECT id, priority, status, payload, arrival_time, claimed_by, claimed_at, lease_expires_at,
                       ROUND(EXTRACT(EPOCH FROM (lease_expires_at - NOW()))::numeric, 1) AS remaining_lease_seconds
                FROM inspection_items
                WHERE status = 'CLAIMED' AND lease_expires_at > NOW() AND claimed_by IS NOT NULL;
                """
            )
            active_claims = []
            for r in active_claims_rows:
                item_dict = dict(r)
                if isinstance(item_dict.get("payload"), str):
                    item_dict["payload"] = json.loads(item_dict["payload"])
                active_claims.append(item_dict)

            preview = await self.peek_next_item(conn=conn)

            return {
                "pending_count": counts["pending"] if counts else 0,
                "claimed_count": counts["claimed"] if counts else 0,
                "passed_count": counts["passed"] if counts else 0,
                "failed_count": counts["failed"] if counts else 0,
                "total_items": counts["total"] if counts else 0,
                "pending_crit": counts["pending_crit"] if counts else 0,
                "pending_high": counts["pending_high"] if counts else 0,
                "pending_rout": counts["pending_rout"] if counts else 0,
                "active_reviewers": [dict(r) for r in active_revs],
                "active_claims": active_claims,
                "next_preview": preview
            }

    async def renew_lease(
        self,
        item_id: str,
        reviewer_id: str,
        extension_seconds: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Extends the lease expiration for an actively claimed item (heartbeat).
        Guarantees only the reviewer actively holding the claim with an unexpired lease can renew it.
        """
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")

        duration = extension_seconds or self.lease_duration

        async with self.pool.acquire() as conn:
            renew_query = """
            WITH target_item AS (
                SELECT id FROM inspection_items
                WHERE id = $1 AND status = 'CLAIMED' AND claimed_by = $2 AND lease_expires_at > NOW()
                FOR UPDATE
            ),
            updated_item AS (
                UPDATE inspection_items
                SET lease_expires_at = NOW() + ($3 || ' seconds')::INTERVAL
                FROM target_item
                WHERE inspection_items.id = target_item.id
                RETURNING inspection_items.id, inspection_items.claimed_by,
                          inspection_items.lease_expires_at,
                          ROUND(EXTRACT(EPOCH FROM (inspection_items.lease_expires_at - NOW()))::numeric, 2) AS remaining_lease_seconds
            )
            SELECT * FROM updated_item;
            """
            row = await conn.fetchrow(renew_query, item_id, reviewer_id, str(duration))

            if not row:
                current = await conn.fetchrow(
                    "SELECT status, claimed_by, lease_expires_at FROM inspection_items WHERE id = $1;",
                    item_id
                )
                if not current:
                    raise ItemNotClaimedError(f"Item {item_id} does not exist.")
                if current["status"] != "CLAIMED":
                    raise ItemNotClaimedError(f"Item {item_id} is no longer claimed (status: {current['status']}).")
                if current["claimed_by"] != reviewer_id:
                    raise ItemNotClaimedError(f"Item {item_id} is claimed by {current['claimed_by']}, not {reviewer_id}.")
                if current["lease_expires_at"] and current["lease_expires_at"] <= datetime.now(timezone.utc):
                    raise ItemNotClaimedError(f"Lease for item {item_id} has already expired.")
                raise ItemNotClaimedError(f"Cannot renew lease for item {item_id}.")

            res = dict(row)
            logger.debug(f"Lease renewed for item {item_id} by {reviewer_id}: {res['remaining_lease_seconds']}s remaining.")
            return res

    async def sweep_expired_leases(self, batch_size: int = 100) -> List[Dict[str, Any]]:
        """
        Scans for items where status = 'CLAIMED' AND lease_expires_at < NOW().
        Atomically resets them to 'PENDING', clears claimed_by, and logs 'LEASE_EXPIRED' audit event.
        Returns list of recovered items.
        """
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")

        async with self.pool.acquire() as conn:
            sweep_query = """
            WITH expired_items AS (
                SELECT id, claimed_by, priority
                FROM inspection_items
                WHERE status = 'CLAIMED' AND lease_expires_at < NOW()
                ORDER BY lease_expires_at ASC
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            ),
            recovered_items AS (
                UPDATE inspection_items
                SET status = 'PENDING',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    lease_expires_at = NULL
                FROM expired_items
                WHERE inspection_items.id = expired_items.id
                RETURNING inspection_items.id, inspection_items.priority, expired_items.claimed_by
            ),
            logged_audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id, metadata)
                SELECT id, 'LEASE_EXPIRED', claimed_by, json_build_object(
                    'priority', priority,
                    'recovered_at', NOW()
                )
                FROM recovered_items
                RETURNING id
            )
            SELECT id, priority, claimed_by FROM recovered_items;
            """
            rows = await conn.fetch(sweep_query, batch_size)

        if rows:
            recovered = [dict(r) for r in rows]
            logger.warning(f"Sweeper recovered {len(recovered)} abandoned items: {[r['id'] for r in recovered]}")
            await self._notify_listeners("LEASE_RECOVERED", {"recovered_items": recovered, "count": len(recovered)})
            return recovered

        return []


class LeaseSweeper:
    """
    Background worker that runs periodic lease recovery sweeps.
    Ensures abandoned items return to the queue within the bounded time.
    """

    def __init__(self, engine: QueueEngine, interval_seconds: float = settings.sweeper_interval_seconds):
        self.engine = engine
        self.interval = interval_seconds
        self.is_running: bool = False
        self._task: Optional[asyncio.Task] = None
        self.total_sweeps: int = 0
        self.total_recovered: int = 0
        self.last_sweep_time: Optional[datetime] = None

    async def _sweep_loop(self):
        logger.info(f"Lease sweeper started. Running every {self.interval:.1f}s.")
        while self.is_running:
            try:
                # Continuous drain loop: if a full batch was recovered, drain immediately without sleeping
                while self.is_running:
                    recovered = await self.engine.sweep_expired_leases(batch_size=100)
                    self.total_sweeps += 1
                    self.last_sweep_time = datetime.now(timezone.utc)
                    if recovered:
                        self.total_recovered += len(recovered)
                    if len(recovered) < 100:
                        break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in lease sweeper iteration: {e}")

            await asyncio.sleep(self.interval)

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._task = asyncio.create_task(self._sweep_loop())
        logger.info("Lease sweeper background task launched.")

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Lease sweeper stopped.")

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "is_running": self.is_running,
            "interval_seconds": self.interval,
            "total_sweeps": self.total_sweeps,
            "total_recovered": self.total_recovered,
            "last_sweep_time": self.last_sweep_time.isoformat() if self.last_sweep_time else None
        }


# Singleton engine and sweeper instances
queue_engine = QueueEngine()
lease_sweeper_service = LeaseSweeper(queue_engine)
