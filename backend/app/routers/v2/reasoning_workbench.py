"""Semantica preview, source-backed proof and explicit instance-graph projection."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.deps import get_current_user, require_editor
from app.models.user import User
from app.models.ontology import OntologyProject
from app.models.logic import LogicRule
from app.models.v2.construction import EvidenceRef
from app.models.v2.action import OntologyActionRun
from app.models.v2.reasoning import ReasoningRun
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.dataset_authorization import require_dataset_access
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError
from app.services.v2.reasoning_authorization import reasoning_permission_digest
from app.services.v2.action_runtime import (
    complete_action_run,
    create_action_run_record,
    fail_action_run,
    transition_action_run,
)
from app.services.v2.object_query.normalize import stable_hash

router = APIRouter(dependencies=[Depends(get_current_user)])
ATOM = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(([^()]*)\)$")
TERM = re.compile(r"^\??[A-Za-z0-9_:./#-]+$")

class ReasoningRequest(BaseModel):
    facts: list[str] = Field(default_factory=list, max_length=2000)
    rules: list[str] = Field(default_factory=list, max_length=50)
    apply_to_graph: bool = False


class ReasoningApplyCommand(BaseModel):
    client_request_id: str = Field(min_length=1, max_length=200)

    class Config:
        extra = "forbid"

def get_db():
    with SessionLocal() as db:
        yield db

def _ontology_scope(db, ontology_id, user):
    item = db.get(OntologyProject, ontology_id)
    if item is None:
        raise HTTPException(404, "本体不存在或无访问权限")
    try:
        access = require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError as exc:
        raise HTTPException(404, "本体不存在或无访问权限") from exc
    return item, access


def _can_manage(project, user):
    return bool(project and (user.role == "admin" or str(project.created_by) == str(user.id)))


def project(db, ontology_id, user):
    return _ontology_scope(db, ontology_id, user)[0]

def atom(text: str, variables: bool = False) -> str:
    match = ATOM.fullmatch(text.strip())
    if not match:
        raise ValueError(f"无效事实表达式：{text}")
    args = [s.strip() for s in match[2].split(",")]
    if not 1 <= len(args) <= 2 or any(not TERM.fullmatch(s) or (s.startswith("?") and not variables) for s in args):
        raise ValueError(f"仅支持一元/二元谓词及安全标识符：{text}")
    return f"{match[1]}({', '.join(args)})"

def normalize(facts, rules):
    facts = list(dict.fromkeys(atom(f) for f in facts))
    normalized, arities, all_atoms = [], {}, list(facts)
    for rule in rules:
        match = re.fullmatch(r"IF\s+(.+?)\s+THEN\s+(.+)", rule.strip(), re.I)
        if not match:
            raise ValueError(f"规则必须采用 IF … AND … THEN …：{rule}")
        conditions = [atom(s, True) for s in re.split(r"\s+AND\s+", match[1], flags=re.I)]
        conclusion = atom(match[2], True)
        if not set(re.findall(r"\?[\w]+", conclusion)) <= set(re.findall(r"\?[\w]+", " ".join(conditions))):
            raise ValueError("结论含有未在前提中绑定的变量")
        if len(conditions) > 4:
            raise ValueError("单条规则最多四个前提")
        normalized.append(f"IF {' AND '.join(conditions)} THEN {conclusion}")
        all_atoms.extend(conditions + [conclusion])
    for value in all_atoms:
        match = ATOM.fullmatch(value)
        arity = len(match[2].split(','))
        if match[1] in arities and arities[match[1]] != arity:
            raise ValueError(f"谓词参数数量不一致：{match[1]}")
        arities[match[1]] = arity
    if not facts or not normalized:
        raise ValueError("请先加载事实并填写至少一条规则")
    return facts, normalized

def _dataset_accessible(db: Session, dataset_id: str, user: User) -> bool:
    try:
        require_dataset_access(db, dataset_id, user)
    except HTTPException:
        return False
    return True


def _permission_digest(db: Session, ontology_id: str, user: User, project_row, access) -> str:
    return reasoning_permission_digest(db, ontology_id, user, project_row, access)


def _reasoning_graph(ontology_id: str, db: Session, user: User) -> dict:
    _ontology_scope(db, ontology_id, user)
    service = FalkorDBService()
    if not service.available:
        raise HTTPException(503, "FalkorDB 不可用，未使用备用内存图")
    data = service.get_graph_data(ontology_id, limit=500)
    # Preserve the relation-first sample needed for cross-entity rules, then
    # run the same object/property/link projection used by the graph API.
    try:
        graph = service._graph(ontology_id)
        rel_result = graph.query(
            "MATCH (a:Instance)-[r]->(b:Instance) "
            "RETURN a, b, type(r), r LIMIT 500"
        )
        relation_edges = []
        relation_nodes = {}
        for source_node, target_node, rel_type, rel in rel_result.result_set:
            source = service._node(source_node)
            target = service._node(target_node)
            if not source.get("id") or not target.get("id"):
                continue
            relation_nodes[str(source["id"])] = source
            relation_nodes[str(target["id"])] = target
            relation_edges.append({"source": str(source["id"]), "target": str(target["id"]),
                                   "type": str(rel_type), "properties": dict(getattr(rel, "properties", {}) or {})})
        if relation_edges:
            data["nodes"] = list(relation_nodes.values())[:500]
            selected_ids = {node["id"] for node in data["nodes"]}
            data["edges"] = [edge for edge in relation_edges if edge["source"] in selected_ids and edge["target"] in selected_ids]
    except Exception:
        pass
    from app.routers.v2.graph import _authorize_graph_data
    return _authorize_graph_data(db, ontology_id, data, user)


def _authorized_assertion_ids(ontology_id: str, data: dict) -> set[str]:
    assertion_ids: set[str] = set()
    for node in data.get("nodes", []):
        identifier = str(node.get("id") or "")
        if identifier:
            assertion_ids.add(identifier)
            if identifier.startswith("FactoryNet:Observation:"):
                identity = identifier.removeprefix("FactoryNet:Observation:")
                digest = hashlib.sha256(f"{ontology_id}:Observation:{identity}".encode()).hexdigest()[:20]
                assertion_ids.add(f"temporal:{ontology_id}:Observation:{digest}")
    for edge in data.get("edges", []):
        props = edge.get("properties") if isinstance(edge.get("properties"), dict) else {}
        assertion_id = props.get("assertion_id")
        if assertion_id:
            assertion_ids.add(str(assertion_id))
            continue
        try:
            assertion_ids.add(atom(f"{edge['type']}({edge['source']}, {edge['target']})"))
        except (KeyError, ValueError):
            continue
    return assertion_ids


def _build_reasoning_inputs(ontology_id: str, db: Session, user: User) -> dict:
    project_row, access = _ontology_scope(db, ontology_id, user)
    data = _reasoning_graph(ontology_id, db, user)
    visible_assertions = _authorized_assertion_ids(ontology_id, data)
    manager = _can_manage(project_row, user)
    refs = {}
    dataset_access: dict[str, bool] = {}
    rows = db.query(EvidenceRef).filter(EvidenceRef.ontology_id == ontology_id).all()
    for row in rows:
        if str(row.assertion_id or "") not in visible_assertions:
            continue
        dataset_id = str(row.source_dataset_id or "")
        if not dataset_id and not manager:
            # Older evidence references may not identify their source resource.
            # Keep those provenance details owner-only until they can be scoped.
            continue
        if dataset_id:
            if dataset_id not in dataset_access:
                dataset_access[dataset_id] = _dataset_accessible(db, dataset_id, user)
            if not dataset_access[dataset_id]:
                continue
        reference = {
            "evidence_ref_id": row.id, "source_file": row.source_file,
            "source_row": row.source_row_id, "source_version": row.source_version,
            "source_dataset_id": row.source_dataset_id, "source": "source_record",
            "evidence_text": row.evidence_text, "content_hash": row.content_hash,
        }
        if not manager:
            # Keep the immutable provenance pointer while withholding source
            # text and file/row locators from non-owners.
            reference.pop("evidence_text", None)
            reference.pop("source_file", None)
            reference.pop("source_row", None)
        refs.setdefault(row.assertion_id, []).append(reference)
    evidence = {}

    def source_refs(identifier):
        matches = refs.get(identifier, [])
        if not matches and identifier.startswith("FactoryNet:Observation:"):
            identity = identifier.removeprefix("FactoryNet:Observation:")
            digest = hashlib.sha256(f"{ontology_id}:Observation:{identity}".encode()).hexdigest()[:20]
            matches = refs.get(f"temporal:{ontology_id}:Observation:{digest}", [])
        return matches

    def add(fact, assertion_id, anchors=()):
        matches = source_refs(assertion_id)
        if not matches and anchors:
            matches = [dict(row, evidence_scope="source_observation_for_relationship")
                       for anchor in anchors for row in source_refs(anchor)]
        evidence[fact] = [{**row, "fact": fact} for row in matches] or [{"fact": fact, "source": "graph_without_source_reference"}]

    for node in data.get("nodes", []):
        try:
            add(atom(f"{node['entity_type']}({node['id']})"), str(node["id"]))
        except (KeyError, ValueError):
            continue
    for edge in data.get("edges", []):
        if (edge.get("properties") or {}).get("derived"):
            continue
        try:
            fact = atom(f"{edge['type']}({edge['source']}, {edge['target']})")
        except (KeyError, ValueError):
            continue
        anchors = ()
        if str(edge["source"]).startswith("FactoryNet:Observation:") and edge["type"] in ("IN_PHASE", "HAS_TOOL_CONDITION", "OBSERVED_ON", "HAS_INSPECTION"):
            anchors = (str(edge["source"]),)
        add(fact, (edge.get("properties") or {}).get("assertion_id", fact), anchors)

    rules = []
    if manager:
        rules = [
            row.formula for row in db.query(LogicRule).filter(
                LogicRule.ontology_id == ontology_id,
                LogicRule.enabled.is_(True),
            ).order_by(LogicRule.id).all()
            if (row.formula or "").startswith("IF ") and re.fullmatch(
                r"IF\s+[^<>]+\([^<>]+\)(?:\s+AND\s+[^<>]+\([^<>]+\))*\s+THEN\s+[^<>]+\([^<>]+\)",
                row.formula.strip(), re.I,
            )
        ]
    return {
        "facts": list(evidence), "evidence": evidence, "rules": rules, "available": True,
        "can_apply": manager, "can_run": user.role in {"admin", "editor"},
        "node_count": len(data.get("nodes") or []), "edge_count": len(data.get("edges") or []),
        "sample_limit": 500, "total_instances": data.get("total_instances"),
        "truncated": data.get("total_instances", 0) > len(data.get("nodes") or []),
        "_access_digest": _permission_digest(db, ontology_id, user, project_row, access),
    }


@router.get("/{ontology_id}/reasoning/inputs")
def reasoning_inputs(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    result = _build_reasoning_inputs(ontology_id, db, user)
    result.pop("_access_digest", None)
    return result

def _compat_parse_atom(text):
    match = ATOM.fullmatch(text.strip())
    if not match:
        raise ValueError(f"无效事实表达式：{text}")
    return match[1], [item.strip() for item in match[2].split(',')]

def _compat_unify(pattern, value, bindings):
    if pattern.startswith('?'):
        if pattern in bindings:
            return bindings[pattern] == value
        bindings[pattern] = value
        return True
    return pattern == value

def _compat_infer(facts, rules, evidence):
    """Small deterministic forward-chaining substitute for optional Semantica.

    The application pins Semantica in the production image, but local installs
    and health checks may deliberately omit that optional package.  This path
    keeps the same one-grounded-proof and source-evidence contract instead of
    returning an empty or fabricated result.
    """
    known = list(dict.fromkeys(facts))
    known_set = set(known)
    proofs = {}
    parsed_rules = []
    for index, rule_text in enumerate(rules, start=1):
        match = re.fullmatch(r"IF\s+(.+?)\s+THEN\s+(.+)", rule_text.strip(), re.I)
        if not match:
            raise ValueError(f"规则必须采用 IF … AND … THEN …：{rule_text}")
        conditions = [item.strip() for item in re.split(r"\s+AND\s+", match[1], flags=re.I)]
        conclusion = match[2].strip()
        parsed_rules.append((index, conditions, conclusion))

    converged = False
    for _ in range(50):
        additions = []
        for rule_index, conditions, conclusion in parsed_rules:
            candidates = [({}, [])]
            for condition in conditions:
                predicate, patterns = _compat_parse_atom(condition)
                next_candidates = []
                for bindings, premises in candidates:
                    for fact in known:
                        fact_predicate, values = _compat_parse_atom(fact)
                        if fact_predicate != predicate or len(values) != len(patterns):
                            continue
                        candidate_bindings = dict(bindings)
                        if all(_compat_unify(pattern, value, candidate_bindings)
                               for pattern, value in zip(patterns, values)):
                            next_candidates.append((candidate_bindings, premises + [fact]))
                candidates = next_candidates
                if not candidates:
                    break
            conclusion_predicate, conclusion_patterns = _compat_parse_atom(conclusion)
            for bindings, premises in candidates:
                values = [bindings.get(pattern, pattern) for pattern in conclusion_patterns]
                grounded = f"{conclusion_predicate}({', '.join(values)})"
                if grounded in known_set or grounded in proofs:
                    continue
                if len(known) + len(additions) >= 10000:
                    raise ValueError("推理超出工作台规模限制，未保存结果")
                proofs[grounded] = {
                    "conclusion": grounded,
                    "rule_id": f"compat-rule-{rule_index}",
                    "rule": f"IF {' AND '.join(conditions)} THEN {conclusion}",
                    "premises": premises,
                    "bindings": bindings,
                    "proof_scope": "one_grounded_derivation",
                    "evidence": [],
                }
                additions.append(grounded)
        if not additions:
            converged = True
            break
        known.extend(additions)
        known_set.update(additions)
    if not converged:
        raise ValueError("推理达到迭代上限，尚未收敛，未保存结果")

    def leaves(fact, seen):
        if fact in seen:
            raise ValueError("推理证据循环")
        proof = proofs.get(fact)
        if proof:
            return [item for premise in proof['premises'] for item in leaves(premise, seen | {fact})]
        rows = evidence.get(fact)
        if rows:
            return [{**row, "fact": row.get("fact", fact)} for row in rows]
        return [{"fact": fact, "source": "manual_input"}]

    for proof in proofs.values():
        proof['evidence'] = list({
            (item.get('evidence_ref_id'), item['fact']): item
            for item in leaves(proof['conclusion'], set())
        }.values())
    return list(proofs.values())

def infer(facts, rules, evidence):
    try:
        from semantica.reasoning import Reasoner
    except ModuleNotFoundError as exc:
        if exc.name and not exc.name.startswith('semantica'):
            raise
        return _compat_infer(facts, rules, evidence)
    # Capture ONE grounded proof before upstream merges alternative premises.
    class TracedReasoner(Reasoner):
        def __init__(self):
            super().__init__(max_iterations=50)
            self.proofs = {}
        def _match_rule(self, rule):
            matches = super()._match_rule(rule)
            if len(matches) > 10000 or len(self.facts) > 10000:
                raise ValueError("推理超出工作台规模限制，未保存结果")
            for conclusion, premises, bindings in matches:
                if conclusion not in self.facts and conclusion not in self.proofs:
                    self.proofs[conclusion] = (rule, list(premises), dict(bindings))
            return matches
    engine = TracedReasoner()
    results = engine.infer_with_results(facts, rules)
    if any(c not in engine.facts for r in engine.rules for c, _, _ in engine._match_rule(r)):
        raise ValueError("推理达到迭代上限，尚未收敛，未保存结果")
    proofs = {}
    for result in results:
        rule, premises, bindings = engine.proofs[result.conclusion]
        proofs[result.conclusion] = {
            "conclusion": result.conclusion, "rule_id": rule.rule_id,
            "rule": f"IF {' AND '.join(rule.conditions)} THEN {rule.conclusion}",
            "premises": premises, "bindings": bindings, "proof_scope": "one_grounded_derivation", "evidence": [],
        }
    def leaves(fact, seen):
        if fact in seen:
            raise ValueError("推理证据循环")
        if fact in proofs:
            return [e for p in proofs[fact]['premises'] for e in leaves(p, seen | {fact})]
        return evidence.get(fact) or [{"fact": fact, "source": "manual_input"}]
    for fact, proof in proofs.items():
        proof['evidence'] = list({(e.get('evidence_ref_id'), e['fact']): e for e in leaves(fact, set())}.values())
    return list(proofs.values())

def reasoning_engine_name():
    try:
        from semantica.reasoning import Reasoner  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name and not exc.name.startswith('semantica'):
            raise
        return "deterministic-compatible (Semantica package unavailable)"
    return "semantica.reasoning.Reasoner"

def _project_edges(ontology_id, run_id, conclusions, db: Session, user: User):
    service = FalkorDBService()
    if not service.available:
        return {"status": "unavailable", "written": 0, "reason": "FalkorDB 不可用；预览记录已保留，可重试"}
    rows = []
    for item in conclusions:
        match = ATOM.fullmatch(item['conclusion'])
        args = [s.strip() for s in match[2].split(',')]
        if len(args) == 2:
            rows.append({"source": args[0], "target": args[1], "predicate": match[1],
                         "fact_id": hashlib.sha256(f"{run_id}:{item['conclusion']}".encode()).hexdigest(),
                         "conclusion": item['conclusion']})
    graph = service._graph(ontology_id)
    ids = sorted({s for row in rows for s in (row['source'], row['target'])})
    if ids:
        existing = graph.query("MATCH (n:Instance) WHERE n._instance_id IN $ids RETURN n", params={"ids": ids})
        raw_nodes = []
        for (node,) in existing.result_set:
            properties = dict(getattr(node, "properties", {}) or {})
            raw_nodes.append({"id": str(properties.get("_instance_id") or ""),
                              "entity_type": str(properties.get("_type") or "Entity"),
                              "properties": properties})
        from app.routers.v2.graph import _authorize_graph_data
        authorized = _authorize_graph_data(db, ontology_id, {"nodes": raw_nodes, "edges": []}, user)
        visible_ids = {str(node.get("id") or "") for node in authorized.get("nodes", [])}
    else:
        visible_ids = set()
    if set(ids) - visible_ids:
        return {"status": "rejected", "written": 0,
                "reason": "关系端点不存在或无当前访问权限；没有写入任何关系"}
    # Dedicated edges preserve original assertions. Stable IDs allow retries.
    if rows:
        graph.query("UNWIND $rows AS row MATCH (a:Instance {_instance_id: row.source}), (b:Instance {_instance_id: row.target}) "
                    "MERGE (a)-[r:INFERRED {fact_id: row.fact_id}]->(b) "
                    "SET r.derived = true, r.predicate = row.predicate, r.conclusion = row.conclusion, r.reasoning_run_id = $run_id",
                    params={"rows": rows, "run_id": run_id})
    return {"status": "completed", "written": len(rows), "stored_facts": len(conclusions), "unary_facts": len(conclusions) - len(rows)}

def response(run):
    payload = run.payload or {}
    public_payload = {key: value for key, value in payload.items() if not str(key).startswith("_")}
    return {"run_id": run.id, "status": run.status, **public_payload, "inferred_facts": payload.get('conclusions', []),
            "derived_fact_count": len(payload.get('conclusions', [])), "graph": payload.get('graph', {"status": "preview", "written": 0}),
            "action_run_id": payload.get("action_run_id"),
            "action_run_status": payload.get("action_run_status"),
            "created_at": run.created_at.isoformat() if run.created_at else None}


def _require_run_access(db: Session, ontology_id: str, run: ReasoningRun, user: User):
    project_row, access = _ontology_scope(db, ontology_id, user)
    if _can_manage(project_row, user):
        return project_row
    if str(run.created_by or "") != str(user.id):
        raise HTTPException(404, "推理运行不存在或无访问权限")
    recorded_digest = (run.payload or {}).get("_access_digest")
    if not recorded_digest:
        raise HTTPException(404, "推理运行不存在或无访问权限")
    current_digest = _permission_digest(db, ontology_id, user, project_row, access)
    if recorded_digest != current_digest:
        raise HTTPException(409, "推理运行的权限上下文已变化，请重新运行")
    return project_row

@router.post("/{ontology_id}/reasoning/run")
def run_reasoning(ontology_id: str, body: ReasoningRequest, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    ontology, _access = _ontology_scope(db, ontology_id, user)
    if body.apply_to_graph:
        if not _can_manage(ontology, user):
            raise HTTPException(403, "只有本体所有者或管理员可以将推理结果写入图")
        raise HTTPException(409, {
            "code": "reasoning_preview_required",
            "message": "推理运行只生成预览；请先检查运行结果，再使用独立的保存操作。",
        })
    try:
        facts, rules = normalize(body.facts, body.rules)
        inputs = _build_reasoning_inputs(ontology_id, db, user)
        conclusions = infer(facts, rules, inputs['evidence'])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"Semantica 推理失败，未保存结果：{exc}")
    run = ReasoningRun(ontology_id=ontology_id, created_by=user.id, status='preview', payload={
        "facts": facts, "rules": rules, "conclusions": conclusions, "ontology_revision_id": ontology.current_revision_id,
        "rules_fired": sorted({r['rule_id'] for r in conclusions}), "engine": reasoning_engine_name(), "proof_scope": "one_grounded_derivation",
        "_access_digest": inputs["_access_digest"],
    })
    db.add(run)
    db.commit()
    return response(run)

@router.get("/{ontology_id}/reasoning/runs")
def list_runs(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    project_row, access = _ontology_scope(db, ontology_id, user)
    manager = _can_manage(project_row, user)
    query = db.query(ReasoningRun).filter(ReasoningRun.ontology_id == ontology_id)
    if not manager:
        query = query.filter(ReasoningRun.created_by == str(user.id))
    runs = query.order_by(ReasoningRun.created_at.desc()).limit(30).all()
    current_digest = None if manager else _permission_digest(db, ontology_id, user, project_row, access)
    return [{
        "run_id": row.id,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "derived_fact_count": len((row.payload or {}).get("conclusions", [])) if manager or (row.payload or {}).get("_access_digest") == current_digest else None,
        "authorization_status": "current" if manager or (row.payload or {}).get("_access_digest") == current_digest else "stale_or_unpinned",
    } for row in runs]

@router.get("/{ontology_id}/reasoning/graph")
def instance_graph(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    data = _reasoning_graph(ontology_id, db, user)
    for edge in data.get('edges', []):
        props = edge.get('properties') or {}
        edge['id'] = props.get('fact_id') or edge['id']
        edge['label'] = props.get('predicate') or edge['type']
    return data

def _reasoning_action_run_query(db, *, ontology_id: str, actor_id: str, scope: str, request_id: str):
    return db.query(OntologyActionRun).filter_by(
        ontology_id=ontology_id,
        executed_by=actor_id,
        idempotency_scope=scope,
        client_request_id=request_id,
    ).populate_existing().with_for_update().first()


@router.post("/{ontology_id}/reasoning/runs/{run_id}/apply")
def apply_reasoning(
    ontology_id: str,
    run_id: str,
    body: ReasoningApplyCommand,
    db: Session = Depends(get_db),
    user: User = Depends(require_editor),
):
    ontology = project(db, ontology_id, user)
    if not _can_manage(ontology, user):
        raise HTTPException(403, "只有本体所有者或管理员可以将推理结果写入图")
    run = db.query(ReasoningRun).filter_by(id=run_id, ontology_id=ontology_id).first()
    if not run:
        raise HTTPException(404, "推理运行不存在")
    _require_run_access(db, ontology_id, run, user)
    request_id = body.client_request_id.strip()
    if not request_id:
        raise HTTPException(422, {"code": "request_id_required", "message": "保存操作需要非空请求编号"})

    ontology, access = _ontology_scope(db, ontology_id, user)
    payload = dict(run.payload or {})
    pinned_revision = payload.get("ontology_revision_id")
    if not pinned_revision or pinned_revision != ontology.current_revision_id:
        raise HTTPException(409, {
            "code": "reasoning_revision_changed",
            "message": "推理结果固定的本体修订已过期；请在当前修订下重新运行。",
        })
    permission_digest = _permission_digest(db, ontology_id, user, ontology, access)
    if not payload.get("_access_digest") or payload.get("_access_digest") != permission_digest:
        raise HTTPException(409, {
            "code": "reasoning_permission_changed",
            "message": "推理结果固定的权限范围已变化；请重新加载当前事实并运行。",
        })

    conclusions = payload.get("conclusions") or []
    conclusion_digest = stable_hash(conclusions)
    request_digest = stable_hash({
        "ontology_id": ontology_id,
        "reasoning_run_id": run.id,
        "actor_id": str(user.id),
        "ontology_revision_id": pinned_revision,
        "permission_digest": permission_digest,
        "conclusion_digest": conclusion_digest,
    })
    preview_digest = stable_hash({
        "request_digest": request_digest,
        "conclusion_count": len(conclusions),
        "target": "inferred_graph_projection",
    })
    scope = f"reasoning-projection:{run.id}"
    action_run = _reasoning_action_run_query(
        db, ontology_id=ontology_id, actor_id=str(user.id), scope=scope, request_id=request_id,
    )

    if action_run is not None and action_run.input_digest != request_digest:
        raise HTTPException(409, {
            "code": "idempotency_conflict",
            "message": "请求编号已用于不同的推理结果或权限上下文。",
        })
    if action_run is None:
        action_run = create_action_run_record(
            db=db,
            action_type_id=None,
            ontology_id=ontology_id,
            parameters={
                "reasoning_run_id": run.id,
                "conclusion_count": len(conclusions),
                "conclusion_digest": conclusion_digest,
            },
            execution_context={
                "source": "reasoning_workbench",
                "target": "inferred_graph_projection",
                "reasoning_run_id": run.id,
                "ontology_revision_id": pinned_revision,
            },
            initial_status="running",
            requester_id=str(user.id),
            executor_id=str(user.id),
            client_request_id=request_id,
            idempotency_scope=scope,
            input_digest=request_digest,
            preview_digest=preview_digest,
            permission_digest=permission_digest,
            projection_status="running",
        )
        db.add(action_run)
        try:
            # Persist the request claim before the graph projection. A retry
            # can then resume the same deterministic fact IDs after a crash.
            db.commit()
        except IntegrityError:
            db.rollback()
            action_run = _reasoning_action_run_query(
                db, ontology_id=ontology_id, actor_id=str(user.id), scope=scope, request_id=request_id,
            )
            if action_run is None:
                raise HTTPException(409, {
                    "code": "action_request_claim_conflict",
                    "message": "保存请求已被并发请求占用；请使用原请求编号重试。",
                })
            if action_run.input_digest != request_digest:
                raise HTTPException(409, {
                    "code": "idempotency_conflict",
                    "message": "请求编号已用于不同的推理结果或权限上下文。",
                })
        else:
            action_run = _reasoning_action_run_query(
                db, ontology_id=ontology_id, actor_id=str(user.id), scope=scope, request_id=request_id,
            )
            if action_run is None:
                raise HTTPException(500, {
                    "code": "action_request_claim_missing",
                    "message": "保存请求记录未能重新读取。",
                })

    if action_run.status == "completed":
        run = db.query(ReasoningRun).filter_by(
            id=run_id, ontology_id=ontology_id,
        ).populate_existing().first()
        if run is None:
            raise HTTPException(404, "推理运行不存在")
        return response(run)
    if action_run.status in {"failed", "cancelled", "reverted"}:
        raise HTTPException(409, {
            "code": "action_request_incomplete",
            "message": "该保存请求已有失败或撤销记录；如需重新尝试，请生成新的请求编号。",
            "action_run_id": action_run.id,
        })
    if action_run.status == "reconciliation_required":
        transition_action_run(
            db, action_run, "running", actor_id=str(user.id), actor_kind="user",
        )
    elif action_run.status == "queued":
        transition_action_run(
            db, action_run, "running", actor_id=str(user.id), actor_kind="user",
        )
    elif action_run.status != "running":
        raise HTTPException(409, {
            "code": "action_request_unavailable",
            "message": "保存请求当前状态不支持继续执行。",
            "action_run_id": action_run.id,
        })

    run = db.query(ReasoningRun).filter_by(
        id=run_id, ontology_id=ontology_id,
    ).populate_existing().with_for_update().first()
    if run is None:
        raise HTTPException(404, "推理运行不存在")
    try:
        graph = _project_edges(ontology_id, run.id, conclusions, db, user)
        if graph["status"] != "completed":
            attempted_at = datetime.now(timezone.utc)
            failure_code = (
                "reasoning_projection_unavailable"
                if graph["status"] == "unavailable"
                else "reasoning_projection_rejected"
            )
            fail_action_run(
                db, action_run, failure_code,
                actor_id=str(user.id), actor_kind="user",
            )
            action_run.projection_status = "failed"
            action_run.projection_error = failure_code
            action_run.projection_attempts = (action_run.projection_attempts or 0) + 1
            action_run.projection_last_attempt_at = attempted_at
            action_run.after_digest = stable_hash(graph)
            action_run.side_effect_results = [graph]
            run.payload = {
                **dict(run.payload or {}),
                "graph": graph,
                "action_run_id": action_run.id,
                "action_run_status": action_run.status,
            }
            db.commit()
            return response(run)

        completed_at = datetime.now(timezone.utc)
        before_snapshot = {
            "reasoning_run_id": run.id,
            "projection_status": "not_applied",
        }
        after_snapshot = {
            "reasoning_run_id": run.id,
            "ontology_revision_id": pinned_revision,
            "conclusion_digest": conclusion_digest,
            "projection_status": "completed",
        }
        complete_action_run(
            db,
            action_run,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            side_effect_results=[graph],
            actor_id=str(user.id),
            actor_kind="user",
            completed_at=completed_at,
        )
        action_run.before_digest = stable_hash(before_snapshot)
        action_run.after_digest = stable_hash(after_snapshot)
        action_run.projection_status = "completed"
        action_run.projection_attempts = (action_run.projection_attempts or 0) + 1
        action_run.projection_last_attempt_at = completed_at
        run.status = "applied"
        run.payload = {
            **dict(run.payload or {}),
            "graph": graph,
            "action_run_id": action_run.id,
            "action_run_status": action_run.status,
        }
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        action_run = db.get(OntologyActionRun, action_run.id)
        run = db.query(ReasoningRun).filter_by(id=run_id, ontology_id=ontology_id).first()
        if action_run is not None and action_run.status in {"running", "reconciliation_required"}:
            if action_run.status == "running":
                transition_action_run(
                    db, action_run, "reconciliation_required",
                    actor_id=str(user.id), actor_kind="user",
                )
            action_run.projection_status = "reconciliation_required"
            action_run.projection_error = "reasoning_projection_outcome_unknown"
            action_run.error = "reasoning_projection_outcome_unknown"
            action_run.projection_attempts = (action_run.projection_attempts or 0) + 1
            action_run.projection_last_attempt_at = datetime.now(timezone.utc)
            if run is not None:
                run.payload = {
                    **dict(run.payload or {}),
                    "graph": {
                        "status": "reconciliation_required",
                        "reason": "图投影结果未知；请使用相同请求编号重试并核对。",
                    },
                    "action_run_id": action_run.id,
                    "action_run_status": action_run.status,
                }
            db.commit()
            raise HTTPException(409, {
                "code": "reasoning_projection_reconciliation_required",
                "message": "图投影结果尚未确认；请使用相同请求编号重试以恢复同一运行。",
                "action_run_id": action_run.id,
                "client_request_id": request_id,
            }) from exc
        db.rollback()
        raise HTTPException(500, {
            "code": "reasoning_projection_failed",
            "message": "推理结果投影失败。",
        }) from exc
    return response(run)

@router.get("/{ontology_id}/reasoning/runs/{run_id}")
def get_reasoning(ontology_id: str, run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    run = db.query(ReasoningRun).filter_by(id=run_id, ontology_id=ontology_id).first()
    if not run:
        raise HTTPException(404, "推理运行不存在")
    _require_run_access(db, ontology_id, run, user)
    return response(run)
