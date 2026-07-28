#!/usr/bin/env python3
"""Unit tests for session_kv_server authentication and endpoints."""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Add directory containing session_kv_server.py to sys.path
sys.path.insert(0, str(Path(__file__).parent.absolute()))


class StubHTTPException(Exception):
    def __init__(self, status_code: int, detail: str = ""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _load_session_kv_server():
    """Load session_kv_server with minimal FastAPI stubs if needed."""
    try:
        return importlib.import_module("session_kv_server")
    except Exception:
        fastapi = types.ModuleType("fastapi")
        fastapi.HTTPException = StubHTTPException
        fastapi.Header = lambda default=None, alias=None: default
        fastapi.Depends = lambda fn: fn
        class _DummyApp:
            def get(self, *a, **k):
                return lambda f: f
        fastapi.FastAPI = _DummyApp
        sys.modules["fastapi"] = fastapi
        return importlib.import_module("session_kv_server")


session_kv_server = _load_session_kv_server()


class TestSessionKVServer(unittest.TestCase):
    def setUp(self):
        self._saved_key = os.environ.get("API_SERVER_KEY")
        self._saved_db = os.environ.get("SESSION_KV_DB_PATH")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_session_kv.db")
        os.environ["SESSION_KV_DB_PATH"] = self.db_path
        session_kv_server.SESSION_KV_DB_PATH = self.db_path
        session_kv_server.init_db()

    def tearDown(self):
        if self._saved_key is None:
            os.environ.pop("API_SERVER_KEY", None)
        else:
            os.environ["API_SERVER_KEY"] = self._saved_key
        if self._saved_db is None:
            os.environ.pop("SESSION_KV_DB_PATH", None)
        else:
            os.environ["SESSION_KV_DB_PATH"] = self._saved_db
        self.temp_dir.cleanup()

    def test_healthz_unauthenticated(self):
        res = session_kv_server.healthz()
        self.assertEqual(res, {"status": "ok"})

    def test_verify_api_key_raises_when_unset(self):
        os.environ.pop("API_SERVER_KEY", None)
        with self.assertRaises(Exception) as ctx:
            session_kv_server.verify_api_key(x_api_key="secret")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("not configured", ctx.exception.detail)

    def test_verify_api_key_raises_when_invalid(self):
        os.environ["API_SERVER_KEY"] = "valid-secret"
        with self.assertRaises(Exception) as ctx:
            session_kv_server.verify_api_key(x_api_key="wrong-secret")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("invalid API key", ctx.exception.detail)

    def test_verify_api_key_succeeds_with_x_api_key(self):
        os.environ["API_SERVER_KEY"] = "valid-secret"
        try:
            session_kv_server.verify_api_key(x_api_key="valid-secret")
        except Exception:
            self.fail("verify_api_key raised unexpectedly with valid X-API-Key")

    def test_verify_api_key_succeeds_with_bearer_token(self):
        os.environ["API_SERVER_KEY"] = "valid-secret"
        try:
            session_kv_server.verify_api_key(authorization="Bearer valid-secret")
        except Exception:
            self.fail("verify_api_key raised unexpectedly with valid Bearer token")

    def test_get_metadata_and_list_sessions(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                ("sess-123", json.dumps({"user_email_hash": "abc"})),
            )
        res = session_kv_server.get_metadata("sess-123")
        self.assertEqual(res, {"user_email_hash": "abc"})

        listed = session_kv_server.list_sessions(limit=10)
        self.assertEqual(len(listed["sessions"]), 1)
        self.assertEqual(listed["sessions"][0]["session_id"], "sess-123")


if __name__ == "__main__":
    unittest.main()
