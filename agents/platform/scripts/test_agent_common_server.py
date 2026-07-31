import importlib
import os
import sys
import time
import types
import unittest
from pathlib import Path

# Add the directory containing agent_common_server.py to sys.path so it can be imported.
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from session_manager import SessionManager


def _load_agent_common_server():
    """Import the module under test.

    These tests are not wired into CI, and the credential logic under test
    (resolve_agent_credentials) depends only on the stdlib. When the hermes
    runtime deps (FastMCP / pydantic / session_manager) aren't importable, fall
    back to minimal stubs so the module still imports in a bare checkout. Each
    stub package sets __path__ so it is treated as a real package.
    """
    try:
        return importlib.import_module("agent_common_server")
    except Exception:
        import session_manager as real_session_manager

        mcp = types.ModuleType("mcp"); mcp.__path__ = []
        mcp_server = types.ModuleType("mcp.server"); mcp_server.__path__ = []
        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = lambda *a, **k: types.SimpleNamespace(
            tool=lambda *a, **k: (lambda f: f), run=lambda: None)
        pydantic = types.ModuleType("pydantic")
        pydantic.Field = lambda *a, **k: None
        sys.modules.update({
            "mcp": mcp, "mcp.server": mcp_server, "mcp.server.fastmcp": fastmcp,
            "pydantic": pydantic, "session_manager": real_session_manager,
        })
        return importlib.import_module("agent_common_server")


resolve_agent_credentials = _load_agent_common_server().resolve_agent_credentials


class TestResolveAgentCredentials(unittest.TestCase):
    """API_SERVER_KEY must fail closed — never silently authenticate as a
    guessable literal when the shared secret is unconfigured (MCP-001)."""

    def setUp(self):
        self._saved = os.environ.get("API_SERVER_KEY")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("API_SERVER_KEY", None)
        else:
            os.environ["API_SERVER_KEY"] = self._saved

    def test_raises_when_key_unset(self):
        os.environ.pop("API_SERVER_KEY", None)
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_raises_when_key_empty(self):
        os.environ["API_SERVER_KEY"] = ""
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_raises_when_key_whitespace(self):
        os.environ["API_SERVER_KEY"] = "   "
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_raises_when_key_unset_or_empty(self):
        os.environ.pop("API_SERVER_KEY", None)
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")
        os.environ["API_SERVER_KEY"] = ""
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_never_falls_back_to_none_literal(self):
        """Regression pin: an unconfigured key must fail closed — raise, never
        yield the guessable literal 'none'."""
        os.environ.pop("API_SERVER_KEY", None)
        with self.assertRaises(ValueError) as ctx:
            resolve_agent_credentials("platform")
        self.assertIn("API_SERVER_KEY is not configured", str(ctx.exception))

    def test_returns_endpoint_and_key_when_set(self):
        os.environ["API_SERVER_KEY"] = "s3cret"
        endpoint, api_key = resolve_agent_credentials("platform")
        self.assertEqual(api_key, "s3cret")
        self.assertIn("8642", endpoint)


class TestDelegationHeaderSecurity(unittest.TestCase):
    """Verify cryptographic signing and verification of X-Hermes-* headers
    for inter-agent authentication and replay/tamper defense."""

    def setUp(self):
        self.sm = SessionManager()
        self.context = {
            "session_id": "sess-abc-123",
            "user_id": "platform:user-456",
            "sender_id": "user-456",
            "metadata": {"user_email": "user@example.com"},
        }
        self.api_key = "primary-secret-key"
        self.secondary_key = "secondary-secret-key"

    def test_signed_delegation_headers(self):
        headers = self.sm.signed_delegation_headers(self.context, self.api_key)
        self.assertEqual(headers["X-Hermes-Session-Id"], "sess-abc-123")
        self.assertEqual(headers["X-Hermes-User-Id"], "platform:user-456")
        self.assertIn("X-Hermes-Signature", headers)
        self.assertIn("X-Hermes-Timestamp", headers)
        self.assertTrue(headers["X-Hermes-Signature"].startswith("sha256="))

    def test_verify_delegation_headers_success(self):
        headers = self.sm.signed_delegation_headers(self.context, self.api_key)
        valid = self.sm.verify_delegation_headers(headers, [self.api_key])
        self.assertTrue(valid)

    def test_verify_delegation_headers_multiple_keys(self):
        headers = self.sm.signed_delegation_headers(self.context, self.secondary_key)
        valid = self.sm.verify_delegation_headers(
            headers, [self.api_key, self.secondary_key]
        )
        self.assertTrue(valid)

    def test_verify_delegation_headers_tampering(self):
        headers = self.sm.signed_delegation_headers(self.context, self.api_key)
        headers["X-Hermes-User-Id"] = "platform:admin-attacker"
        valid = self.sm.verify_delegation_headers(headers, [self.api_key])
        self.assertFalse(valid)

    def test_verify_delegation_headers_replay_expired(self):
        headers = self.sm.signed_delegation_headers(self.context, self.api_key)
        valid = self.sm.verify_delegation_headers(
            headers, [self.api_key], max_skew_seconds=-1
        )
        self.assertFalse(valid)
        headers["X-Hermes-Timestamp"] = str(int(time.time()) - 400)
        self.assertFalse(
            self.sm.verify_delegation_headers(headers, [self.api_key])
        )


if __name__ == "__main__":
    unittest.main()
