"""From a rule alert to a reviewed change: the alert's suggested Action becomes a submitted Decision Case.

Rules never apply an Action.  This turns the Action an alert suggests into a case that carries the alert's evidence in its
reason and context, previews it and submits it for review.  Review, execution and the audit trail are the Decision Case's own.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.v2.decision_case import DecisionCase
from app.models.v2.rule_runtime import RuleAlert, RuleResource
from app.services.v2.decision_cases import CaseError, DecisionCaseService
from app.services.v2.object_query.metadata import load_sql_metadata
from app.services.v2.rule_runtime import RuleService


class AlertActionError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def case_key(alert_id: str) -> str:
    return f"alert:{alert_id}"


def propose_alert_action(db: Session, ontology_id: str, user: User, alert_id: str, access) -> tuple[DecisionCase, bool]:
    """Returns (case, created).  Asking again for the same alert returns the case it already opened."""
    alert = db.get(RuleAlert, alert_id)
    if alert is None or alert.ontology_id != ontology_id:
        raise AlertActionError("alert_not_found", "告警不存在", 404)
    existing = db.query(DecisionCase).filter_by(ontology_id=ontology_id, client_request_id=case_key(alert.id)).first()
    if existing is not None:
        return existing, False
    if alert.status == "resolved":
        raise AlertActionError("alert_resolved", "告警已经解决", 409)
    suggested = alert.suggested_action or {}
    request = suggested.get("request") or {}
    if not suggested.get("action_id") or not request.get("target_object_id"):
        raise AlertActionError("no_suggested_action", "这条告警没有建议的处置动作")
    rule = db.get(RuleResource, alert.rule_id)
    evidence = alert.evidence or {}
    cases = DecisionCaseService(db, ontology_id, user)
    try:
        state = cases.target_state(request["target_object_id"])
        label = state["properties"].get("name") or alert.subject_id
        reason = (f"规则“{rule.display_name if rule else alert.rule_id}”告警：{label} 的窗口均值在 {alert.window_start} 至 {alert.window_end} 持续越线"
                  f"（峰值 {evidence.get('peak')}，连续 {evidence.get('breaching_windows')} 个窗口）。建议执行规则绑定的处置动作，由审核人确认后才会改动正式数据。")
        case = cases.create(kind="maintenance_review", title=f"处置告警：{label}", action_type_id=suggested["action_id"], target_object_id=request["target_object_id"],
                            target_object_type=state["object_type"], parameters=request.get("parameters") or {}, evidence_refs=[], reason=reason,
                            expected_etag=state["etag"], client_request_id=case_key(alert.id),
                            context={"source": "rule_alert", "alert_id": alert.id, "rule_id": alert.rule_id, "rule_version_id": alert.rule_version_id,
                                     "window_start": alert.window_start, "window_end": alert.window_end, "peak": evidence.get("peak"),
                                     "breaching_windows": evidence.get("breaching_windows"), "series_id": evidence.get("series_id")})
        preview = cases.preview(case.id)
        case = cases.submit(case.id, preview["preview_digest"])
    except CaseError as exc:
        db.rollback()
        raise AlertActionError(exc.code, exc.message, exc.status) from exc
    if alert.status == "open":
        service = RuleService(db, ontology_id, user, load_sql_metadata(db, ontology_id))
        service.access = access
        service.transition(alert.id, "acknowledged", f"已提交处置审核：{case.id}")
    return case, True
