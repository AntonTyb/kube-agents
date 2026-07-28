"""Centralized thread-safe redaction engine and security policy enforcement for audit logs."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any, Dict, List, Optional, Tuple


class SecurityAuditViolationError(Exception):
    """Raised when an audit plugin detects a high-risk security policy violation."""

    pass


class AuditRedactor:
    """Thread-safe regex and dictionary redactor for secrets, PII, and security violations."""

    PRIVATE_KEY_PATTERN = re.compile(
        r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA\s+)?PRIVATE\s+KEY-----",
        re.IGNORECASE,
    )
    GCP_API_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z-_]{35}")
    BEARER_TOKEN_PATTERN = re.compile(r"(?i)bearer\s+[a-zA-Z0-9_\-\.=]+")
    GITHUB_TOKEN_PATTERN = re.compile(r"ghp_[a-zA-Z0-9]{36}")
    OPENAI_TOKEN_PATTERN = re.compile(r"sk-[a-zA-Z0-9]{48}")
    SECRET_KV_PATTERN = re.compile(
        r"(?i)\b(password|secret|token|api_key|apikey|access_token|client_secret)\b([\"']?\s*[:=]\s*)([\"']?)([^\"'\s,}{\]]+)\3"
    )
    EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

    HIGH_RISK_PATTERNS = (
        re.compile(r"(?i)ignore\s+(?:all\s+)?previous\s+instructions"),
        re.compile(r"(?i)system\s+override"),
        re.compile(r"(?i)cat\s+/etc/(?:shadow|passwd)"),
        re.compile(r"(?i)rm\s+-rf\s+/"),
        re.compile(r"(?i)drop\s+table"),
    )

    SENSITIVE_KEYS = {
        "password",
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

    @classmethod
    def redact_text(cls, text: str) -> str:
        if not text:
            return text
        text = cls.PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", text)
        text = cls.GCP_API_KEY_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED_SECRET]", text)
        text = cls.GITHUB_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.OPENAI_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.SECRET_KV_PATTERN.sub(r"\1\2\3[REDACTED_SECRET]\3", text)
        text = cls.EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
        return text

    @classmethod
    def redact(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls.redact_text(value)
        elif isinstance(value, dict):
            redacted_dict: Dict[Any, Any] = {}
            for k, v in value.items():
                k_str = str(k).lower()
                if any(s in k_str for s in cls.SENSITIVE_KEYS):
                    redacted_dict[k] = "[REDACTED_SECRET]" if isinstance(v, (str, bytes)) else cls.redact(v)
                elif "email" in k_str or "mail" in k_str:
                    redacted_dict[k] = "[REDACTED_EMAIL]" if isinstance(v, (str, bytes)) else cls.redact(v)
                else:
                    redacted_dict[k] = cls.redact(v)
            return redacted_dict
        elif isinstance(value, list):
            return [cls.redact(item) for item in value]
        elif isinstance(value, tuple):
            return tuple(cls.redact(item) for item in value)
        return value

    @classmethod
    def redact_in_place(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return data
        for k, v in list(data.items()):
            k_str = str(k).lower()
            if any(s in k_str for s in cls.SENSITIVE_KEYS):
                data[k] = "[REDACTED_SECRET]" if isinstance(v, (str, bytes)) else cls.redact(v)
            elif "email" in k_str or "mail" in k_str:
                data[k] = "[REDACTED_EMAIL]" if isinstance(v, (str, bytes)) else cls.redact(v)
            else:
                data[k] = cls.redact(v)
        return data

    @classmethod
    def check_security_violations(cls, value: Any) -> None:
        if isinstance(value, str):
            for pattern in cls.HIGH_RISK_PATTERNS:
                if pattern.search(value):
                    raise SecurityAuditViolationError(
                        f"Security audit policy violation: high-risk pattern detected ({pattern.pattern})"
                    )
        elif isinstance(value, dict):
            for k, v in value.items():
                cls.check_security_violations(str(k))
                cls.check_security_violations(v)
        elif isinstance(value, (list, tuple)):
            for item in value:
                cls.check_security_violations(item)

    @staticmethod
    def hmac_hash(value: str, salt: Optional[bytes] = None) -> str:
        if not value:
            return ""
        if salt is None:
            salt_str = (
                os.getenv("SESSION_KV_SALT")
                or os.getenv("API_SERVER_KEY")
                or "default-session-kv-salt"
            )
            salt = salt_str.encode("utf-8")
        return hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()
