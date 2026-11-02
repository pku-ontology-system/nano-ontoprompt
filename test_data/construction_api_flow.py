"""Reusable API flow for private, structured Dataset Construction demos."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests


STRUCTURED_EXTENSIONS = {".csv", ".tsv", ".xlsx"}


def _login(api: str, username: str | None, password: str | None, *, label: str) -> requests.Session:
    if not username or not password:
        raise RuntimeError(f"Set explicit {label} username and password environment variables before running this flow")
    session = requests.Session()
    response = session.post(
        f"{api}/api/v1/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    body = _json_body(response, f"{label} login")
    data = body.get("data") if isinstance(body, dict) else None
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError(f"{label} login response did not contain an access token")
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def login_session(api: str) -> requests.Session:
    """Log in as the candidate author using explicit demo credentials."""
    return _login(
        api,
        os.environ.get("PROJECT4_E2E_USERNAME"),
        os.environ.get("PROJECT4_E2E_PASSWORD"),
        label="author",
    )


def login_reviewer_session(api: str) -> requests.Session:
    """Log in as a distinct reviewer; private Dataset flows require an admin reviewer."""
    author = (os.environ.get("PROJECT4_E2E_USERNAME") or "").strip().casefold()
    reviewer = (os.environ.get("PROJECT4_E2E_REVIEWER_USERNAME") or "").strip().casefold()
    if not author or not reviewer or author == reviewer:
        raise RuntimeError(
            "Set PROJECT4_E2E_REVIEWER_USERNAME/PASSWORD to a different account; "
            "private Dataset review requires an admin or an explicit dataset grant"
        )
    return _login(
        api,
        os.environ.get("PROJECT4_E2E_REVIEWER_USERNAME"),
        os.environ.get("PROJECT4_E2E_REVIEWER_PASSWORD"),
        label="independent reviewer",
    )


def first_structured_file(folder: Path) -> Path:
    """Pick a stable CSV/TSV/XLSX representative from a domain fixture folder."""
    candidates = sorted(
        path for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in STRUCTURED_EXTENSIONS
    )
    if not candidates:
        raise FileNotFoundError(f"No CSV/TSV/XLSX fixture found in {folder}")
    return candidates[0]


def build_private_structured_dataset(
    session: requests.Session,
    api: str,
    file_path: Path,
    *,
    reviewer_session: requests.Session,
    domain: str,
    ontology_name: str,
    row_limit: int = 500,
) -> dict[str, Any]:
    """Upload a private Dataset, freeze a candidate, and require independent review."""
    author = (os.environ.get("PROJECT4_E2E_USERNAME") or "").strip().casefold()
    reviewer = (os.environ.get("PROJECT4_E2E_REVIEWER_USERNAME") or "").strip().casefold()
    if not author or not reviewer or author == reviewer:
        raise RuntimeError("Author and independent reviewer credentials must be present and different before upload")
    with file_path.open("rb") as source:
        upload_response = session.post(
            f"{api}/api/v2/datasets/upload",
            params={"privacy_level": "private"},
            files={"file": (file_path.name, source, "application/octet-stream")},
            timeout=120,
        )
    upload_body = _json_body(upload_response, "Dataset upload")
    dataset = _data_object(upload_body)
    dataset_id = _required_string(dataset.get("id"), "Dataset ID")

    draft_response = session.post(
        f"{api}/api/v2/construction/drafts",
        json={
            "dataset_id": dataset_id,
            "data_class": "regular",
            "target_mode": "create",
            "new_ontology_name": ontology_name,
            "new_ontology_domain": domain,
            "privacy_level": "private",
            "selection": {"row_limit": row_limit},
        },
        timeout=30,
    )
    draft = _data_object(_json_body(draft_response, "ConstructionDraft creation"))
    draft_id = _required_string(draft.get("id"), "ConstructionDraft ID")

    preflight_response = session.post(
        f"{api}/api/v2/construction/drafts/{draft_id}/m3-preflight", timeout=30,
    )
    preflight = _json_body(preflight_response, "Construction preflight")
    if _data_object(preflight).get("preflight", {}).get("allowed") is not True:
        raise RuntimeError("Private Dataset preflight did not allow construction")

    mapping_response = session.post(
        f"{api}/api/v2/construction/drafts/{draft_id}/generate-mapping", timeout=60,
    )
    mapping_result = _data_object(_json_body(mapping_response, "MappingTask creation"))
    mapping_task_id = _required_string(mapping_result.get("mapping_task_id"), "MappingTask ID")
    mapping_task = _poll_task(
        session,
        f"{api}/api/v2/mapping-tasks/{mapping_task_id}",
        label="MappingTask",
        timeout_seconds=600,
    )

    draft_response = session.get(
        f"{api}/api/v2/construction/drafts/{draft_id}", timeout=30,
    )
    draft = _data_object(_json_body(draft_response, "ConstructionDraft read"))
    mapping = draft.get("mapping") if isinstance(draft.get("mapping"), dict) else {}
    suggestions = mapping.get("suggestions") if isinstance(mapping.get("suggestions"), list) else []
    if not suggestions:
        raise RuntimeError("ConstructionDraft returned no mapping candidates")

    patch_response = session.patch(
        f"{api}/api/v2/construction/drafts/{draft_id}",
        json={"mapping": {"suggestions": suggestions, "confirmed": suggestions}},
        timeout=30,
    )
    _json_body(patch_response, "Mapping candidate confirmation")

    run_response = session.post(
        f"{api}/api/v2/construction/drafts/{draft_id}/build",
        json={"mapping_confirmed": True, "mode": "create"},
        timeout=30,
    )
    submission = _data_object(_json_body(run_response, "Construction review submission"))
    review = submission.get("review") if isinstance(submission.get("review"), dict) else {}
    review_id = _required_string(review.get("id"), "ConstructionReview ID")
    if review.get("status") != "pending_review":
        raise RuntimeError("Construction candidate was not frozen for independent review")

    decision_response = reviewer_session.post(
        f"{api}/api/v2/construction/reviews/{review_id}/decision",
        json={"decision": "approved", "note": "本地演示：核对已固定来源版本、映射摘要和影响预览后批准"},
        timeout=30,
    )
    decision = _data_object(_json_body(decision_response, "Independent construction approval"))
    run = decision.get("run") if isinstance(decision.get("run"), dict) else {}
    run_id = _required_string(run.get("id"), "ConstructionRun ID")
    completed = _poll_task(
        reviewer_session,
        f"{api}/api/v2/construction-runs/{run_id}",
        label="ConstructionRun",
        timeout_seconds=900,
    )
    metrics = completed.get("metrics") if isinstance(completed.get("metrics"), dict) else {}
    return {
        "domain": domain,
        "file": file_path.name,
        "dataset_id": dataset_id,
        "draft_id": draft_id,
        "mapping_task_id": mapping_task_id,
        "review_id": review_id,
        "reviewer_id": decision.get("review", {}).get("reviewed_by") if isinstance(decision.get("review"), dict) else None,
        "run_id": run_id,
        "ontology_id": _required_string(completed.get("ontology_id"), "Ontology ID"),
        "status": str(completed.get("status") or ""),
        "mapping_status": str(mapping_task.get("status") or ""),
        "entities": int(metrics.get("entity_type_count") or 0),
        "instances": int(metrics.get("instance_count") or 0),
        "edges": int(metrics.get("edges_written") or 0),
        "logic": int(metrics.get("logic_rule_count") or 0),
        "actions": 0,
        "construction_method": "private_dataset_deterministic_mapping",
    }


def _poll_task(
    session: requests.Session,
    url: str,
    *,
    label: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = session.get(url, timeout=30)
        body = _data_object(_json_body(response, label))
        status = str(body.get("status") or "")
        if status == "completed":
            return body
        if status in {"failed", "cancelled", "waiting_for_model"}:
            raise RuntimeError(f"{label} ended with status={status}")
        time.sleep(2)
    raise TimeoutError(f"{label} timed out after {timeout_seconds} seconds")


def _json_body(response: requests.Response, label: str) -> Any:
    if not response.ok:
        raise RuntimeError(f"{label} failed with HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} returned a non-JSON response") from exc


def _data_object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else body


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} missing from API response")
    return value
