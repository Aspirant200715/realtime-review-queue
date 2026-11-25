import asyncio
import json
import logging
from datetime import datetime, date
import decimal
from typing import Dict, Set, Any, Optional
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("review_queue.websocket")


def serialize_json(obj: Any) -> str:
    """Serializes objects to JSON, handling datetime and Decimal types."""
    def default_encoder(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, decimal.Decimal):
            return float(o)
        return str(o)
    return json.dumps(obj, default=default_encoder)


class ConnectionManager:
    """
    Manages active WebSocket connections for reviewer screens and telemetry monitors.
    Provides push-based broadcasting for queue backlog updates, item transitions,
    and client-specific lease heartbeats.
    """

    def __init__(self):
        # Maps client_id (reviewer_id or 'monitor') to a set of active WebSockets
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._seq_counter: int = 0

    def get_next_seq(self) -> int:
        """Returns a monotonically increasing sequence ID for broadcast ordering."""
        self._seq_counter += 1
        return self._seq_counter

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        async with self._lock:
            if client_id not in self.active_connections:
                self.active_connections[client_id] = set()
            self.active_connections[client_id].add(websocket)
        logger.info(f"WebSocket client connected: {client_id} (Total clients: {self.total_connections})")

    async def disconnect(self, websocket: WebSocket, client_id: str):
        async with self._lock:
            if client_id in self.active_connections:
                self.active_connections[client_id].discard(websocket)
                if not self.active_connections[client_id]:
                    del self.active_connections[client_id]
        logger.info(f"WebSocket client disconnected: {client_id} (Total clients: {self.total_connections})")

    async def _prune_socket(self, ws: WebSocket):
        """Removes a dead or unresponsive socket across all registered client mappings."""
        async with self._lock:
            empty_keys = []
            for client_id, s_set in self.active_connections.items():
                if ws in s_set:
                    s_set.discard(ws)
                    if not s_set:
                        empty_keys.append(client_id)
            for k in empty_keys:
                del self.active_connections[k]

    @property
    def total_connections(self) -> int:
        return sum(len(conns) for conns in self.active_connections.values())

    async def send_personal_message(self, message: Dict[str, Any], client_id: str):
        """Sends a JSON payload to all active screens of a specific reviewer."""
        async with self._lock:
            sockets = list(self.active_connections.get(client_id, []))

        text_data = serialize_json(message)
        for ws in sockets:
            try:
                await asyncio.wait_for(ws.send_text(text_data), timeout=1.0)
            except Exception as e:
                logger.warning(f"Error sending message to {client_id}, pruning socket: {e}")
                await self._prune_socket(ws)

    async def broadcast(self, message: Dict[str, Any]):
        """Broadcasts a JSON payload to all connected screens simultaneously."""
        all_sockets = []
        async with self._lock:
            for s_set in self.active_connections.values():
                all_sockets.extend(s_set)

        if not all_sockets:
            return

        text_data = serialize_json(message)
        # Fan out push to all connected screens concurrently with timeout protection
        await asyncio.gather(
            *[self._safe_send(ws, text_data) for ws in all_sockets],
            return_exceptions=True
        )

    async def _safe_send(self, ws: WebSocket, data: str):
        try:
            await asyncio.wait_for(ws.send_text(data), timeout=1.0)
        except Exception as e:
            logger.debug(f"Failed to send to socket, pruning: {e}")
            await self._prune_socket(ws)


ws_manager = ConnectionManager()
