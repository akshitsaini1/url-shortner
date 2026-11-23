import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from shortener.app import LinkRequest, create_app


@pytest.mark.parametrize("value", ["file:///etc/passwd", "javascript:alert(1)", "missing-scheme"])
def test_invalid_url(value):
    with pytest.raises(ValidationError):
        LinkRequest(url=value)


def test_redirect_and_expiry(tmp_path):
    now = [1000]
    client = TestClient(create_app(tmp_path / "links.sqlite", lambda: now[0]))
    assert client.get("/missing").status_code == 404
    assert client.get("/urls/missing/analytics").status_code == 404
    assert client.post("/urls", json={"url": "https://example.com", "expires_in_seconds": 0}).status_code == 422
    response = client.post("/urls", json={"url": "https://example.com/path", "expires_in_seconds": 5})
    assert response.status_code == 201
    code = response.json()["code"]
    redirect = client.get(f"/{code}", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "https://example.com/path"
    now[0] += 5
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410
    assert client.get(f"/urls/{code}/analytics").json()["clicks"] == 1


def test_migrates_existing_links(tmp_path):
    path = tmp_path / "links.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE links (code TEXT PRIMARY KEY,url TEXT NOT NULL,clicks INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO links VALUES ('legacy', 'https://example.com/', 3)")
    client = TestClient(create_app(path))
    assert client.get("/legacy", follow_redirects=False).status_code == 302
    assert client.get("/urls/legacy/analytics").json() == {
        "code": "legacy", "url": "https://example.com/", "clicks": 4, "expires_at": None
    }
