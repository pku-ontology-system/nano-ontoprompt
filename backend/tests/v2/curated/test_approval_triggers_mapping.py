from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.routers.v2.curated import approve_review


def test_persistent_review_approval_triggers_incremental_mapping():
    db = MagicMock()
    review = SimpleNamespace(
        id="review-1", status="approved", dataset_version_id="version-12",
    )
    mapping_result = {
        "triggered_mappings": [{
            "mapping_id": "mapping-1", "task_id": "celery-1",
            "dataset_version_id": "version-12",
        }],
    }

    with patch("app.services.v2.curated.review_service.ReviewService") as review_service, \
         patch("app.services.v2.incremental.orchestrator.IncrementalOrchestrator") as orchestrator:
        review_service.return_value.approve.return_value = review
        orchestrator.return_value.on_review_approved.return_value = mapping_result

        response = approve_review("review-1", notes="checked", db=db, _admin=None)

    assert response["dataset_version_id"] == "version-12"
    assert response["mapping_apply"] == mapping_result
    orchestrator.return_value.on_review_approved.assert_called_once_with("review-1")
