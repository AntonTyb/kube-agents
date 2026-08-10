"""Centralised redaction and pseudonymisation for audit logs and session metadata.

Two independent jobs live here because both are needed by the same four call
sites (the two audit hooks, the session store, and the OTel bridge):

* :meth:`AuditRedactor.redact` / :meth:`AuditRedactor.redact_text` strip
  credentials and e-mail addresses out of anything on its way to stdout.
* :meth:`AuditRedactor.hmac_hash` turns a user identity into a stable
  pseudonym, so session rows and span attributes carry a hash rather than the
  address itself.

Deliberately *not* here: raising on a match. These helpers are called from
`pre_gateway_dispatch` and from `start_span`, so an exception — including one
from a regex false positive — would land in the message-dispatch path or in
every span the agent opens. Redaction fails open by design; the enforcement
boundary is Kubernetes RBAC and the credential proxy, not a logging hook.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("hermes.plugin.common.redactor")

SALT_ENV_VAR = "SESSION_KV_SALT"

_fallback_salt: Optional[bytes] = None
_fallback_salt_lock = threading.Lock()


def _resolve_salt() -> bytes:
    """Return the HMAC salt, generating a per-process one if none is configured.

    Failing closed here was tried and is wrong: ``hmac_hash`` is called
    unconditionally for any Google Chat user id, from ``SessionMetadata``'s
    constructor, and the caller swallows the exception — so a missing salt took
    out session metadata entirely (no session_id, chat_id or thread_id row ever
    written) and with it thread resolution, incident lookup and span identity.

    The salt is optional in every install path, so "absent" is the common case
    on upgrade rather than a misconfiguration. Degrade loudly instead: hashes
    stay correct and unlinkable, they simply stop being comparable across a pod
    restart.
    """
    configured = (os.getenv(SALT_ENV_VAR) or "").strip()
    if configured:
        return configured.encode("utf-8")

    global _fallback_salt
    with _fallback_salt_lock:
        if _fallback_salt is None:
            _fallback_salt = secrets.token_bytes(32)
            logger.warning(
                "%s is not configured; falling back to a per-process random salt. "
                "Identity pseudonyms remain safe but will not be stable across pod "
                "restarts. Set %s in the agent Secret to make them stable.",
                SALT_ENV_VAR,
                SALT_ENV_VAR,
            )
        return _fallback_salt


class AuditRedactor:
    """Stateless regex and dictionary redactor for secrets and PII."""

    PRIVATE_KEY_PATTERN = re.compile(
        r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY(?:\s+BLOCK)?-----"
        r"[\s\S]*?"
        r"-----END\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY(?:\s+BLOCK)?-----",
        re.IGNORECASE,
    )
    GCP_API_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z\-_]{35}")
    GCP_OAUTH_TOKEN_PATTERN = re.compile(r"ya29\.[0-9A-Za-z\-_.]{20,}")
    BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bbearer\s+([a-zA-Z0-9_\-.=]{15,})")
    GITHUB_TOKEN_PATTERN = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")
    OPENAI_TOKEN_PATTERN = re.compile(r"sk-[A-Za-z0-9]{20,}")
    SECRET_KV_PATTERN = re.compile(
        r"(?i)\b(password|passwd|secret|token|api_key|apikey|access_token|client_secret)\b"
        r"([\"']?\s*[:=]\s*)([\"']?)([^\"'\s,}{\]]+)\3"
    )
    EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

    SENSITIVE_KEYS = {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "client_secret",
        "authorization",
        "auth",
        "private_key",
        "credential",
        "credentials",
    }

    @staticmethod
    def _get_key_words(key: Any) -> Set[str]:
        """Split a mapping key into lowercase words, camelCase included.

        ``clientSecret`` and ``client_secret`` must both match, while
        ``tokenizer`` and ``author`` must not — hence whole-word matching
        against :attr:`SENSITIVE_KEYS` rather than a substring test.
        """
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()
        words = set(re.split(r"[^a-z0-9]+", text))
        words.add(text)
        return {word for word in words if word}

    @classmethod
    def redact_text(cls, text: str) -> str:
        if not text:
            return text
        text = cls.PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", text)
        text = cls.GCP_API_KEY_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.GCP_OAUTH_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED_SECRET]", text)
        text = cls.GITHUB_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.OPENAI_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.SECRET_KV_PATTERN.sub(r"\1\2\3[REDACTED_SECRET]\3", text)
        text = cls.EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
        return text

    @classmethod
    def redact(cls, value: Any) -> Any:
        """Recursively redact a value, keying off mapping keys where present."""
        if isinstance(value, bytes):
            return cls.redact_text(value.decode("utf-8", errors="replace")).encode("utf-8")
        if isinstance(value, str):
            return cls.redact_text(value)
        if isinstance(value, dict):
            redacted: Dict[Any, Any] = {}
            for key, item in value.items():
                words = cls._get_key_words(key)
                if words & cls.SENSITIVE_KEYS:
                    redacted[key] = (
                        "[REDACTED_SECRET]" if isinstance(item, (str, bytes)) else cls.redact(item)
                    )
                elif "email" in words or "mail" in words:
                    redacted[key] = (
                        "[REDACTED_EMAIL]" if isinstance(item, (str, bytes)) else cls.redact(item)
                    )
                else:
                    redacted[key] = cls.redact(item)
            return redacted
        if isinstance(value, list):
            return [cls.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls.redact(item) for item in value)
        return value

    @staticmethod
    def hmac_hash(value: str, salt: Optional[bytes] = None) -> str:
        """Pseudonymise ``value`` as a hex HMAC-SHA256 digest.

        Never raises: an unconfigured salt yields a per-process one (see
        :func:`_resolve_salt`) rather than taking the caller down.
        """
        if not value:
            return ""
        return hmac.new(
            salt if salt is not None else _resolve_salt(),
            str(value).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def pseudonymise_identity(cls, value: Any) -> str:
        """Hash ``value`` when it looks like an e-mail address, else pass it through.

        Google Chat reports the user's address as the user id; Slack reports an
        opaque member id, which is already a pseudonym and stays readable.
        """
        text = str(value or "")
        if "@" not in text:
            return text
        return cls.hmac_hash(text)
