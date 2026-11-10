"""The lineage of an ontology, written as OpenLineage events from what the platform already records about it.

source table --[Auto Ontology run]--> object type --[rule]--> alerts --[decision]--> decision cases
                                          \\--[health check]--> report      \\--[entity resolution]--> entities

Nothing here is guessed: a type is linked to a table only where an Auto Ontology candidate was published for it, a property to a column only
where the candidate recorded the source field, a rule to the properties its logic names, a decision case to the alert it was raised from.
Types that were made by hand have no upstream, and the graph shows that. The events replace the earlier ones of the same ontology, so
regenerating after a change gives the current picture; the graph itself is derived from the events on read, like every other lineage graph.
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.ontology import OntologyProject
from app.models.v2.action import OntologyActionType
from app.models.v2.analysis import AnalysisRun
from app.models.v2.decision_case import DecisionCase
from app.models.v2.rule_runtime import RuleAlert, RuleResource, RuleVersion
from app.services.v2.analysis import lineage, ontology_metadata, ontology_source, openlineage as ol
from app.services.v2.analysis.ontology_suite import identity_property

PRODUCER = "urn:workbench:ontology-lineage"
LAYERS = {"source": "数据源表", "build": "Auto Ontology 生成", "object": "对象类型", "rule": "规则", "alert": "告警", "decision": "处置", "analysis": "数据分析"}
FILTER_BLOCKS = {"filter"}


SOURCE_PREFIX = "ontology:"


def source_tag(ontology_id: str) -> str:
    return f"{SOURCE_PREFIX}{ontology_id}"


def namespace_of(project: OntologyProject) -> str:
    return f"ontology:{project.name}"[:200]


def rule_properties(logic: dict) -> dict[str, list[str]]:
    """The properties a rule's logic reads: the ones it filters on, the series it watches, and the ones it shows as evidence."""
    filters: list[str] = []
    series: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            prop = node.get("property")
            if isinstance(prop, dict) and isinstance(prop.get("api_name"), str):
                filters.append(prop["api_name"])
            elif isinstance(prop, str):
                filters.append(prop)
            for key, value in node.items():
                if key == "series_property" and isinstance(value, str):
                    series.append(value)
                elif key != "property":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for block in logic.get("blocks") or []:
        walk(block)
    evidence = [p for p in (logic.get("output") or {}).get("evidence_properties") or [] if isinstance(p, str)]
    unique = lambda items: list(dict.fromkeys(items))  # noqa: E731
    return {"filters": unique(filters), "series": unique(series), "evidence": unique(evidence)}


class Builder:
    def __init__(self, db: Session, user: Any, ontology_id: str, now: datetime) -> None:
        self.db, self.user, self.ontology_id, self.now = db, user, ontology_id, now
        self.project = db.get(OntologyProject, ontology_id)
        self.ns = namespace_of(self.project)
        self.types = {t["api_name"]: t for t in ontology_source.object_types(db, user, ontology_id)}
        self.evidence = ontology_metadata.candidate_evidence(db, ontology_id)
        self.events: list[dict] = []
        self.counts: Counter = Counter()

    # ---- datasets
    def type_dataset(self, name: str, *, extra: dict | None = None, input_facets: dict | None = None, column_lineage: dict | None = None) -> dict:
        description = self.types[name]
        facets = {"schema": ol.facet("SchemaDatasetFacet", {"fields": [{"name": p["api_name"], "type": p["data_type"]} for p in description["properties"]]}, producer=PRODUCER),
                  "documentation": ol.facet("DocumentationDatasetFacet", {"description": f"对象类型 {name}，{len(description['properties'])} 个属性"}, producer=PRODUCER),
                  **ol.layer("object", PRODUCER), **ol.ref("object_type", ontology_id=self.ontology_id, api_name=name), **(extra or {})}
        if column_lineage:
            facets["columnLineage"] = ol.facet("ColumnLineageDatasetFacet", {"fields": column_lineage}, producer=PRODUCER)
        out: dict[str, Any] = {"namespace": self.ns, "name": name, "facets": facets}
        if input_facets:
            out["inputFacets"] = input_facets
        return out

    def plain(self, name: str, layer: str, description: str, *, fields: list[dict] | None = None, column_lineage: dict | None = None, ref: dict | None = None,
              namespace: str | None = None) -> dict:
        facets: dict[str, Any] = {"documentation": ol.facet("DocumentationDatasetFacet", {"description": description}, producer=PRODUCER), **ol.layer(layer, PRODUCER), **(ref or {})}
        if fields:
            facets["schema"] = ol.facet("SchemaDatasetFacet", {"fields": fields}, producer=PRODUCER)
        if column_lineage:
            facets["columnLineage"] = ol.facet("ColumnLineageDatasetFacet", {"fields": column_lineage}, producer=PRODUCER)
        return {"namespace": namespace or self.ns, "name": name, "facets": facets}

    # ---- jobs
    def job(self, name: str, layer: str, description: str, started: datetime, took: float, *, inputs: list[dict], outputs: list[dict], ref: dict | None = None,
            run_facets: dict | None = None) -> None:
        run_id = str(uuid.uuid4())
        facets = {"documentation": ol.facet("DocumentationJobFacet", {"description": description}, producer=PRODUCER), **ol.layer(layer, PRODUCER), **(ref or {})}
        self.events.append(ol.event("START", self.ns, name, run_id, started, job_facets=facets, run_facets=run_facets, producer=PRODUCER))
        self.events.append(ol.event("COMPLETE", self.ns, name, run_id, started + timedelta(seconds=max(took, 0.001)), inputs=inputs, outputs=outputs, job_facets=facets,
                                    run_facets=run_facets, producer=PRODUCER))
        self.counts[layer] += 1

    # ---- Auto Ontology builds
    def builds(self) -> None:
        by_set: dict[str, list[tuple[Any, dict, dict]]] = {}
        sets = {}
        for name, evidence in self.evidence.items():
            if name not in self.types:
                continue
            by_set.setdefault(evidence["set_id"], []).append((name, evidence, self.draft_of(evidence["item_id"])))
            sets[evidence["set_id"]] = evidence
        for set_id, rows in by_set.items():
            meta = sets[set_id]
            generator = meta.get("generator") or {}
            source_label = generator.get("source_label") or "关系库"
            source_ns = f"source:{source_label}"[:200]
            tables: dict[str, dict] = {}
            outputs = []
            for name, evidence, draft in rows:
                table = evidence.get("source_table")
                if table and table not in tables:
                    columns = evidence.get("columns") or []
                    tables[table] = self.plain(table, "source", f"来源表 {table}，主键 {', '.join(evidence.get('primary_key') or []) or '未声明'}",
                                               fields=[{"name": c["name"], "type": c.get("type")} for c in columns if c.get("name")], namespace=source_ns,
                                               ref=ol.ref("source_table", table=table, source=source_label))
                lineage_of = {}
                for prop in draft.get("properties") or []:
                    field = str(prop.get("source_field") or "")
                    if "." in field and prop.get("id") in {p["api_name"] for p in self.types[name]["properties"]}:
                        t, c = field.rsplit(".", 1)
                        lineage_of[prop["id"]] = {"inputFields": [{"namespace": source_ns, "name": t, "field": c}], "transformationDescription": "属性取自来源表的这一列"}
                outputs.append(self.type_dataset(name, column_lineage=lineage_of or None))
            started = self.when(meta.get("created_at"))
            run_facets = {"autoOntologyRun": ol.facet("AutoOntologyRunFacet", {k: v for k, v in {
                "model": generator.get("model_label"), "locality": generator.get("locality"), "seconds": generator.get("seconds"), "llm_calls": generator.get("llm_calls"),
                "candidate_set": meta.get("set_title"), "sample": generator.get("sample"), "types": len(rows)}.items() if v is not None}, custom=True, producer=PRODUCER)}
            self.job(f"autoontology.{set_id[:8]}", "build", f"Auto Ontology 候选集“{meta.get('set_title')}”：{len(rows)} 个对象类型经人工审阅后发布",
                     started, float(generator.get("seconds") or 1), inputs=list(tables.values()), outputs=outputs, ref=ol.ref("candidate_set", ontology_id=self.ontology_id, set_id=set_id),
                     run_facets=run_facets)

    def draft_of(self, item_id: str) -> dict:
        from app.models.v2.ontology_candidates import OntologyCandidateItem

        item = self.db.get(OntologyCandidateItem, item_id)
        return (item.final or item.draft or {}) if item else {}

    def when(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return self.now

    # ---- rules, alerts, decision cases
    def rules(self) -> None:
        cases = self.db.query(DecisionCase).filter(DecisionCase.ontology_id == self.ontology_id).all()
        for rule in self.db.query(RuleResource).filter(RuleResource.ontology_id == self.ontology_id).order_by(RuleResource.api_name).all():
            if rule.status == "archived":
                continue
            version = (self.db.query(RuleVersion).filter(RuleVersion.rule_id == rule.id, RuleVersion.version == rule.current_version).first()
                       or self.db.query(RuleVersion).filter(RuleVersion.rule_id == rule.id).order_by(RuleVersion.version.desc()).first())
            logic = (version.logic if version else None) or {}
            source_type = ((logic.get("input") or {}).get("type_ref") or {}).get("api_name")
            if source_type not in self.types:
                continue
            names = [p["api_name"] for p in self.types[source_type]["properties"]]
            used = rule_properties(logic)
            known = lambda items: [p for p in items if p in names]  # noqa: E731
            filters, series, evidence = known(used["filters"]), known(used["series"]), known(used["evidence"])
            identity, _ = identity_property(source_type, names, self.evidence.get(source_type, {}).get("primary_key") or [])
            subject_inputs = ([identity] if identity else []) + filters
            alerts = self.db.query(RuleAlert).filter(RuleAlert.rule_id == rule.id).all()
            by_status = Counter(a.status for a in alerts)
            ref_ns = self.ns

            def feeds(*columns: str) -> dict:
                return {"inputFields": [{"namespace": ref_ns, "name": source_type, "field": c} for c in dict.fromkeys(columns)]}

            alert_lineage: dict[str, dict] = {}
            if subject_inputs:
                alert_lineage["subject"] = {**feeds(*subject_inputs), "transformationDescription": "规则的筛选条件决定检查哪些对象"}
            if series:
                alert_lineage["window"] = {**feeds(*series), "transformationDescription": "监控的时间序列，按窗口取聚合后与阈值比较"}
                alert_lineage["peak"] = {**feeds(*series), "transformationDescription": "越线期间窗口聚合值的最大值"}
            for column in evidence:
                alert_lineage[f"evidence.{column}"] = {**feeds(column), "transformationDescription": "告警里附带的证据属性"}
            alert_fields = [{"name": "subject", "type": "STRING"}, {"name": "severity", "type": "STRING"}, {"name": "status", "type": "STRING"}]
            alert_fields += [{"name": "window", "type": "TIMESTAMP"}, {"name": "peak", "type": "DOUBLE"}] if series else []
            alert_fields += [{"name": f"evidence.{c}", "type": "STRING"} for c in evidence]
            alert_name = f"alerts.{rule.api_name}"
            summary = "，".join(f"{status} {count}" for status, count in sorted(by_status.items())) or "还没有告警"
            alert_set = self.plain(alert_name, "alert", f"规则“{rule.display_name}”产生的告警：共 {len(alerts)} 条（{summary}）", fields=alert_fields, column_lineage=alert_lineage or None,
                                   ref=ol.ref("alerts", ontology_id=self.ontology_id, rule_id=rule.id))
            started = self.when(rule.last_scheduled_at or rule.updated_at)
            self.job(f"rule.{rule.api_name}", "rule", f"{rule.display_name}（版本 {version.version if version else rule.current_version}，{rule.status}）：{rule.description or '按规则逻辑检查对象'}",
                     started, 1.0, inputs=[self.type_dataset(source_type)], outputs=[alert_set], ref=ol.ref("rule", ontology_id=self.ontology_id, rule_id=rule.id, api_name=rule.api_name),
                     run_facets={"ruleRun": ol.facet("RuleRunFacet", {"status": rule.status, "alerts": len(alerts), "schedule_minutes": rule.schedule_minutes,
                                                                     "properties_read": sorted({*filters, *series, *evidence})}, custom=True, producer=PRODUCER)})
            alert_ids = {a.id for a in alerts}
            mine = [c for c in cases if (c.context_json or {}).get("alert_id") in alert_ids]
            action_id = (logic.get("action") or {}).get("action_id")
            action = self.db.get(OntologyActionType, action_id) if action_id else None
            if mine or action:
                states = Counter(c.status for c in mine)
                case_lineage = {"target": {"inputFields": [{"namespace": self.ns, "name": alert_name, "field": "subject"}], "transformationDescription": "处置单的对象就是告警的对象"}}
                if series:
                    case_lineage["reason"] = {"inputFields": [{"namespace": self.ns, "name": alert_name, "field": f} for f in ("window", "peak")], "transformationDescription": "处置理由写明越线的窗口和峰值"}
                cases_set = self.plain(f"cases.{rule.api_name}", "decision", f"由告警提交的处置单：共 {len(mine)} 个（{'，'.join(f'{s} {n}' for s, n in sorted(states.items())) or '还没有'}）",
                                       fields=[{"name": "target", "type": "STRING"}, {"name": "reason", "type": "STRING"}, {"name": "status", "type": "STRING"}], column_lineage=case_lineage,
                                       ref=ol.ref("decision_cases", ontology_id=self.ontology_id))
                self.job(f"decision.{rule.api_name}", "decision", f"告警提交处置审核，建议动作：{action.name if action else '未绑定'}；由审核人批准后执行",
                         started + timedelta(seconds=2), 1.0, inputs=[alert_set], outputs=[cases_set], ref=ol.ref("decision_cases", ontology_id=self.ontology_id))

    # ---- the analyses that were run on the objects
    def analyses(self) -> None:
        latest: dict[tuple[str, str], AnalysisRun] = {}
        rows = (self.db.query(AnalysisRun).filter(AnalysisRun.dataset_id.like(f"ontology:{self.ontology_id}:%"), AnalysisRun.kind.in_(("health", "resolution")))
                .order_by(AnalysisRun.created_at.desc()).limit(300).all())
        for row in rows:
            object_type = (row.params or {}).get("object_type")
            if object_type in self.types:
                latest.setdefault((row.kind, object_type), row)
        for (kind, object_type), row in sorted(latest.items()):
            started = self.when(row.created_at)
            result = row.result or {}
            if kind == "health":
                quality = {"dataQualityAssertions": ol.facet("DataQualityAssertionsDatasetFacet", {"assertions": ol.assertions(result)}, producer=PRODUCER),
                           "dataQualityMetrics": ol.facet("DataQualityMetricsInputDatasetFacet", {"rowCount": (result.get("meta") or {}).get("object_count", 0)}, producer=PRODUCER)}
                stats = result.get("statistics") or {}
                report = self.plain(f"health.{object_type}", "analysis", f"{object_type} 的数据健康报告：{stats.get('successful_expectations')}/{stats.get('evaluated_expectations')} 条期望通过",
                                    ref=ol.ref("analysis_run", ontology_id=self.ontology_id, run_id=row.id, run_kind=kind, object_type=object_type))
                self.job(f"validate.{object_type}", "analysis", "按本体推出的期望逐条检查对象（照 Great Expectations）", started, 1.0,
                         inputs=[self.type_dataset(object_type, input_facets=quality)], outputs=[report],
                         ref=ol.ref("analysis_run", ontology_id=self.ontology_id, run_id=row.id, run_kind=kind, object_type=object_type))
            else:
                settings = result.get("settings") or {}
                used = [c["column"] for c in settings.get("comparisons") or [] if c.get("column")]
                summary = result.get("summary") or {}
                entities = self.plain(f"entities.{object_type}", "analysis", f"{object_type} 消歧后的 {summary.get('entities_found')} 个实体（{summary.get('records')} 条记录）",
                                      fields=[{"name": "entity_id", "type": "BIGINT"}],
                                      column_lineage={"entity_id": {"inputFields": [{"namespace": self.ns, "name": object_type, "field": c} for c in used],
                                                                    "transformationDescription": "匹配概率高于阈值的记录对，取连通分量"}} if used else None,
                                      ref=ol.ref("analysis_run", ontology_id=self.ontology_id, run_id=row.id, run_kind=kind, object_type=object_type))
                self.job(f"resolve.{object_type}", "analysis", "概率式记录匹配，识别同一实体的多个对象（照 Splink）", started, float(summary.get("run_time_ms") or 1000) / 1000,
                         inputs=[self.type_dataset(object_type)], outputs=[entities],
                         ref=ol.ref("analysis_run", ontology_id=self.ontology_id, run_id=row.id, run_kind=kind, object_type=object_type))


def build_events(db: Session, user: Any, ontology_id: str, *, now: datetime | None = None) -> tuple[list[dict], dict]:
    builder = Builder(db, user, ontology_id, now or datetime.now(timezone.utc))
    if builder.project is None:
        raise lineage.LineageError("unknown ontology")
    builder.builds()
    builder.rules()
    builder.analyses()
    return builder.events, {"namespace": builder.ns, "jobs": dict(builder.counts), "types": len(builder.types)}


def generate(db: Session, user: Any, ontology_id: str) -> dict:
    """Write the ontology's lineage, replacing the earlier copy."""
    events, summary = build_events(db, user, ontology_id)
    if not events:
        raise lineage.LineageError("这个本体还没有可以画出血缘的内容：没有发布过的 Auto Ontology 候选，也没有规则或分析记录")
    for event in events:
        lineage.validate_event(event)
    removed = lineage.reset(db, source=source_tag(ontology_id))
    rows = lineage.ingest(db, events, user.id, source=source_tag(ontology_id))
    return {**summary, "events": len(rows), "replaced": removed, "source": source_tag(ontology_id)}
