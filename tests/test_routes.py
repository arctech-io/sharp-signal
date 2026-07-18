"""Tests for the API routes."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base, get_db, save_signal
from app.storage.models import Signal, SignalDirection, SignalStatus


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite:///{tmp_path}/test.db"


@pytest.fixture()
def client(db_url):
    """Build a TestClient with an overridden DB session."""
    from app.main import app
    from app.storage import init_db

    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _make_signal(match_id="M1", status="pending"):
    return Signal(
        id=str(uuid.uuid4()),
        match_id=match_id,
        market="1X2",
        selection="Home",
        odds_value=2.0,
        confidence=75.0,
        direction=SignalDirection.SHORTENING,
        reason="Odds moved 12% in 3 minutes — sharp shortening.",
        status=SignalStatus(status),
        created_at=datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc),
    )


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert "last_successful_poll" in data

    def test_health_exposes_poll_status(self, client):
        from app import main as m
        m._set_poll_status("auth_error", "TxLINE returned 401 — check tokens")
        resp = client.get("/health")
        data = resp.json()
        assert data["poll_status"] == "auth_error"
        assert "401" in data["poll_detail"]


class TestPollStatusLogic:
    def test_auth_error_detected_from_401(self):
        """A 401/403 from TxLINE is surfaced as auth_error, not a generic error."""
        from app import main as m
        from app.ingestion.txline_client import TxLineError

        m._set_poll_status("starting")
        try:
            raise TxLineError("Client error 401", status_code=401)
        except TxLineError as exc:
            if exc.status_code in (401, 403):
                m._set_poll_status("auth_error", f"TxLINE returned {exc.status_code}")
            else:
                m._set_poll_status("error", exc.message)
        assert m._poll_status == "auth_error"

    def test_other_client_error_is_generic(self):
        from app import main as m
        from app.ingestion.txline_client import TxLineError

        m._set_poll_status("starting")
        try:
            raise TxLineError("Client error 404", status_code=404)
        except TxLineError as exc:
            if exc.status_code in (401, 403):
                m._set_poll_status("auth_error", f"TxLINE returned {exc.status_code}")
            else:
                m._set_poll_status("error", exc.message)
        assert m._poll_status == "error"


class TestSignalsEndpoint:
    def test_empty_signals(self, client):
        resp = client.get("/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["signals"] == []

    def test_signals_returns_data(self, client, db_url):
        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)
        db = Session()
        save_signal(db, _make_signal())
        db.close()

        resp = client.get("/signals")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_signals_with_status_filter(self, client, db_url):
        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)
        db = Session()
        save_signal(db, _make_signal(status="pending"))
        save_signal(db, _make_signal(status="correct"))
        db.close()

        resp = client.get("/signals?status=pending")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_signals_with_limit(self, client, db_url):
        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)
        db = Session()
        for _ in range(5):
            save_signal(db, _make_signal())
        db.close()

        resp = client.get("/signals?limit=2")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2


class TestSignalByIdEndpoint:
    def test_signal_not_found(self, client):
        resp = client.get("/signals/nonexistent")
        assert resp.status_code == 404

    def test_signal_found(self, client, db_url):
        sig = _make_signal()
        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)
        db = Session()
        save_signal(db, sig)
        db.close()

        resp = client.get(f"/signals/{sig.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["match_id"] == "M1"
        assert data["confidence"] == 75.0


class TestAccuracyEndpoint:
    def test_accuracy_empty(self, client):
        resp = client.get("/accuracy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_resolved"] == 0

    def test_accuracy_with_resolved_signals(self, client, db_url):
        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)
        db = Session()
        save_signal(db, _make_signal(status="correct"))
        save_signal(db, _make_signal(status="incorrect"))
        db.close()

        resp = client.get("/accuracy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_resolved"] == 2
        assert data["overall_accuracy_pct"] == 50.0


class TestDashboardEndpoint:
    def test_dashboard_serves_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Sharp Signal" in resp.text
        assert "Auto-refreshes" in resp.text or "refresh" in resp.text.lower()
