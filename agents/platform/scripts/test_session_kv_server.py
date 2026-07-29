#!/usr/bin/env python3
"""Unit tests for session_kv_server authentication, endpoints, and utilities."""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Create a temporary SQLite database for testing and set it in the environment
# BEFORE importing session_kv_server to prevent it from creating the default production DB path.
db_fd, temp_db_path = tempfile.mkstemp()
os.close(db_fd)
os.environ["SESSION_KV_DB_PATH"] = temp_db_path

# Add directory containing session_kv_server.py to sys.path
sys.path.insert(0, str(Path(__file__).parent.absolute()))


class StubHTTPException(Exception):
    def __init__(self, status_code: int, detail: str = ""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _load_session_kv_server():
    """Load session_kv_server with minimal stubs if needed."""
    try:
        return importlib.import_module("session_kv_server")
    except Exception:
        fastapi = types.ModuleType("fastapi")
        fastapi.HTTPException = StubHTTPException
        fastapi.Header = lambda default=None, alias=None: default
        fastapi.Depends = lambda fn: fn
        fastapi.BackgroundTasks = object
        class _DummyApp:
            def get(self, *a, **k):
                return lambda f: f
            def post(self, *a, **k):
                return lambda f: f
        fastapi.FastAPI = _DummyApp

        mcp = types.ModuleType("mcp"); mcp.__path__ = []
        mcp_server = types.ModuleType("mcp.server"); mcp_server.__path__ = []
        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = lambda *a, **k: types.SimpleNamespace(
            tool=lambda *a, **k: (lambda f: f), run=lambda: None)
        pydantic = types.ModuleType("pydantic")
        pydantic.Field = lambda *a, **k: None
        session_manager = types.ModuleType("session_manager")
        session_manager.SessionManager = object

        sys.modules.update({
            "fastapi": fastapi,
            "mcp": mcp,
            "mcp.server": mcp_server,
            "mcp.server.fastmcp": fastmcp,
            "pydantic": pydantic,
            "session_manager": session_manager,
        })
        return importlib.import_module("session_kv_server")


session_kv_server = _load_session_kv_server()
from session_kv_server import (
    clean_event_message,
    clean_reason_label,
    clean_workload_name,
    get_severity_details,
)


class TestSessionKvServerUtils(unittest.TestCase):
    def test_clean_workload_name_pod_replicas(self):
        self.assertEqual(clean_workload_name("pod", "billing-processor-6cfdb6b98b-zwv24"), "billing-processor")
        self.assertEqual(clean_workload_name("pod", "redis-master-0"), "redis-master-0")
        self.assertEqual(clean_workload_name("pod", "billing-pod-zwv24"), "billing-pod")
        self.assertEqual(clean_workload_name("service", "billing-processor-service"), "billing-processor-service")

    def test_clean_reason_label_camel_case(self):
        self.assertEqual(clean_reason_label("FailedToDrainNode"), "Failed to drain node")
        self.assertEqual(clean_reason_label("PodEviction"), "Pod eviction")
        self.assertEqual(clean_reason_label("FailedMount"), "Failed mount")
        self.assertEqual(clean_reason_label("Unhealthy"), "Unhealthy")

    def test_clean_event_message_pdb(self):
        msg = "cannot be evicted: would violate PDB default/billing-processor-pdb"
        self.assertEqual(clean_event_message(msg), "Eviction would violate PDB billing-processor-pdb")
        msg_general = "MountVolume.SetUp failed for volume \"config\""
        self.assertEqual(clean_event_message(msg_general), msg_general)

    def test_get_severity_details(self):
        self.assertEqual(get_severity_details("Warning", "FailedMount"), ("🔴", "Critical"))
        self.assertEqual(get_severity_details("Warning", "FailedScheduling"), ("🔴", "Critical"))
        self.assertEqual(get_severity_details("Warning", "FailedToDrainNode"), ("🔴", "Critical"))
        self.assertEqual(get_severity_details("Warning", "Unhealthy"), ("🟡", "Warning"))
        self.assertEqual(get_severity_details("Normal", "Scheduled"), ("🔵", "Info"))


class TestSessionKvServerApi(unittest.TestCase):
    def setUp(self):
        os.environ["API_SERVER_KEY"] = "test-secret-key"

    def tearDown(self):
        pass

    def test_create_session(self):
        data = session_kv_server.create_session()
        self.assertIn("sessionID", data)
        self.assertTrue(data["sessionID"].startswith("k8s-evt-"))

    def test_get_session_metadata_not_found(self):
        with self.assertRaises(Exception) as ctx:
            session_kv_server.get_metadata("non-existent-session")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_create_and_get_session_metadata(self):
        create_resp = session_kv_server.create_session()
        session_id = create_resp["sessionID"]
        data = session_kv_server.get_metadata(session_id)
        self.assertEqual(data.get("platform"), "k8s-watcher")
        self.assertIn("created_at", data)

    def test_store_and_get_incident(self):
        incident_data = {
            "chat_id": "test-chat",
            "thread_id": "test-thread",
            "report": "This is a test report with Option A and Option B"
        }
        resp = session_kv_server.store_incident(incident_data)
        self.assertEqual(resp, {"status": "stored"})

        data = session_kv_server.get_incident("test-chat", "test-thread")
        self.assertEqual(data["chat_id"], "test-chat")
        self.assertEqual(data["thread_id"], "test-thread")
        self.assertEqual(data["report"], "This is a test report with Option A and Option B")

    def test_get_incident_not_found(self):
        with self.assertRaises(Exception) as ctx:
            session_kv_server.get_incident("missing", "missing")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_database_cleanup_ttl(self):
        from datetime import datetime, timedelta
        old_time = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(temp_db_path) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO session_metadata (session_id, metadata, updated_at) VALUES (?, ?, ?)",
                    ("old-session", "{\"platform\": \"k8s-watcher\"}", old_time)
                )
                conn.execute(
                    "INSERT OR IGNORE INTO incidents (chat_id, thread_id, report, created_at) VALUES (?, ?, ?, ?)",
                    ("old-chat", "old-thread", "old-report", old_time)
                )
                conn.execute(
                    "INSERT OR IGNORE INTO incidents (chat_id, thread_id, report) VALUES (?, ?, ?)",
                    ("fresh-chat", "fresh-thread", "fresh-report")
                )

        resp = session_kv_server.create_session()
        self.assertIn("sessionID", resp)

        with sqlite3.connect(temp_db_path) as conn:
            res = conn.execute("SELECT session_id FROM session_metadata WHERE session_id = ?", ("old-session",)).fetchone()
            self.assertIsNone(res)
            res = conn.execute("SELECT report FROM incidents WHERE chat_id = ? AND thread_id = ?", ("old-chat", "old-thread")).fetchone()
            self.assertIsNone(res)
            res = conn.execute("SELECT report FROM incidents WHERE chat_id = ? AND thread_id = ?", ("fresh-chat", "fresh-thread")).fetchone()
            self.assertIsNotNone(res)
            self.assertEqual(res[0], "fresh-report")


class TestSessionKvServerQueryBuilding(unittest.TestCase):
    @patch.dict(os.environ, {"GCP_PROJECT_ID": "test-project-id"})
    def test_build_agent_query_with_project_id(self):
        payload = {
            "reason": "FailedMount",
            "namespace": "test-ns",
            "kind_of_object": "Pod",
            "name": "test-pod",
            "message": "some message"
        }
        query = session_kv_server._build_agent_query("test-session", payload)
        self.assertIn("project=test-project-id", query)
        self.assertNotIn("jayantid-gkedemos", query)

    @patch.dict(os.environ, {"GCP_PROJECT": "test-project-legacy"})
    def test_build_agent_query_with_legacy_project(self):
        payload = {
            "reason": "FailedMount",
            "namespace": "test-ns",
            "kind_of_object": "Pod",
            "name": "test-pod",
            "message": "some message"
        }
        with patch.dict(os.environ, {"GCP_PROJECT_ID": ""}):
            query = session_kv_server._build_agent_query("test-session", payload)
            self.assertIn("project=test-project-legacy", query)

    def test_build_agent_query_no_project(self):
        payload = {
            "reason": "FailedMount",
            "namespace": "test-ns",
            "kind_of_object": "Pod",
            "name": "test-pod",
            "message": "some message"
        }
        with patch.dict(os.environ, {"GCP_PROJECT_ID": "", "GCP_PROJECT": ""}):
            query = session_kv_server._build_agent_query("test-session", payload)
            self.assertNotIn("project=", query)


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
        session_kv_server.SESSION_KV_DB_PATH = os.environ.get("SESSION_KV_DB_PATH", temp_db_path)
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
    try:
        unittest.main()
    finally:
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
