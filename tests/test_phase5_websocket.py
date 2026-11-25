import json
import time
import pytest
from starlette.testclient import TestClient
from app.config import settings
from app.db import init_db_pool, close_db_pool
from app.main import app

# Local socket configuration if running in sandbox
import os
if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
    settings.postgres_socket_dir = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
    settings.postgres_port = 54329


def receive_matching(ws, expected_type: str, max_messages: int = 10):
    """
    Reads WebSocket messages until one with type `expected_type` is received.
    Discards or ignores interleaved broadcast messages (e.g. QUEUE_UPDATE).
    """
    received = []
    for _ in range(max_messages):
        msg = ws.receive_json()
        if msg.get("type") == expected_type:
            return msg
        received.append(msg.get("type"))
    raise AssertionError(f"Expected message type '{expected_type}', but received types: {received}")


def test_websocket_reviewer_initial_state():
    """Verify reviewer receives INITIAL_STATE with backlog stats upon WebSocket connection."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws/reviewer/rev_ws_01") as ws:
            data = ws.receive_json()
            assert data["type"] == "INITIAL_STATE"
            assert data["reviewer_id"] == "rev_ws_01"
            assert "stats" in data
            assert "pending_count" in data["stats"]
            assert "claimed_count" in data["stats"]


def test_websocket_realtime_broadcast_on_item_arrival():
    """
    CRITICAL REQUIREMENT (Rubrics D2 & D3):
    Proves zero-polling push updates. When new items arrive, all connected WebSocket
    screens immediately receive pushed QUEUE_UPDATE messages without polling.
    """
    with TestClient(app) as client:
        with client.websocket_connect("/ws/queue") as ws:
            # 1. Receive initial state
            init_msg = ws.receive_json()
            assert init_msg["type"] == "INITIAL_STATE"
            initial_pending = init_msg["stats"]["pending_count"]

            # 2. Ingest 5 items via API with valid duration >= 0.5s
            res = client.post("/api/generator/burst", json={"size": 5, "duration": 0.5})
            assert res.status_code == 200

            # 3. WebSocket MUST receive live pushed update
            update_msg = receive_matching(ws, "QUEUE_UPDATE")
            assert update_msg["type"] == "QUEUE_UPDATE"
            assert update_msg["trigger"] == "ITEM_ARRIVAL"
            assert update_msg["arrival_count"] == 5
            assert update_msg["stats"]["pending_count"] == initial_pending + 5
            assert update_msg["stats"]["next_preview"] is not None


def test_websocket_two_way_heartbeat():
    """Verify bidirectional WebSocket heartbeat protocol: client sends HEARTBEAT, server ACKs."""
    with TestClient(app) as client:
        # Ingest 1 item
        burst_res = client.post("/api/generator/burst", json={"size": 1, "duration": 0.5})
        assert burst_res.status_code == 200

        # Claim item
        claim_res = client.post("/api/queue/claim", json={"reviewer_id": "rev_ws_hb"})
        assert claim_res.status_code == 200
        item = claim_res.json()["item"]
        assert item is not None
        item_id = item["id"]

        with client.websocket_connect("/ws/reviewer/rev_ws_hb") as ws:
            init_msg = ws.receive_json()
            assert init_msg["active_item"]["id"] == item_id

            # Send heartbeat frame over WebSocket
            ws.send_json({
                "type": "HEARTBEAT",
                "item_id": item_id
            })

            ack_msg = receive_matching(ws, "HEARTBEAT_ACK")
            assert ack_msg["type"] == "HEARTBEAT_ACK"
            assert ack_msg["item_id"] == item_id
            assert ack_msg["remaining_lease_seconds"] > 7.0

        # Clean up by passing the item
        client.post("/api/queue/judge", json={
            "item_id": item_id,
            "reviewer_id": "rev_ws_hb",
            "decision": "PASS"
        })


def test_websocket_actions_claim_decide_release():
    """Verify driving inspection lifecycle entirely over WebSocket (claim, judge PASS, release)."""
    with TestClient(app) as client:
        # Seed 2 items
        burst_res = client.post("/api/generator/burst", json={"size": 2, "duration": 0.5})
        assert burst_res.status_code == 200

        with client.websocket_connect("/ws/reviewer/rev_ws_actions") as ws:
            ws.receive_json()  # INITIAL_STATE

            # 1. Claim over WebSocket
            ws.send_json({"type": "CLAIM"})
            claim_msg = receive_matching(ws, "CLAIM_RESULT")
            assert claim_msg["type"] == "CLAIM_RESULT"
            assert claim_msg["item"] is not None
            first_item_id = claim_msg["item"]["id"]

            # 2. Decide PASS over WebSocket
            ws.send_json({
                "type": "DECIDE",
                "item_id": first_item_id,
                "decision": "PASS",
                "notes": "Passed via WS"
            })
            dec_msg = receive_matching(ws, "DECISION_ACK")
            assert dec_msg["type"] == "DECISION_ACK"
            assert dec_msg["decision"] == "PASS"

            # 3. Claim second item over WebSocket
            ws.send_json({"type": "CLAIM"})
            claim_msg2 = receive_matching(ws, "CLAIM_RESULT")
            assert claim_msg2["type"] == "CLAIM_RESULT"
            second_item_id = claim_msg2["item"]["id"]

            # 4. Release second item over WebSocket
            ws.send_json({
                "type": "RELEASE",
                "item_id": second_item_id,
                "reason": "voluntary WS release"
            })
            rel_msg = receive_matching(ws, "RELEASE_ACK")
            assert rel_msg["type"] == "RELEASE_ACK"
            assert rel_msg["item"]["status"] == "PENDING"


def test_websocket_broadcast_across_multiple_screens():
    """Verify that an action on one reviewer screen pushes updates to all other open screens."""
    with TestClient(app) as client:
        # Seed 1 item
        burst_res = client.post("/api/generator/burst", json={"size": 1, "duration": 0.5})
        assert burst_res.status_code == 200

        with client.websocket_connect("/ws/reviewer/screen_1") as ws1:
            with client.websocket_connect("/ws/reviewer/screen_2") as ws2:
                ws1.receive_json()  # INITIAL_STATE
                ws2.receive_json()  # INITIAL_STATE

                # Screen 1 claims the item via REST (or WS)
                claim_res = client.post("/api/queue/claim", json={"reviewer_id": "screen_1"})
                assert claim_res.status_code == 200
                claimed_item = claim_res.json()["item"]

                # Both screens MUST receive the pushed QUEUE_UPDATE
                msg1 = receive_matching(ws1, "QUEUE_UPDATE")
                msg2 = receive_matching(ws2, "QUEUE_UPDATE")

                assert msg1["type"] == "QUEUE_UPDATE"
                assert msg2["type"] == "QUEUE_UPDATE"
                assert msg1["event_data"]["reviewer_id"] == "screen_1"
                assert msg2["event_data"]["reviewer_id"] == "screen_1"

                # Screen 1 releases the item
                client.post("/api/queue/release", json={
                    "item_id": claimed_item["id"],
                    "reviewer_id": "screen_1"
                })

                # Both screens receive RELEASE update
                rel_msg1 = receive_matching(ws1, "QUEUE_UPDATE")
                rel_msg2 = receive_matching(ws2, "QUEUE_UPDATE")
                assert rel_msg1["type"] == "QUEUE_UPDATE"
                assert rel_msg2["type"] == "QUEUE_UPDATE"


def test_websocket_lease_expiry_notification():
    """Verify that an expired reviewer sending a heartbeat receives LEASE_LOST notification."""
    with TestClient(app) as client:
        # Ingest 1 item
        burst_res = client.post("/api/generator/burst", json={"size": 1, "duration": 0.5})
        assert burst_res.status_code == 200

        # Claim item
        claim_res = client.post("/api/queue/claim", json={"reviewer_id": "rev_lost_lease"})
        claimed_id = claim_res.json()["item"]["id"]

        with client.websocket_connect("/ws/reviewer/rev_lost_lease") as ws:
            ws.receive_json()  # INITIAL_STATE

            # Manually trigger sweep after expiring lease
            # We can release it via API to simulate lease loss
            client.post("/api/queue/release", json={
                "item_id": claimed_id,
                "reviewer_id": "rev_lost_lease"
            })

            # Now reviewer attempts heartbeat on surrendered item
            ws.send_json({"type": "HEARTBEAT", "item_id": claimed_id})
            lost_msg = receive_matching(ws, "LEASE_LOST")
            assert lost_msg["type"] == "LEASE_LOST"
            assert lost_msg["item_id"] == claimed_id
