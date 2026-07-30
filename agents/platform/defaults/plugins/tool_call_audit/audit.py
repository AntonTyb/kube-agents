import fnmatch
import json
import logging
import os
import pathlib
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:
    yaml = None

logger = logging.getLogger("hermes.plugin.tool_call_audit")

_PAYLOAD_LOG_LIMIT = 2000

DEFAULT_EXECUTION_BOUNDS = {
    "sandbox_mode": "enforced",
    "command_timeout_seconds": 60,
    "allowed_binary_prefixes": [
        "git status",
        "git diff",
        "git log",
        "git checkout",
        "git add",
        "git commit",
        "kubectl get",
        "kubectl describe",
        "kubectl logs",
        "gcloud logging read",
        "gcloud container clusters describe",
        "pytest",
        "python3 -m unittest",
        "python3 ./skills/",
        "python3 ./scripts/",
    ],
    "blocked_command_patterns": [
        "rm -rf /",
        "sudo ",
        "chmod ",
        "chown ",
        "nohup ",
        "curl * | bash",
        "wget * | bash",
        "pip install *",
    ],
    "writable_paths": [
        "/tmp",
        "/opt/data/scratch",
    ],
    "read_only_paths": [
        "/opt/hermes/skills",
        "/opt/defaults",
        "/etc",
    ],
}


def _load_execution_bounds(config_path: Optional[str] = None) -> Dict[str, Any]:
    paths_to_check = []
    if config_path:
        paths_to_check.append(pathlib.Path(config_path))
    paths_to_check.append(pathlib.Path("/opt/defaults/config.yaml"))

    try:
        curr = pathlib.Path(__file__).resolve()
        for p in curr.parents:
            candidate = p / "agents" / "platform" / "config.yaml"
            if candidate.exists():
                paths_to_check.append(candidate)
                break
    except Exception:
        pass

    for path in paths_to_check:
        if path.exists() and yaml is not None:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    bounds = data.get("execution_bounds", {}).get("hermes_cli", {})
                    if bounds:
                        return bounds
            except Exception as exc:
                logger.warning("Failed to load execution bounds from %s: %s", path, exc)
    return DEFAULT_EXECUTION_BOUNDS


def verify_execution_bounds(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    config_path: Optional[str] = None,
) -> None:
    shell_tools = {"hermes-cli", "hermes_cli", "shell", "bash", "run_command", "cli"}
    if not tool_name or tool_name.lower() not in shell_tools:
        return
    if not args or not isinstance(args, dict):
        return

    cmd = args.get("command") or args.get("cmd") or args.get("command_line") or args.get("CommandLine") or ""
    if isinstance(args.get("args"), str) and not cmd:
        cmd = args.get("args")
    elif isinstance(args.get("args"), (list, tuple)) and not cmd:
        cmd = " ".join(str(x) for x in args.get("args"))
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(x) for x in cmd)

    if not cmd or not isinstance(cmd, str):
        return

    cmd_stripped = cmd.strip()
    bounds = _load_execution_bounds(config_path)
    if not bounds:
        bounds = DEFAULT_EXECUTION_BOUNDS

    # 1. Check blocked command patterns
    blocked_patterns = bounds.get("blocked_command_patterns", [])
    for pattern in blocked_patterns:
        if not pattern:
            continue
        if "*" in pattern:
            glob_pat = pattern if pattern.startswith("*") else f"*{pattern}"
            glob_pat = glob_pat if glob_pat.endswith("*") else f"{glob_pat}*"
            if fnmatch.fnmatch(cmd_stripped, glob_pat):
                raise PermissionError(
                    f"Command '{cmd_stripped}' is blocked by execution bounds: matches blocked pattern '{pattern}'."
                )
        else:
            if pattern in cmd_stripped:
                raise PermissionError(
                    f"Command '{cmd_stripped}' is blocked by execution bounds: matches blocked pattern '{pattern}'."
                )

    # 2. Check filesystem write confinement and read-only paths
    mutating_tokens = {"rm", "mv", "cp", "touch", "chmod", "chown", "mkdir", "rmdir", "sed", "tee", "vi", "nano", ">", ">>"}
    tokens = cmd_stripped.split()
    is_mutating = any(t in mutating_tokens for t in tokens)

    read_only_paths = bounds.get("read_only_paths", [])
    writable_paths = bounds.get("writable_paths", [])

    for token in tokens:
        if token.startswith("/"):
            for ro_path in read_only_paths:
                if token == ro_path or token.startswith(ro_path.rstrip("/") + "/"):
                    if is_mutating:
                        raise PermissionError(
                            f"Command '{cmd_stripped}' is blocked by execution bounds: write access to read-only path '{ro_path}' is forbidden."
                        )
            if is_mutating:
                is_writable = any(
                    token == w_path or token.startswith(w_path.rstrip("/") + "/")
                    for w_path in writable_paths
                )
                if not is_writable:
                    raise PermissionError(
                        f"Command '{cmd_stripped}' is blocked by execution bounds: write access to path '{token}' outside writable paths is restricted."
                    )

    # 3. Check allowlist prefixes if sandbox mode is enforced
    if bounds.get("sandbox_mode", "").lower() == "enforced":
        allowed_prefixes = bounds.get("allowed_binary_prefixes", [])
        if allowed_prefixes:
            matched = False
            for pref in allowed_prefixes:
                if cmd_stripped == pref:
                    matched = True
                    break
                if pref.endswith("/") and cmd_stripped.startswith(pref):
                    matched = True
                    break
                if cmd_stripped.startswith(pref + " "):
                    matched = True
                    break
            if not matched:
                raise PermissionError(
                    f"Command '{cmd_stripped}' is blocked by execution bounds: command does not match any allowed binary prefix."
                )


def _serialize(value: Any) -> str:
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
    try:
        verify_execution_bounds(tool_name, args)
    except PermissionError as exc:
        _emit(
            "tool_call_denied",
            {
                "tool_name": tool_name,
                "task_id": task_id,
                "args": _serialize(args or {}),
                "reason": str(exc),
            },
        )
        raise
    try:
        _emit(
            "tool_call_start",
            {"tool_name": tool_name, "task_id": task_id, "args": _serialize(args or {})},
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_tool_call hook: %s", exc, exc_info=True)



def log_post_tool_call(
    tool_name: str = "",
    result: Any = None,
    duration_ms: Optional[float] = None,
    task_id: str = "",
    **kwargs: Any,
) -> None:
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
    except Exception as exc:
        logger.error("Error in tool_call_audit post_tool_call hook: %s", exc, exc_info=True)


def log_pre_approval_request(
    command: str = "",
    description: str = "",
    pattern_key: str = "",
    surface: str = "",
    **kwargs: Any,
) -> None:
    try:
        _emit(
            "approval_request",
            {
                "surface": surface,
                "pattern_key": pattern_key,
                "description": description,
                "command": _serialize(command),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_approval_request hook: %s", exc, exc_info=True)


def log_post_approval_response(
    command: str = "",
    description: str = "",
    pattern_key: str = "",
    surface: str = "",
    choice: str = "",
    **kwargs: Any,
) -> None:
    try:
        _emit(
            "approval_response",
            {
                "surface": surface,
                "pattern_key": pattern_key,
                "choice": choice,
                "description": description,
                "command": _serialize(command),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit post_approval_response hook: %s", exc, exc_info=True)


def log_pre_gateway_dispatch(
    event: Any,
    gateway: Any = None,
    session_store: Any = None,
    **kwargs: Any,
) -> None:
    try:
        source = getattr(event, "source", None)
        session_id = ""
        if source is not None and session_store is not None:
            try:
                session_entry = session_store.get_or_create_session(source)
                session_id = getattr(session_entry, "session_id", "") or ""
            except Exception:
                pass

        text = getattr(event, "text", "") or ""
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
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_gateway_dispatch hook: %s", exc, exc_info=True)

