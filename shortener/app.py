"""HTTP API and transactional storage for short links."""

import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, HttpUrl


class LinkRequest(BaseModel):
    url: HttpUrl
    expires_in_seconds: Annotated[int, Field(strict=True, gt=0, le=31_536_000)] | None = None


class Link(BaseModel):
    code: str
    url: HttpUrl
    expires_at: float | None


class Analytics(Link):
    clicks: int


def connect(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def initialize(path):
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            CREATE TABLE IF NOT EXISTS links (
                code TEXT PRIMARY KEY, url TEXT NOT NULL,
                clicks INTEGER NOT NULL DEFAULT 0, expires_at REAL
            )
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(links)")}
        if "expires_at" not in columns:
            db.execute("ALTER TABLE links ADD COLUMN expires_at REAL")


def create_app(path: str | Path, clock=time.time):
    initialize(path)
    app = FastAPI(title="URL Shortener")

    @app.post("/urls", status_code=201)
    def create(request: LinkRequest) -> Link:
        expiry = None if request.expires_in_seconds is None else clock() + request.expires_in_seconds
        with closing(connect(path)) as db, db:
            for _ in range(5):
                code = secrets.token_urlsafe(6)
                created = db.execute(
                    "INSERT INTO links(code,url,expires_at) VALUES (?,?,?) ON CONFLICT(code) DO NOTHING",
                    (code, str(request.url), expiry),
                )
                if created.rowcount:
                    return Link(code=code, url=request.url, expires_at=expiry)
        raise HTTPException(503, "Unable to allocate a short code")

    @app.get("/urls/{code}/analytics")
    def analytics(code: str) -> Analytics:
        with closing(connect(path)) as db:
            row = db.execute("SELECT * FROM links WHERE code=?", (code,)).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown short code")
        return Analytics(**dict(row))

    @app.get("/{code}", response_class=RedirectResponse)
    def redirect(code: str):
        with closing(connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT url,expires_at FROM links WHERE code=?", (code,)).fetchone()
            if row is None:
                raise HTTPException(404, "Unknown short code")
            if row["expires_at"] is not None and row["expires_at"] <= clock():
                raise HTTPException(410, "Short link expired")
            db.execute("UPDATE links SET clicks=clicks+1 WHERE code=?", (code,))
        return RedirectResponse(row["url"], status_code=302, headers={"Cache-Control": "no-store"})

    return app


def serve():
    return create_app(os.environ.get("SHORTENER_DB", "links.sqlite"))
