"""Model config selection helpers for LLM/VLM call sites."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Iterable


VLM_TAGS = {"VLM提取", "vlm", "vision", "视觉", "多模态", "multimodal"}
VLM_TOKENS = ("omni", "vlm", "vision", "multimodal")


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def usage_tags(model_config) -> list[str]:
    options = getattr(model_config, "options", None) or {}
    return [str(tag) for tag in _as_list(options.get("usage_tags"))]


def is_vlm_config(model_config) -> bool:
    options = getattr(model_config, "options", None) or {}
    tags = set(usage_tags(model_config))
    if tags & VLM_TAGS:
        return True
    modalities = " ".join(str(x).lower() for x in _as_list(options.get("modalities")))
    model_names = " ".join(str(x).lower() for x in _as_list(getattr(model_config, "models", None)))
    provider = str(getattr(model_config, "provider", "") or "").lower()
    return (
        "vision" in modalities
        or "image" in modalities
        or any(token in model_names for token in VLM_TOKENS)
        or any(token in provider for token in ("omni", "vlm"))
    )


def select_llm_model_config(
    db=None,
    model_id: str | None = None,
    purpose_tags: Iterable[str] = (),
    allow_vlm: bool = False,
):
    """Select a configured LLM.

    Text LLM callers should keep allow_vlm=False so a VLM such as mimo-omni
    does not override a text model such as DeepSeek just because it was updated
    later. VLM callers pass allow_vlm=True and purpose_tags=("VLM提取",).
    """
    from app.database import SessionLocal
    from app.models.model_config import ModelConfig

    owns_db = db is None
    db = db or SessionLocal()
    try:
        query = db.query(ModelConfig).filter(ModelConfig.config_type == "llm")
        if model_id:
            selected = query.filter(ModelConfig.id == model_id).first()
            if selected:
                return selected

        configs = query.order_by(ModelConfig.updated_at.desc()).all()
        if not configs:
            return None

        requested_tags = [str(tag) for tag in purpose_tags if tag]
        for tag in requested_tags:
            for item in configs:
                if tag in usage_tags(item):
                    return item

        if allow_vlm:
            vlm_candidates = [item for item in configs if is_vlm_config(item)]
            candidates = vlm_candidates or configs
        else:
            candidates = [item for item in configs if not is_vlm_config(item)]
        return candidates[0] if candidates else configs[0]
    finally:
        if owns_db:
            db.close()


def model_config_provenance_snapshot(model_config) -> dict | None:
    """Return a non-secret fingerprint suitable for an immutable run manifest."""
    if model_config is None:
        return None

    def digest(value) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    encrypted_credential = getattr(model_config, "api_key_encrypted", None) or ""
    gateway_base = os.getenv("LITELLM_API_BASE", "").strip()
    gateway_key = os.getenv("LITELLM_API_KEY", "").strip()
    snapshot = {
        "config_id": str(getattr(model_config, "id", "")),
        "provider": getattr(model_config, "provider", None),
        "models": [str(item) for item in _as_list(getattr(model_config, "models", None))],
        "config_type": getattr(model_config, "config_type", None),
        "updated_at": getattr(model_config, "updated_at", None).isoformat()
        if getattr(model_config, "updated_at", None) else None,
        "api_base_digest": digest(getattr(model_config, "api_base", None)) if getattr(model_config, "api_base", None) else None,
        "options_digest": digest(getattr(model_config, "options", None) or {}),
        # Store only one-way fingerprints; neither credentials nor endpoints are included.
        "credential_fingerprint": hashlib.sha256(encrypted_credential.encode("utf-8")).hexdigest() if encrypted_credential else None,
        "gateway_config_digest": digest({"api_base": gateway_base, "api_key": gateway_key})
        if gateway_base or gateway_key else None,
        "provider_model_artifact_reproducibility": "unverified",
    }
    snapshot["config_digest"] = digest(snapshot)
    return snapshot


def resolve_pinned_llm_model_config(db, pin: dict):
    """Resolve an exact Pipeline ModelConfig pin and reject configuration drift."""
    from app.models.model_config import ModelConfig

    config_id = pin.get("model_config_id") if isinstance(pin, dict) else None
    expected_digest = pin.get("model_config_digest") if isinstance(pin, dict) else None
    if not config_id or not expected_digest:
        return None, "model_unavailable"
    config = db.query(ModelConfig).filter(
        ModelConfig.id == config_id,
        ModelConfig.config_type == "llm",
    ).first()
    if config is None:
        return None, "model_config_missing"
    snapshot = model_config_provenance_snapshot(config)
    if not snapshot or snapshot.get("config_digest") != expected_digest:
        return None, "model_config_changed"
    return config, None


def llm_call_kwargs(model_config) -> dict | None:
    if not model_config:
        return None
    from app.services import encryption_service

    models = _as_list(getattr(model_config, "models", None))
    model_name = str(models[0]) if models else ""
    if not model_name:
        return None
    api_key = ""
    encrypted = getattr(model_config, "api_key_encrypted", None)
    if encrypted:
        api_key = encryption_service.decrypt(encrypted)
    # Ollama's OpenAI-compatible endpoint ignores the bearer value, but the
    # OpenAI SDK still requires a non-empty key at client construction time.
    if not api_key and str(getattr(model_config, "provider", "")).lower() in {"ollama", "local"}:
        api_key = "ollama"
    # In the workbench runtime every model request goes through the local
    # LiteLLM gateway. A direct provider URL remains available for lightweight
    # unit tests and legacy deployments that do not configure the gateway.
    import os
    gateway_base = os.getenv("LITELLM_API_BASE", "").strip()
    if gateway_base:
        gateway_key = os.getenv("LITELLM_API_KEY", "").strip() or api_key or "local-gateway"
        return {
            "provider": "openai",
            "api_key": gateway_key,
            "api_base": gateway_base.rstrip("/"),
            "model": model_name,
        }
    return {
        "provider": getattr(model_config, "provider", None),
        "api_key": api_key,
        "api_base": getattr(model_config, "api_base", None),
        "model": model_name,
    }
