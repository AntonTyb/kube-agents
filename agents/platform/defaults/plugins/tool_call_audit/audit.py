import json
import logging
from typing import Any, Dict, Optional

try:
    from ..common.redactor import AuditRedactor, SecurityAuditViolationError
except (ImportError, ValueError):
    from agents.platform.defaults.plugins.common.redactor import (
        AuditRedactor,
        SecurityAuditViolationError,
    )

logger = logging.getLogger("hermes.plugin.tool_call_audit")

_PAYLOAD_LOG_LIMIT = 2000


def _serialize(value: Any) -> str:
    value = AuditRedactor.redact(value)
    if isinstance(value, str):
        if len(value) > _PAYLOAD_LOG_LIMIT:
            return value[:_PAYLOAD_LOG_LIMIT] + "...(truncated)"
        return value
    try:
        serialized = json.dumps(value, default=str, sort_keys=True)
    except Exception:
        serialized = str(value)
    if len(serialized) > _PAYLOAD_LOG_LIMIT:
        return serialized[:_PAYLOAD_LOG_LIMIT] + "...(truncated)"
    return serialized


def _emit(event: str, fields: Dict[str, Any]) -> None:
    record = {"audit_event": event, **fields}
    logger.info(json.dumps(record, default=str, sort_keys=True))


def log_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    **kwargs: Any,
) -> None:
    AuditRedactor.check_security_violations(args)
    if isinstance(args, dict):
        AuditRedactor.redact_in_place(args)
    try:
        _emit(
            "tool_call_start",
            {"tool_name": tool_name, "task_id": task_id, "args": _serialize(args or {})},
        )
    except SecurityAuditViolationError:
        raise
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_tool_call hook: %s", exc, exc_info=True)
        raise


def log_post_tool_call(
    tool_name: str = "",
    result: Any = None,
    duration_ms: Optional[float] = None,
    task_id: str = "",
    **kwargs: Any,
) -> None:
    AuditRedactor.check_security_violations(result)
    if isinstance(result, dict):
        AuditRedactor.redact_in_place(result)
    try:
        _emit(
            "tool_call_end",
            {
                "tool_name": tool_name,
                "task_id": task_id,
                "duration_ms": duration_ms,
                "result": _serialize(result),
            },
        )
    except SecurityAuditViolationError:
        raise
    except Exception as exc:
        logger.error("Error in tool_call_audit post_tool_call hook: %s", exc, exc_info=True)
        raise


def log_pre_approval_request(
    command: str = "",
    description: str = "",
    pattern_key: str = "",
    surface: str = "",
    **kwargs: Any,
) -> None:
    AuditRedactor.check_security_violations(command)
    AuditRedactor.check_security_violations(description)
    try:
        _emit(
            "approval_request",
            {
                "surface": surface,
                "pattern_key": pattern_key,
                "description": _serialize(description),
                "command": _serialize(command),
            },
        )
    except SecurityAuditViolationError:
        raise
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_approval_request hook: %s", exc, exc_info=True)
        raise


def log_post_approval_response(
    command: str = "",
    description: str = "",
    pattern_key: str = "",
    surface: str = "",
    choice: str = "",
    **kwargs: Any,
) -> None:
    AuditRedactor.check_security_violations(command)
    AuditRedactor.check_security_violations(description)
    try:
        _emit(
            "approval_response",
            {
                "surface": surface,
                "pattern_key": pattern_key,
                "choice": choice,
                "description": _serialize(description),
                "command": _serialize(command),
            },
        )
    except SecurityAuditViolationError:
        raise
    except Exception as exc:
        logger.error("Error in tool_call_audit post_approval_response hook: %s", exc, exc_info=True)
        raise


def log_pre_gateway_dispatch(
    event: Any,
    gateway: Any = None,
    session_store: Any = None,
    **kwargs: Any,
) -> None:
    text = getattr(event, "text", "") or ""
    AuditRedactor.check_security_violations(text)
    try:
        source = getattr(event, "source", None)
        session_id = ""
        if source is not None and session_store is not None:
            try:
                session_entry = session_store.get_or_create_session(source)
                session_id = getattr(session_entry, "session_id", "") or ""
            except Exception:
                pass

        platform = ""
        user_id = ""
        if source is not None:
            platform_obj = getattr(source, "platform", "") or ""
            platform = getattr(platform_obj, "value", None) or str(platform_obj)
            user_id = getattr(source, "user_id", "") or ""

        _emit(
            "gateway_dispatch",
            {
                "session_id": session_id,
                "platform": platform,
                "user_id": user_id,
                "text": _serialize(text),
            },
        )
    except SecurityAuditViolationError:
        raise
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_gateway_dispatch hook: %s", exc, exc_info=True)
        raise
