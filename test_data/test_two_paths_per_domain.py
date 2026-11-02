#!/usr/bin/env python3
"""两条路径 × 三业务域的本地演示脚本

修正点: Pipeline Mapping 每个业务域只建 **一个** pipeline, 其 connector 节点
以「文件上传」方式挂载该域文件夹的全部文件 (config.files[]), 后端按每个文件
的 kind 自动选 A/B/C 路由, 产出多个 curated dataset。

Dataset Construction: 每域选一个 CSV/TSV/XLSX 样本，经私有 Dataset 和确定性映射构筑。
该路径不调用 LLM，不代表领域语义推理能力。

测试登录从 PROJECT4_E2E_USERNAME / PROJECT4_E2E_PASSWORD 读取。
"""
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests
from construction_api_flow import build_private_structured_dataset, first_structured_file, login_reviewer_session, login_session

API = "http://localhost:8000"
TD = Path(__file__).parent
DOMAINS = ["供应链", "财务", "医疗"]
# Pipeline 的历史演示仍读取域目录中的所有支持来源；Dataset Construction
# 单独选择一份结构化样本，不将文档或多模态来源误标成已支持路径。

s = login_session(API)
reviewer = login_reviewer_session(API)
print("✅ 作者与独立审核人登录")


def pascal(fname: str) -> str:
    base = fname.rsplit(".", 1)[0]
    parts = re.split(r"[_\-\s]+", base)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)[:40] or "Entity"


def clean_ontologies():
    if os.environ.get("PROJECT4_E2E_ALLOW_DESTRUCTIVE_CLEANUP") != "1":
        print("🛡️ 跳过全库本体删除；如确需清理，请显式设置 PROJECT4_E2E_ALLOW_DESTRUCTIVE_CLEANUP=1")
        return
    ontos = s.get(f"{API}/api/v1/ontologies").json().get("data", [])
    if isinstance(ontos, dict):
        ontos = ontos.get("items", [])
    for o in ontos:
        s.delete(f"{API}/api/v1/ontologies/{o['id']}")
    print(f"🧹 清理 {len(ontos)} 个旧本体")


# ─────────────────────────── Pipeline Mapping ───────────────────────────
def run_mapping(domain: str) -> dict:
    folder = TD / domain
    files = sorted(folder.glob("*"))
    # 1) 上传该域全部文件, 收集 connector.files
    conn_files = []
    for f in files:
        with open(f, "rb") as fh:
            r = s.post(f"{API}/api/v2/datasets/upload", files={"file": (f.name, fh)})
        j = r.json().get("data") or r.json()
        conn_files.append({"name": f.name, "dataset_id": j["id"], "kind": j.get("kind")})

    # 2) 单个 pipeline, connector 挂全部文件 (贴合前端 definition 结构)
    definition = {
        "nodes": [
            {"id": "conn", "type": "connector", "position": {"x": 0, "y": 0}, "label": "连接器",
             "config": {"source_type": "file", "files": conn_files}},
            {"id": "stor", "type": "storage", "position": {"x": 260, "y": 0}, "label": "存储器",
             "config": {"storage_mode": "auto"}},
            {"id": "tran", "type": "transform", "position": {"x": 520, "y": 0}, "label": "转换器",
             "config": {"path": "auto", "steps": []}},
            {"id": "out", "type": "output", "position": {"x": 780, "y": 0}, "label": "输出",
             "config": {"dataset_type": "curated_dataset", "primary_key": []}},
        ],
        "edges": [{"id": "e1", "source": "conn", "target": "stor"},
                  {"id": "e2", "source": "stor", "target": "tran"},
                  {"id": "e3", "source": "tran", "target": "out"}],
    }
    r = s.post(f"{API}/api/v2/pipelines", json={
        "name": f"{domain}-管道-{int(time.time())}", "definition": definition})
    pid = (r.json().get("data") or r.json())["id"]
    print(f"\n══ {domain} Mapping: 1 个 pipeline, connector 挂 {len(conn_files)} 个文件 ══")

    res = s.post(f"{API}/api/v2/pipelines/{pid}/run-sync").json()
    outputs = ((res.get("stats") or {}).get("meta") or {}).get("outputs", [])
    print(f"  run-sync: {res.get('status')}, 产出 {len(outputs)} 个 curated dataset")
    for o in outputs:
        print(f"    {o.get('source_file'):30s} route={o.get('route')} rows={o.get('rows_out')}")

    # 3) 审批 + 建映射 (pk 交给后端自动推断)
    r = s.post(f"{API}/api/v1/ontologies", json={
        "name": f"{domain}-Mapping-{int(time.time())}", "domain": domain,
        "build_mode": "pipeline_mapping", "description": "单管道多文件 connector 测试"})
    oid = r.json()["data"]["id"]
    for o in outputs:
        cid = o["curated_dataset_id"]
        s.post(f"{API}/api/v2/curated/{cid}/review", params={"action": "approve"})
        s.post(f"{API}/api/v2/ontologies/{oid}/mappings", json={
            "curated_dataset_id": cid, "entity_class": pascal(o["source_file"]),
            "field_mapping": {}})
    rb = s.post(f"{API}/api/v2/ontologies/{oid}/mappings/build-all")
    res2 = rb.json()
    g = s.get(f"{API}/api/v2/ontologies/{oid}/graph?limit=1000").json()
    g = g.get("data", g)
    lbl = {n["id"]: (n.get("labels") or ["?"])[0] for n in g["nodes"]}
    pairs = Counter((lbl.get(e["source"], "?"), e["type"], lbl.get(e["target"], "?")) for e in g["edges"])
    print(f"  build-all: {rb.status_code} 实体={res2.get('total_entities')} 关系={res2.get('total_relations')} "
          f"逻辑={res2.get('total_logic')} 动作={res2.get('total_actions')}")
    print(f"  图谱: {len(g['nodes'])} 节点 {len(g['edges'])} 边")
    for k, v in pairs.most_common():
        print(f"    {k[0]} -[{k[1]}]-> {k[2]} × {v}")
    return {"domain": domain, "oid": oid, "pipeline_id": pid, "files": len(conn_files),
            "curated": len(outputs), "nodes": len(g["nodes"]), "edges": len(g["edges"]),
            "logic": res2.get("total_logic"), "actions": res2.get("total_actions")}


# ─────────────────────────── Dataset Construction ───────────────────────────
def run_construction(domain: str) -> dict:
    source = first_structured_file(TD / domain)
    print(f"\n══ {domain} Dataset Construction: {source.name} ══")
    return build_private_structured_dataset(
        s,
        API,
        source,
        reviewer_session=reviewer,
        domain=domain,
        ontology_name=f"{domain}-DatasetConstruction-{int(time.time())}",
    )


if __name__ == "__main__":
    clean_ontologies()
    mapping_res = [run_mapping(d) for d in DOMAINS]
    construction_res = [run_construction(d) for d in DOMAINS]

    print("\n" + "=" * 60)
    print("Pipeline Mapping 汇总 (每域 1 个 pipeline)")
    for r in mapping_res:
        print(f"  ✅ {r['domain']}: {r['files']}文件→1管道→{r['curated']}curated | "
              f"{r['nodes']}节点 {r['edges']}边 logic={r['logic']} actions={r['actions']} | {r['oid']}")
    print("私有 Dataset Construction 汇总（确定性结构映射）")
    for r in construction_res:
        print(f"  {'✅' if r['status'] == 'completed' and r['entities'] > 0 else '❌'} "
              f"{r['domain']}: {r['file']} {r['status']} | "
              f"实体类型={r['entities']} 实例={r['instances']} 边={r['edges']} "
              f"logic={r['logic']} actions={r['actions']} | {r['ontology_id']}")
