"""Allowlisted error categories for persisted and public temporal API details."""
from __future__ import annotations

from typing import Any


TEMPORAL_ERROR_CODES = frozenset({
    "ACTION_VERSION_CONFLICT",
    "CURRENT_STATE_CONFLICT",
    "DATA_CLASS_MISMATCH",
    "DISPATCH_ATTEMPTS_EXHAUSTED",
    "DISPATCH_FAILED",
    "EVENTS_INCOMPLETE",
    "EVENT_NOT_FOUND",
    "EVENT_NOT_TERMINAL_FAILED",
    "EVENT_PAYLOAD_CONFLICT",
    "EVENT_RUN_MISMATCH",
    "EVENT_WATERMARK_MISMATCH",
    "GRAPH_REBUILD_NOT_ALLOWED",
    "IDEMPOTENCY_KEY_REUSED",
    "INVALID_CONTROL",
    "INVALID_EVENT",
    "INVALID_EVENT_TIME",
    "INVALID_RETRY_REQUEST_KEY",
    "INVALID_SNAPSHOT_MANIFEST",
    "INVALID_SOURCE_ID",
    "INVALID_SOURCE_MAPPING",
    "INVALID_SOURCE_MODE",
    "INVALID_TIME_VALUE",
    "INVALID_SPEED",
    "INVALID_TIME_KIND",
    "LATE_EVENT_NOT_SUPPORTED",
    "MANUAL_REPLAY_REQUIRED",
    "MODEL_CREDENTIAL_UNAVAILABLE",
    "MODEL_NOT_CONFIGURED",
    "NO_EVENTS",
    "ONTOLOGY_NOT_FOUND",
    "OBSERVATION_OBJECT_TYPE_MISSING",
    "PROJECTION_ATTEMPTS_EXHAUSTED",
    "RECONCILIATION_SOURCE_UNSUPPORTED",
    "RETRY_REASON_REQUIRED",
    "RUN_ALREADY_ACTIVE",
    "RUN_NOT_COMPLETED",
    "RUN_NOT_FOUND",
    "RUN_NOT_TERMINAL",
    "RUN_PUBLISHED",
    "RUN_CANCELLED",
    "SCHEMA_REVISION_CONFLICT",
    "SCHEMA_REVISION_REQUIRED",
    "SNAPSHOT_STALE",
    "SOURCE_MODE_MISMATCH",
    "SOURCE_NOT_INSTALLED",
    "SOURCE_NOT_READY",
    "STORAGE_OBJECT_MISSING",
    "STREAM_VALIDATION_FAILED",
    "STREAM_INVALID",
    "STREAM_WORKER_FAILED",
    "TEMPORAL_EVENT_INVALID",
    "TEMPORAL_OPERATION_FAILED",
    "TEMPORAL_PROJECTION_FAILED",
    "TEMPORAL_PROFILE_FAILED",
    "TEMPORAL_REPLAY_CANCELLED",
    "TEMPORAL_REPLAY_FAILED",
    "TEMPORAL_REPLAY_LOCAL_FALLBACK",
    "TEMPORAL_REPLAY_NO_GRAPH_RELATIONS",
    "TEMPORAL_REPLAY_NO_VALID_ROWS",
    "TEMPORAL_REPLAY_WAITING_FOR_EVENT",
    "TEMPORAL_STREAM_FAILED",
    "TEMPORAL_SOURCE_INSTALL_FAILED",
    "TEMPORAL_SOURCE_PARSE_FAILED",
    "UNKNOWN_EPISODE",
    "UNKNOWN_SERIES",
    "UNSUPPORTED_LATE_EVENT_POLICY",
    "UNSUPPORTED_OPERATION",
    "WRONG_SOURCE",
    "dispatch_attempts_exhausted",
    "dispatch_failed",
    "projection_error",
    "projection_attempts_exhausted",
    "stream_worker_error",
    "worker_lease_expired",
})

_LEGACY_ERROR_ALIASES = {
    "用户取消时序模拟": "TEMPORAL_REPLAY_CANCELLED",
    "用户取消动态运行": "TEMPORAL_REPLAY_CANCELLED",
    "等待推送事件": "TEMPORAL_REPLAY_WAITING_FOR_EVENT",
    "Celery unavailable; background fallback": "TEMPORAL_REPLAY_LOCAL_FALLBACK",
    "数据版本没有可读取的存储对象": "SOURCE_NOT_READY",
    "MiniMax-M3 未配置": "MODEL_NOT_CONFIGURED",
    "MiniMax-M3 凭据无法解密": "MODEL_CREDENTIAL_UNAVAILABLE",
}

_INVALID_INPUT_CODES = frozenset({
    "ACTION_VERSION_CONFLICT",
    "DATA_CLASS_MISMATCH",
    "EVENT_PAYLOAD_CONFLICT",
    "EVENT_RUN_MISMATCH",
    "EVENT_WATERMARK_MISMATCH",
    "IDEMPOTENCY_KEY_REUSED",
    "INVALID_CONTROL",
    "INVALID_EVENT",
    "INVALID_EVENT_TIME",
    "INVALID_RETRY_REQUEST_KEY",
    "INVALID_SNAPSHOT_MANIFEST",
    "INVALID_SOURCE_ID",
    "INVALID_SOURCE_MAPPING",
    "INVALID_SOURCE_MODE",
    "INVALID_SPEED",
    "INVALID_TIME_KIND",
    "LATE_EVENT_NOT_SUPPORTED",
    "MANUAL_REPLAY_REQUIRED",
    "NO_EVENTS",
    "RECONCILIATION_SOURCE_UNSUPPORTED",
    "RETRY_REASON_REQUIRED",
    "RUN_ALREADY_ACTIVE",
    "RUN_NOT_COMPLETED",
    "RUN_NOT_FOUND",
    "RUN_NOT_TERMINAL",
    "RUN_PUBLISHED",
    "SCHEMA_REVISION_CONFLICT",
    "SCHEMA_REVISION_REQUIRED",
    "SOURCE_MODE_MISMATCH",
    "SOURCE_NOT_INSTALLED",
    "SOURCE_NOT_READY",
    "STORAGE_OBJECT_MISSING",
    "UNKNOWN_EPISODE",
    "UNKNOWN_SERIES",
    "INVALID_TIME_VALUE",
    "UNSUPPORTED_LATE_EVENT_POLICY",
    "UNSUPPORTED_OPERATION",
    "WRONG_SOURCE",
    "TEMPORAL_REPLAY_NO_GRAPH_RELATIONS",
    "TEMPORAL_REPLAY_NO_VALID_ROWS",
})


def temporal_error_details(error: Any, *, fallback_code: str = "TEMPORAL_OPERATION_FAILED") -> tuple[str | None, str | None]:
    """Normalize temporal errors to a known code and a non-sensitive message."""
    if error is None:
        return None, None

    code_attr = getattr(error, "code", None)
    value = str(code_attr if isinstance(code_attr, str) and code_attr else error).strip()
    if not value:
        return None, None

    code = _LEGACY_ERROR_ALIASES.get(value)
    if code is None and value.startswith("Celery unavailable; background fallback:"):
        code = "TEMPORAL_REPLAY_LOCAL_FALLBACK"
    if code is None and value.startswith("NO_VALID_TEMPORAL_ROWS:"):
        code = "TEMPORAL_REPLAY_NO_VALID_ROWS"
    if code is None and value.startswith("NO_GRAPH_RELATIONS:"):
        code = "TEMPORAL_REPLAY_NO_GRAPH_RELATIONS"
    if code is None:
        prefix = value.split(":", 1)[0].strip()
        if prefix in TEMPORAL_ERROR_CODES:
            code = prefix
        elif value in TEMPORAL_ERROR_CODES:
            code = value

    if code is None:
        code = fallback_code if fallback_code in TEMPORAL_ERROR_CODES else "TEMPORAL_OPERATION_FAILED"

    if code == "TEMPORAL_REPLAY_CANCELLED":
        message = "时序任务已取消。"
    elif code == "TEMPORAL_REPLAY_LOCAL_FALLBACK":
        message = "后台队列不可用，任务已切换为本地执行。"
    elif code == "TEMPORAL_REPLAY_WAITING_FOR_EVENT":
        message = "等待新的推送事件。"
    elif code == "worker_lease_expired":
        message = "工作租约已过期，任务进入恢复流程。"
    elif code in {"DISPATCH_FAILED", "DISPATCH_ATTEMPTS_EXHAUSTED", "dispatch_failed", "dispatch_attempts_exhausted"}:
        message = "时序任务派发暂不可用，请稍后查看运行状态。"
    elif code in {"PROJECTION_ATTEMPTS_EXHAUSTED", "projection_attempts_exhausted"}:
        message = "事件投影多次失败，请检查错误码和数据版本。"
    elif code == "STORAGE_OBJECT_MISSING":
        message = "时序来源文件不可用。"
    elif code == "SOURCE_NOT_READY":
        message = "时序来源尚未就绪，请稍后重试。"
    elif code == "GRAPH_REBUILD_NOT_ALLOWED":
        message = "当前运行状态不允许重建时序图投影。"
    elif code in {"CURRENT_STATE_CONFLICT", "SCHEMA_REVISION_CONFLICT", "SNAPSHOT_STALE", "ACTION_VERSION_CONFLICT"}:
        message = "时序来源或本体版本已变化，请刷新状态后重试。"
    elif code == "STREAM_VALIDATION_FAILED":
        message = "时序运行未通过发布校验，请检查校验状态。"
    elif code == "OBSERVATION_OBJECT_TYPE_MISSING":
        message = "当前本体缺少观测数据对应的对象类型。"
    elif code == "MODEL_NOT_CONFIGURED":
        message = "时序分析所需模型尚未配置。"
    elif code == "MODEL_CREDENTIAL_UNAVAILABLE":
        message = "时序分析模型凭据当前不可用。"
    elif code == "TEMPORAL_SOURCE_INSTALL_FAILED":
        message = "时序样例安装未完成，请检查服务状态后重试。"
    elif code == "TEMPORAL_SOURCE_PARSE_FAILED":
        message = "时序文件无法解析，请检查文件格式和列内容。"
    elif code == "TEMPORAL_PROFILE_FAILED":
        message = "时序数据画像生成失败，请检查错误码并重试。"
    elif code in {"EVENTS_INCOMPLETE", "RUN_NOT_COMPLETED", "RUN_NOT_TERMINAL", "EVENT_NOT_TERMINAL_FAILED"}:
        message = "时序任务尚未达到此操作所需的完成状态。"
    elif code in _INVALID_INPUT_CODES:
        message = "时序请求或事件数据未通过校验，请检查错误码和输入。"
    else:
        message = "时序任务未能完成，请检查错误码并重试。"
    return code, message


_SAFE_HTTP_INTEGER_FIELDS = {
    "EVENTS_INCOMPLETE": frozenset({"committed", "total"}),
    "INVALID_EVENT": frozenset({"row_index"}),
    "INVALID_TIME_VALUE": frozenset({"row_index"}),
}


def temporal_error_payload(
    error: Any,
    *,
    fallback_code: str = "TEMPORAL_OPERATION_FAILED",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a safe HTTP error detail without forwarding provider text or arbitrary extras."""
    code, message = temporal_error_details(error, fallback_code=fallback_code)
    if code is None or message is None:
        code, message = temporal_error_details(fallback_code)
    payload: dict[str, Any] = {"code": code, "message": message}
    source_extra = extra if extra is not None else getattr(error, "extra", None)
    allowed_fields = _SAFE_HTTP_INTEGER_FIELDS.get(code or "", frozenset())
    if isinstance(source_extra, dict):
        for key in allowed_fields:
            value = source_extra.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000:
                payload[key] = value
    return payload

