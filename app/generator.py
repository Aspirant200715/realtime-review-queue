import asyncio
import json
import logging
import random
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Callable
import asyncpg
from app.config import settings

logger = logging.getLogger("review_queue.generator")

# Inspection defect domain dictionaries
COMPONENTS = [
    "Turbine Blade",
    "Hydraulic Seal",
    "Circuit Trace",
    "Fuel Injection Nozzle",
    "Bearing Assembly",
    "Pressure Flange",
    "Optical Sensor Housing",
    "Structural Strut"
]

CRITICAL_DEFECTS = [
    "Structural Fracture",
    "Through-Wall Porosity",
    "Subsurface Delamination",
    "Severe Thermal Distortion"
]

HIGH_DEFECTS = [
    "Weld Undercut",
    "Incomplete Joint Fusion",
    "Micro-Crack Cluster",
    "Tolerance Drift (>0.3mm)"
]

ROUTINE_DEFECTS = [
    "Surface Scuff",
    "Minor Burr",
    "Coating Thickness Variance",
    "Parting Line Flash",
    "Slight Discoloration"
]

CAMERAS = ["CAM-ALPHA-01", "CAM-BETA-02", "CAM-GAMMA-03", "CAM-DELTA-04"]


class ItemFactory:
    """Produces realistic inspection item records with controlled priority spreads."""

    _counter: int = 0

    @classmethod
    def create_item(cls) -> Dict[str, Any]:
        cls._counter += 1
        now = datetime.now(timezone.utc)
        timestamp_ms = int(now.timestamp() * 1000)
        item_id = f"item_{timestamp_ms}_{cls._counter:06d}_{random.randint(1000, 9999)}"

        # Priority spread:
        # 10% Critical (9 - 10)
        # 25% High (6 - 8)
        # 65% Routine (1 - 5)
        roll = random.random()
        if roll < 0.10:
            priority = random.choice([9, 10])
            category = "CRITICAL"
            defect_type = random.choice(CRITICAL_DEFECTS)
            confidence = round(random.uniform(0.85, 0.99), 3)
        elif roll < 0.35:
            priority = random.choice([6, 7, 8])
            category = "HIGH"
            defect_type = random.choice(HIGH_DEFECTS)
            confidence = round(random.uniform(0.70, 0.92), 3)
        else:
            priority = random.choice([1, 2, 3, 4, 5])
            category = "ROUTINE"
            defect_type = random.choice(ROUTINE_DEFECTS)
            confidence = round(random.uniform(0.50, 0.78), 3)

        component = random.choice(COMPONENTS)
        camera = random.choice(CAMERAS)

        payload = {
            "part_number": f"PART-{random.randint(1000, 9999)}-{chr(random.randint(65, 90))}",
            "component": component,
            "category": category,
            "defect_type": defect_type,
            "confidence_score": confidence,
            "camera_id": camera,
            "dimensions_mm": {
                "width": round(random.uniform(1.0, 50.0), 2),
                "height": round(random.uniform(0.5, 30.0), 2),
                "depth": round(random.uniform(0.1, 10.0), 2)
            },
            "inspection_stage": "Stage-2 Human Verification"
        }

        return {
            "id": item_id,
            "priority": priority,
            "payload": payload,
            "arrival_time": now
        }


class LoadGenerator:
    """
    Manages background load generation:
    1. Steady background rate of ~5 items/second with realistic jitter.
    2. Unpredictable bursts of ~250 items in <10 seconds.
    3. Metrics tracking and listener callbacks for real-time WebSocket push.
    """

    def __init__(
        self,
        pool: Optional[asyncpg.Pool] = None,
        steady_rate: float = settings.generator_steady_rate,
        burst_size: int = settings.generator_burst_size,
        burst_duration: float = settings.generator_burst_duration,
        min_burst_interval: float = settings.generator_min_burst_interval,
        max_burst_interval: float = settings.generator_max_burst_interval,
    ):
        self.pool = pool
        self.steady_rate = steady_rate
        self.burst_size = burst_size
        self.burst_duration = burst_duration
        self.min_burst_interval = min_burst_interval
        self.max_burst_interval = max_burst_interval

        self.is_running: bool = False
        self.steady_enabled: bool = True
        self._steady_task: Optional[asyncio.Task] = None
        self._burst_task: Optional[asyncio.Task] = None

        # Metrics
        self.total_generated: int = 0
        self.total_bursts: int = 0
        self.last_burst_time: Optional[datetime] = None
        self.last_burst_metrics: Optional[Dict[str, Any]] = None
        self._recent_arrivals: deque = deque(maxlen=200)

        # Broadcast hooks (called after items are inserted into DB)
        self.listeners: List[Callable[[List[Dict[str, Any]]], Any]] = []

    def set_pool(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    def register_listener(self, listener: Callable[[List[Dict[str, Any]]], Any]) -> None:
        self.listeners.append(listener)

    async def _notify_listeners(self, items: List[Dict[str, Any]]) -> None:
        for listener in self.listeners:
            try:
                res = listener(items)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Error in generator listener: {e}")

    async def insert_items(self, items: List[Dict[str, Any]]) -> None:
        """Atomically inserts items and CREATED audit records into PostgreSQL."""
        if not items:
            return

        if not self.pool:
            # Standalone or dry-run mode
            self.total_generated += len(items)
            now_ts = time.time()
            for _ in items:
                self._recent_arrivals.append(now_ts)
            await self._notify_listeners(items)
            return

        item_rows = [
            (
                item["id"],
                item["priority"],
                json.dumps(item["payload"]),
                item["arrival_time"]
            )
            for item in items
        ]

        audit_rows = [
            (
                item["id"],
                "CREATED",
                json.dumps({"priority": item["priority"], "category": item["payload"].get("category")})
            )
            for item in items
        ]

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO inspection_items (id, priority, status, payload, arrival_time)
                    VALUES ($1, $2, 'PENDING', $3::jsonb, $4);
                    """,
                    item_rows
                )
                await conn.executemany(
                    """
                    INSERT INTO audit_events (item_id, event_type, metadata)
                    VALUES ($1, $2, $3::jsonb);
                    """,
                    audit_rows
                )

        self.total_generated += len(items)
        now_ts = time.time()
        for _ in items:
            self._recent_arrivals.append(now_ts)

        await self._notify_listeners(items)

    async def trigger_burst(
        self,
        size: Optional[int] = None,
        duration: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Emits a burst of inspection items.
        Default: 250 items dumped in under 10 seconds (default 8.0s = ~31 items/sec).
        """
        count = size or self.burst_size
        target_duration = duration or self.burst_duration

        logger.info(f"Triggering burst: {count} items over {target_duration:.2f} seconds...")
        start_time = time.perf_counter()

        # We dump items in micro-batches (e.g., 5 items every ~160ms)
        # to simulate high-frequency realistic arrival floods
        batch_size = 5
        num_batches = (count + batch_size - 1) // batch_size
        delay_per_batch = target_duration / num_batches if num_batches > 0 else 0

        items_emitted = 0
        for b in range(num_batches):
            current_batch_size = min(batch_size, count - items_emitted)
            batch = [ItemFactory.create_item() for _ in range(current_batch_size)]
            await self.insert_items(batch)
            items_emitted += current_batch_size

            # Sleep between micro-batches to shape the duration
            if b < num_batches - 1 and delay_per_batch > 0:
                await asyncio.sleep(delay_per_batch)

        elapsed = time.perf_counter() - start_time
        rate = items_emitted / elapsed if elapsed > 0 else 0
        self.total_bursts += 1
        self.last_burst_time = datetime.now(timezone.utc)

        metrics = {
            "burst_id": self.total_bursts,
            "items_generated": items_emitted,
            "elapsed_seconds": round(elapsed, 3),
            "effective_rate_per_sec": round(rate, 2),
            "target_duration_seconds": target_duration,
            "completed_in_under_10s": elapsed < 10.0,
            "timestamp": self.last_burst_time.isoformat()
        }
        self.last_burst_metrics = metrics
        logger.info(f"Burst completed: {items_emitted} items in {elapsed:.3f}s ({rate:.1f} items/s). Under 10s: {elapsed < 10.0}")
        return metrics

    async def _steady_loop(self) -> None:
        """Maintains the continuous steady background trickle (~5 items/second)."""
        logger.info(f"Steady load generator started at target rate: {self.steady_rate} items/s.")
        base_interval = 1.0 / self.steady_rate

        while self.is_running:
            if self.steady_enabled:
                try:
                    item = ItemFactory.create_item()
                    await self.insert_items([item])
                except Exception as e:
                    logger.error(f"Error in steady generator iteration: {e}")

            # Jitter: Gaussian/Uniform distribution around base interval
            jitter = random.uniform(0.85, 1.15)
            sleep_time = max(0.05, base_interval * jitter)
            await asyncio.sleep(sleep_time)

    async def _burst_scheduler_loop(self) -> None:
        """Schedules bursts at unpredictable intervals during a run."""
        logger.info(f"Burst scheduler started (intervals between {self.min_burst_interval}s and {self.max_burst_interval}s).")
        while self.is_running:
            # Unpredictable interval between bursts
            wait_time = random.uniform(self.min_burst_interval, self.max_burst_interval)
            await asyncio.sleep(wait_time)

            if self.is_running:
                logger.info(f"Scheduled unpredictable burst firing after {wait_time:.1f}s calm period...")
                try:
                    await self.trigger_burst()
                except Exception as e:
                    logger.error(f"Error executing scheduled burst: {e}")

    def start(self) -> None:
        """Starts the generator background tasks."""
        if self.is_running:
            return
        self.is_running = True
        self._steady_task = asyncio.create_task(self._steady_loop())
        self._burst_task = asyncio.create_task(self._burst_scheduler_loop())
        logger.info("LoadGenerator background loops successfully launched.")

    def stop(self) -> None:
        """Stops all background generator tasks."""
        self.is_running = False
        if self._steady_task:
            self._steady_task.cancel()
            self._steady_task = None
        if self._burst_task:
            self._burst_task.cancel()
            self._burst_task = None
        logger.info("LoadGenerator stopped.")

    def get_metrics(self) -> Dict[str, Any]:
        """Calculates current rolling ingestion metrics."""
        now = time.time()
        # Compute arrival rate over the last 5 seconds
        recent = [t for t in self._recent_arrivals if now - t <= 5.0]
        current_rate = len(recent) / 5.0

        return {
            "is_running": self.is_running,
            "steady_enabled": self.steady_enabled,
            "current_rate_per_sec": round(current_rate, 2),
            "target_steady_rate": self.steady_rate,
            "total_items_generated": self.total_generated,
            "total_bursts_triggered": self.total_bursts,
            "last_burst_timestamp": self.last_burst_time.isoformat() if self.last_burst_time else None,
            "last_burst_metrics": self.last_burst_metrics
        }


# Singleton generator instance for application lifespan
generator_service = LoadGenerator()
