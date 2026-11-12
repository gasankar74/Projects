import os

from cryptography.fernet import Fernet

os.environ["MERIDIAN_DATABASE_URL"] = os.environ.get("MERIDIAN_TEST_DATABASE_URL", "postgresql+psycopg://meridian:meridian@localhost:5432/meridian_test")
os.environ["MERIDIAN_TOKEN_ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
os.environ["MERIDIAN_DEV_LOGIN_ENABLED"] = "true"
os.environ["MERIDIAN_GOOGLE_CLIENT_ID"] = "test-client-id"
os.environ["MERIDIAN_GOOGLE_CLIENT_SECRET"] = "test-secret"
# Tests talk to the in-memory fake through the real Google hostnames, whatever a local .env says.
os.environ["MERIDIAN_GOOGLE_AUTH_URL"] = "https://accounts.google.com/o/oauth2/v2/auth"
os.environ["MERIDIAN_GOOGLE_TOKEN_URL"] = "https://oauth2.googleapis.com/token"
os.environ["MERIDIAN_GOOGLE_USERINFO_URL"] = "https://openidconnect.googleapis.com/v1/userinfo"
os.environ["MERIDIAN_GOOGLE_CALENDAR_API"] = "https://www.googleapis.com/calendar/v3"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import models  # noqa: E402,F401
from app.db import Base, get_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.auth import google_client  # noqa: E402
from app.services.google import GoogleClient  # noqa: E402

from .fake_google import FakeGoogle  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def schema():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def clean_db():
    yield
    engine = get_engine()
    with engine.begin() as conn:
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {names} CASCADE"))


@pytest.fixture
def google():
    fake = FakeGoogle()
    app.dependency_overrides[google_client] = lambda: GoogleClient(transport=fake.transport())
    yield fake
    app.dependency_overrides.pop(google_client, None)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def login(c: TestClient, email="greg@example.com", tz="America/New_York"):
    r = c.post("/auth/dev-login", json={"email": email, "name": "Greg", "timezone": tz})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def authed(client):
    login(client)
    return client
