"""Install a person-master ontology whose Person objects contain duplicates, to run the analysis pages on a platform ontology.

The records are Splink's fake_1000 (1000 records of about 250 people, with misspelt names, missing values and wrong dates; MIT, see
examples/analysis). They go through the platform's own route:

  1. the table's structure is written as an Auto Ontology model-interchange document (no model is involved: the candidate set says so),
  2. the document is imported as a candidate set, every candidate is accepted and the set is published as a new ontology revision,
  3. the data steward's constraint on e-mail addresses is added to the Person definition,
  4. the 1000 records are installed as Person objects, the same way the factory twin installs its objects.

Run inside the backend container:
  python scripts/seed_person_master_demo.py --api http://127.0.0.1:8000
The administrator account comes from FIRST_ADMIN_USER / FIRST_ADMIN_PASSWORD (or --username / --password); it is never printed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
from pathlib import Path

import httpx
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.models.entity_instance import EntityInstance  # noqa: E402
from app.models.ontology import OntologyProject  # noqa: E402
from app.models.v2.ontology_candidates import OntologyCandidateSet  # noqa: E402
from app.services.v2.analysis import ontology_metadata  # noqa: E402
from app.services.v2.graph.falkordb_service import FalkorDBService  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "examples" / "analysis" / "fake_1000.csv"
NAME = "人员主数据（Splink 演示）"
TABLE = "person_master"
NAMESPACE = uuid.UUID("0b8d1c5e-6b0e-4f7a-9d8e-5a1f0c2d7e11")
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"
LABELS = {"unique_id": ("记录编号", "数据源里的记录编号"), "first_name": ("名", "名字，可能错拼或缺失"), "surname": ("姓", "姓氏，可能错拼或缺失"),
          "dob": ("出生日期", "出生日期，同一个人的多条记录里可能差一两位"), "city": ("城市", "所在城市，可能错拼或缺失"),
          "email": ("邮箱", "联系邮箱，同一个人的多条记录里可能不同"), "cluster": ("真实分组", "演示数据自带：同一个人的记录有相同的值，用来给消歧结果打分")}


def unwrap(response: httpx.Response, expected: int = 200):
    if response.status_code != expected:
        raise RuntimeError(f"{response.request.method} {response.request.url}: {response.status_code} {response.text[:800]}")
    body = response.json()
    return body.get("data", body) if isinstance(body, dict) else body


def login(client: httpx.Client, username: str, password: str) -> dict:
    body = unwrap(client.post("/api/v1/auth/login", json={"username": username, "password": password}))
    token = body.get("access_token") or (body.get("data") or {}).get("access_token")
    if not token:
        raise RuntimeError("login response had no access token")
    return {"Authorization": f"Bearer {token}"}


def ident(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, ":".join(parts)))


def interchange(frame: pd.DataFrame) -> str:
    """The table as an Auto Ontology model-interchange document: one table, one term that represents it."""
    columns, attributes = [], []
    for name in frame.columns:
        series = frame[name]
        kind = "bigint" if pd.api.types.is_integer_dtype(series) else "date" if name == "dob" else "text"
        samples = [v.item() if hasattr(v, "item") else v for v in series.dropna().drop_duplicates().head(3).tolist()]
        columns.append({"id": ident("column", name), "name": name, "description": LABELS[name][1], "type": kind, "sample_values": samples,
                        "is_nullable": bool(series.isna().any()), "is_unique": bool(series.is_unique)})
        attributes.append({"id": ident("attribute", name), "column_id": ident("column", name), "name": LABELS[name][0], "description": LABELS[name][1]})
    table = {"id": ident("table", TABLE), "name": TABLE, "description": "人员主数据：每行是一条人员记录，同一个人可能有多条", "pk": ["unique_id"], "type": "base table", "columns": columns}
    document = {"data_layer": {"databases": [{"id": ident("database"), "dialect": "csv", "schemas": [{"id": ident("schema"), "name": "public", "database_name": "fake_1000", "tables": [table]}]}],
                               "foreign_keys": []},
                "semantic_layer": {"terms": [{"id": ident("term", "Person"), "name": "Person", "description": "人员：一个真实的人，在数据源里可能有多条记录", "represents": [table["id"]],
                                              "columns_attributes": attributes}], "semantic_fks": []}}
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def publish_candidates(client: httpx.Client, headers: dict, ontology_id: str, document: str) -> dict:
    root = f"/api/v2/ontologies/{ontology_id}/ai-candidates"
    imported = unwrap(client.post(f"{root}:import", headers=headers, json={"document": document, "title": "人员主数据表 · 表结构导入"}), 201)
    set_id = imported["id"]
    with SessionLocal() as db:
        row = db.get(OntologyCandidateSet, set_id)
        # The document was written by this script from the CSV header, not generated by Auto Ontology: say so on the candidate set.
        row.generator = {**(row.generator or {}), "tool": "表结构导入脚本（按 Auto Ontology 交换格式写出）", "model_label": "未使用模型", "chat_model": None, "embed_model": None,
                         "locality": "local", "source_label": "Splink fake_1000 · 人员主数据", "sample": True, "reviewed": True}
        db.commit()
    for item in imported["items"]:
        unwrap(client.post(f"{root}/{set_id}/items/{item['id']}:review", headers=headers, json={"decision": "accepted"}))
    return unwrap(client.post(f"{root}/{set_id}:publish", headers=headers, json={}))


def add_constraints(ontology_id: str) -> str:
    """The data steward's rule: an e-mail address looks like one. The platform has no editor for property constraints yet."""
    with SessionLocal() as db:
        entity = ontology_metadata.type_entities(db, ontology_id)["Person"]
        definitions = [dict(d) for d in (entity.properties or {}).get("property_definitions", [])]
        for definition in definitions:
            if definition.get("id") == "email":
                definition["pattern"] = EMAIL_PATTERN
        entity.properties = {**(entity.properties or {}), "property_definitions": definitions}
        db.commit()
        return entity.id


def clean(value):
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA:
        return None
    return value.item() if hasattr(value, "item") else value


def install_objects(ontology_id: str, entity_id: str, frame: pd.DataFrame) -> int:
    graph = FalkorDBService()
    if not graph.available:
        raise RuntimeError("FalkorDB is unavailable")
    nodes = []
    with SessionLocal() as db:
        for record in frame.to_dict("records"):
            row = {key: clean(value) for key, value in record.items() if clean(value) is not None}
            source_id = str(row["unique_id"])
            object_id = f"{ontology_id[:8]}:Person:{source_id}"
            db.add(EntityInstance(id=object_id, entity_id=entity_id, ontology_id=ontology_id, row_identity=source_id, row_data=row))
            nodes.append({"id": object_id, "entity_type": "Person", "properties": row})
        db.commit()
    graph.upsert_instances(ontology_id, nodes)
    return len(nodes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default=os.environ.get("FIRST_ADMIN_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("FIRST_ADMIN_PASSWORD", ""))
    parser.add_argument("--force-new", action="store_true")
    args = parser.parse_args()
    if not args.password:
        raise SystemExit("Set FIRST_ADMIN_PASSWORD or pass --password")
    frame = pd.read_csv(DATA)
    with SessionLocal() as db:
        existing = db.query(OntologyProject).filter_by(name=NAME).first()
        if existing and not args.force_new:
            print(json.dumps({"ontology_id": existing.id, "reused": True}, ensure_ascii=False))
            return
    with httpx.Client(base_url=args.api, timeout=300) as client:
        headers = login(client, args.username, args.password)
        ontology = unwrap(client.post("/api/v1/ontologies", headers=headers, json={
            "name": NAME if not args.force_new else f"{NAME} · {uuid.uuid4().hex[:6]}", "domain": "科技",
            "description": "Splink 的 fake_1000：1000 条人员记录，约 250 个人，用来演示实体消歧、数据健康和数据血缘"}), 201)
        ontology_id = ontology["id"]
        published = publish_candidates(client, headers, ontology_id, interchange(frame))
    entity_id = add_constraints(ontology_id)
    objects = install_objects(ontology_id, entity_id, frame)
    print(json.dumps({"ontology_id": ontology_id, "objects": objects, "revision": (published.get("revision") or {}).get("id"), "reused": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
