import pytest
from starlette.testclient import TestClient
from app.main import app


def test_frontend_index_serving():
    """Verify root / serves static index.html with appropriate titles and DOM anchors."""
    with TestClient(app) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        text = res.text
        assert "REALTIME REVIEW QUEUE" in text
        assert "stat-pending" in text
        assert "btn-view-grid" in text
        assert "reviewer-select" in text


def test_frontend_static_assets_serving():
    """Verify static assets style.css and app.js are served correctly."""
    with TestClient(app) as client:
        # 1. CSS
        css_res = client.get("/static/style.css")
        assert css_res.status_code == 200
        assert "text/css" in css_res.headers["content-type"]
        assert "--accent-cyan" in css_res.text

        # 2. JS
        js_res = client.get("/static/app.js")
        assert js_res.status_code == 200
        assert "javascript" in js_res.headers["content-type"]
        assert "handleServerMessage" in js_res.text
        assert "HEARTBEAT" in js_res.text
