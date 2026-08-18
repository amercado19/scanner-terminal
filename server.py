#!/usr/bin/env python3
"""
Read-only JSON endpoint for the SPY 0DTE terminal.

The browser NEVER talks to MongoDB directly and NEVER sees DB credentials.
This tiny service reads the single payload document server-side and returns it
as JSON. Deploy as a Render "Web Service" (free tier is fine for read-only).

Endpoints:
  GET /            -> health text
  GET /healthz     -> {"ok": true}
  GET /api/spy     -> the spy_live_data document (JSON)

CORS is locked to your GitHub Pages origin by default. Override with
ALLOWED_ORIGINS (comma-separated) if you serve the page elsewhere.
"""
import os
import datetime as dt
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo import MongoClient

MONGODB_URI      = os.environ.get("MONGODB_URI", "")
MONGODB_USER     = os.environ.get("MONGODB_USER", "andresmercado1919_db_user")
MONGODB_PASSWORD = os.environ.get("MONGODB_PASSWORD", "")
MONGODB_CLUSTER_HOST = os.environ.get("MONGODB_CLUSTER_HOST", "cluster0.rku8nto.mongodb.net")
DB_NAME         = os.environ.get("DB_NAME", "spy_terminal_db")
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "spy_payloads")
DOC_ID          = os.environ.get("DOC_ID", "spy_live_data")
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://amercado19.github.io",
).split(",")

app = FastAPI(title="SPY 0DTE Terminal API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_methods=["GET"],
    allow_headers=["*"],
)

def _build_uri():
    if MONGODB_PASSWORD:
        from urllib.parse import quote_plus
        return (f"mongodb+srv://{quote_plus(MONGODB_USER)}:{quote_plus(MONGODB_PASSWORD)}"
                f"@{MONGODB_CLUSTER_HOST}/?retryWrites=true&w=majority&appName=Cluster0")
    return MONGODB_URI

_client = None
def _coll():
    global _client
    if _client is None:
        _client = MongoClient(_build_uri(), serverSelectionTimeoutMS=8000)
    return _client[DB_NAME][COLLECTION_NAME]


@app.get("/")
def root():
    return {"service": "spy-0dte-terminal", "see": "/api/spy"}


@app.get("/healthz")
def healthz():
    try:
        _coll().database.client.admin.command("ping")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.get("/api/spy")
def get_spy():
    try:
        doc = _coll().find_one({"_id": DOC_ID})
        if not doc:
            return JSONResponse(
                {"error": "no data yet", "hint": "worker has not written a payload"},
                status_code=404,
            )
        doc["_id"] = str(doc["_id"])
        # small server-side freshness helper for the UI
        epoch = doc.get("updated_epoch")
        if epoch:
            doc["age_seconds"] = int(dt.datetime.utcnow().timestamp()) - int(epoch)
        return JSONResponse(doc, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
