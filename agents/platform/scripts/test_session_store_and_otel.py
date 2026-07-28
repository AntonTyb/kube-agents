#!/usr/bin/env python3
"""Unit tests for PII protection in session_store and session_otel_bridge."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Add repo root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agents.platform.defaults.plugins.common.redactor import AuditRedactor
from agents.platform.defaults.plugins.session_otel_bridge.bridge import (
    OtelSessionBridge,
)
from agents.platform.defaults.plugins.session_store.store import (
    SessionMetadata,
    SessionMetadataStore,
)


class TestSessionStorePII(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "session_kv.db")
        self._saved_db = os.environ.get("SESSION_KV_DB_PATH")
        os.environ["SESSION_KV_DB_PATH"] = self.db_path
        # Reset SessionMetadataStore connection
        SessionMetadataStore._close_unlocked()

    def tearDown(self):
        SessionMetadataStore._close_unlocked()
        if self._saved_db is None:
            os.environ.pop("SESSION_KV_DB_PATH", None)
        else:
            os.environ["SESSION_KV_DB_PATH"] = self._saved_db
        self.temp_dir.cleanup()

    def test_session_metadata_hashes_email(self):
        email = "user@example.com"
        meta = SessionMetadata(
            session_id="s-1",
            platform="google_chat",
            user_id=email,
            user_email=email,
        )
        data = meta.to_dict()
        self.assertNotIn("user_email", data)
        self.assertIn("user_email_hash", data)
        expected_hash = AuditRedactor.hmac_hash(email)
        self.assertEqual(data["user_email_hash"], expected_hash)
        self.assertEqual(data["user_id"], expected_hash)
        self.assertNotIn(email, str(data))

    def test_session_metadata_store_persists_hash_not_email(self):
        email = "secret@example.com"
        meta = SessionMetadata(
            session_id="s-2",
            platform="google_chat",
            user_id=email,
            user_email=email,
        )
        SessionMetadataStore.write("s-2", meta.to_dict())

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT metadata FROM session_metadata WHERE session_id = 's-2'"
            ).fetchone()

        self.assertIsNotNone(row)
        stored_dict = json.loads(row[0])
        self.assertNotIn(email, row[0])
        self.assertIn("user_email_hash", stored_dict)

    def test_otel_session_bridge_anonymizes_identity(self):
        email = "otel-user@example.com"
        meta = SessionMetadata(
            session_id="s-otel",
            platform="google_chat",
            user_id=email,
            user_email=email,
        )
        SessionMetadataStore.write("s-otel", meta.to_dict())

        bridge = OtelSessionBridge(db_path=Path(self.db_path))
        attrs = bridge._span_attributes_for_session("s-otel")

        self.assertIn("user.id", attrs)
        self.assertIn("hermes.sender.id", attrs)
        self.assertNotIn(email, attrs.values())
        self.assertIn(AuditRedactor.hmac_hash(email), attrs["hermes.sender.id"])


if __name__ == "__main__":
    unittest.main()
