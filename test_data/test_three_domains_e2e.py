#!/usr/bin/env python3
"""三业务域全链路脚本：私有 Dataset Construction 与 Pipeline Mapping。

Dataset Construction 使用每个领域的一个结构化 CSV/XLSX fixture 和确定性映射，
不调用 LLM，也不代表领域语义推理能力。
"""
import re, sys, time
from pathlib import Path
import requests
from construction_api_flow import build_private_structured_dataset, first_structured_file, login_reviewer_session, login_session

API = "http://localhost:8000"
TD  = Path(__file__).parent
DOMAINS = ["供应链", "财务", "医疗"]

s = login_session(API)
reviewer = login_reviewer_session(API)
print("✅ 作者与独立审核人登录成功\n")


# ── 公共工具 ──────────────────────────────────────────────────────────────────

def pascal(fname: str) -> str:
    base = fname.rsplit(".", 1)[0]
    parts = re.split(r"[_\-\s]+", base)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)[:40] or "Entity"


def snapshot(oid: str) -> dict:
    ents = s.get(f"{API}/api/v1/ontologies/{oid}/entities", params={"page_size": 500}).json()
    d = ents.get("data", ents)
    items = d.get("items", d) if isinstance(d, dict) else d
    graph = s.get(f"{API}/api/v2/ontologies/{oid}/graph", params={"limit": 1000}).json()
    graph = graph.get("data", graph)
    logic = s.get(f"{API}/api/v1/ontologies/{oid}/logic").json().get("data", [])
    acts  = s.get(f"{API}/api/v1/ontologies/{oid}/actions").json().get("data", [])
    by_type: dict = {}
    for e in items:
        t = e.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    return {
        "entities":  len(items),
        "edges":     len(graph.get("edges", [])),
        "logic":     len(logic),
        "actions":   len(acts),
        "by_type":   by_type,
    }


# ── Dataset Construction 路径 ─────────────────────────────────────────────────

def run_construction(domain: str) -> dict:
    source = first_structured_file(TD / domain)
    return build_private_structured_dataset(
        s,
        API,
        source,
        reviewer_session=reviewer,
        domain=domain,
        ontology_name=f"{domain}-DatasetConstruction-{int(time.time())}",
    )


# ── Pipeline Mapping 路径 ─────────────────────────────────────────────────────

def run_mapping(domain: str) -> dict:
    folder = TD / domain
    conn_files = []
    for f in sorted(folder.glob("*")):
        with open(f, "rb") as fh:
            j = s.post(f"{API}/api/v2/datasets/upload", files={"file": (f.name, fh)}).json()
        d = j.get("data") or j
        conn_files.append({"name": f.name, "dataset_id": d["id"], "kind": d.get("kind")})

    definition = {
        "nodes": [
            {"id": "conn", "type": "connector", "position": {"x": 0,   "y": 0}, "label": "连接器",
             "config": {"source_type": "file", "files": conn_files}},
            {"id": "stor", "type": "storage",   "position": {"x": 260, "y": 0}, "label": "存储器",
             "config": {"storage_mode": "auto"}},
            {"id": "tran", "type": "transform", "position": {"x": 520, "y": 0}, "label": "转换器",
             "config": {"path": "auto", "steps": []}},
            {"id": "out",  "type": "output",    "position": {"x": 780, "y": 0}, "label": "输出",
             "config": {"dataset_type": "curated_dataset", "primary_key": []}},
        ],
        "edges": [{"id": "e1", "source": "conn", "target": "stor"},
                  {"id": "e2", "source": "stor", "target": "tran"},
                  {"id": "e3", "source": "tran", "target": "out"}],
    }
    resp = s.post(f"{API}/api/v2/pipelines", json={
        "name": f"{domain}-管道-{int(time.time())}", "definition": definition,
    })
    pid = (resp.json().get("data") or resp.json())["id"]

    res     = s.post(f"{API}/api/v2/pipelines/{pid}/run-sync").json()
    outputs = ((res.get("stats") or {}).get("meta") or {}).get("outputs", [])

    r = s.post(f"{API}/api/v1/ontologies", json={
        "name": f"{domain}-Mapping-{int(time.time())}",
        "domain": domain, "build_mode": "pipeline_mapping",
    })
    oid = r.json()["data"]["id"]
    for o in outputs:
        cid = o["curated_dataset_id"]
        s.post(f"{API}/api/v2/curated/{cid}/review", params={"action": "approve"})
        s.post(f"{API}/api/v2/ontologies/{oid}/mappings", json={
            "curated_dataset_id": cid,
            "entity_class": pascal(o["source_file"]),
            "field_mapping": {},
        })

    rb = s.post(f"{API}/api/v2/ontologies/{oid}/mappings/build-all")
    if not rb.ok:
        return {"domain": domain, "oid": oid, "status": "build_failed",
                "error": rb.text[:100], "files": len(conn_files), "curated": len(outputs)}

    snap = snapshot(oid)
    routes = {o["source_file"]: o.get("route", "?") for o in outputs}
    return {"domain": domain, "oid": oid, "status": "success",
            "files": len(conn_files), "curated": len(outputs),
            "routes": routes, **snap}


# ── 主流程 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    WIDTH = 60
    construction_results = []
    mapping_results = []

    # ── 私有 Dataset Construction ──
    print("=" * WIDTH)
    print("  Dataset Construction（确定性结构映射）")
    print("=" * WIDTH)
    for domain in DOMAINS:
        print(f"  [{domain}] 构筑中...", end=" ", flush=True)
        r = run_construction(domain)
        construction_results.append(r)
        ok = r.get("status") == "completed" and r.get("entities", 0) > 0
        flag = "✅" if ok else "❌"
        print(f"{flag} 文件={r['file']} 状态={r['status']}  "
              f"实体={r.get('entities',0)}  边={r.get('edges',0)}  "
              f"逻辑={r.get('logic',0)}  动作={r.get('actions',0)}")
        print(f"      已写实例={r.get('instances', 0)} · 构筑方式={r.get('construction_method')}")

    # ── Pipeline Mapping ──
    print()
    print("=" * WIDTH)
    print("  Pipeline Mapping 路径")
    print("=" * WIDTH)
    for domain in DOMAINS:
        print(f"  [{domain}] 运行中...", end=" ", flush=True)
        r = run_mapping(domain)
        mapping_results.append(r)
        ok = r.get("status") == "success" and r.get("entities", 0) > 0
        flag = "✅" if ok else "❌"
        print(f"{flag} curated={r.get('curated',0)}  "
              f"实体={r.get('entities',0)}  边={r.get('edges',0)}  "
              f"逻辑={r.get('logic',0)}  动作={r.get('actions',0)}")
        if r.get("routes"):
            for fname, route in r["routes"].items():
                print(f"      {fname:<35} route={route}")
        if r.get("by_type"):
            print("    实体类型分布:")
            for t, cnt in sorted(r["by_type"].items(), key=lambda x: -x[1])[:6]:
                print(f"      {t}: {cnt}")

    # ── 汇总 ──
    print()
    print("=" * WIDTH)
    print("  汇总")
    print("=" * WIDTH)
    print(f"  {'域':<8} {'Dataset Construction':^28} {'Pipeline Mapping':^28}")
    print(f"  {'':8} {'实体':>6} {'边':>5} {'逻辑':>5} {'动作':>5}  "
          f"{'实体':>6} {'边':>5} {'逻辑':>5} {'动作':>5}")
    all_ok = True
    for s_r, m_r in zip(construction_results, mapping_results):
        domain = s_r["domain"]
        s_ok = s_r.get("status") == "completed" and s_r.get("entities",0) > 0
        m_ok = m_r.get("status") == "success" and m_r.get("entities",0) > 0
        flag = "✅" if (s_ok and m_ok) else "❌"
        all_ok = all_ok and s_ok and m_ok
        print(f"  {flag} {domain:<6} "
              f"{s_r.get('entities',0):>6} {s_r.get('edges',0):>5} "
              f"{s_r.get('logic',0):>5} {s_r.get('actions',0):>5}  "
              f"{m_r.get('entities',0):>6} {m_r.get('edges',0):>5} "
              f"{m_r.get('logic',0):>5} {m_r.get('actions',0):>5}")

    print()
    print(f"  最终结果: {'✅ 全部通过' if all_ok else '❌ 存在失败'}")
    sys.exit(0 if all_ok else 1)
