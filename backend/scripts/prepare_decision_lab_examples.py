"""Prepare bounded, reproducible benchmark examples for the decision lab.

Run inside the backend container after the existing FactoryNet and I-BADAS
installers have completed. Only the selected records are written to disk.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from app.database import SessionLocal
from app.models.v2.dataset import Dataset, DatasetVersion
from app.services.storage_service import get_storage_service
from app.services.v2.datasets.factorynet_installer import FACTORYNET_SHA256, FACTORYNET_SOURCE_ID, FACTORYNET_URL
from app.services.v2.datasets.ibadas_installer import IBADAS_REPO, IBADAS_REVISION, IBADAS_SOURCE_ID


ROOT = Path(__file__).resolve().parents[1] / "data" / "decision_lab"
AI4I_URL = "https://archive.ics.uci.edu/static/public/601/ai4i+2020+predictive+maintenance+dataset.zip"
AI4I_ARCHIVE_SHA256 = "f601f14294bcf190f9d720676b7f0aea46a26cde9ab8ebc7b4f8174d9d26b252"
AI4I_CSV_SHA256 = "dc6630cd9b1f0f853922fad78a1b6436570d3f1ec863f1dd5c4340ac56bc8a8e"
FAILURE_MODES = ("TWF", "HDF", "PWF", "OSF", "RNF")
EPISODES = ("UMich_CNC_01", "UMich_CNC_06", "UMich_CNC_08", "UMich_CNC_13")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(name: str, value: object) -> str:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    (ROOT / name).write_bytes(raw)
    return _sha(raw)


def prepare_ai4i() -> tuple[list[dict], dict]:
    request = urllib.request.Request(AI4I_URL, headers={"User-Agent": "Project4 decision-lab sampler"})
    with urllib.request.urlopen(request, timeout=30) as response:
        archive = response.read(1_000_001)
    if len(archive) > 1_000_000 or _sha(archive) != AI4I_ARCHIVE_SHA256:
        raise ValueError("AI4I archive size or checksum changed")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        csv_bytes = bundle.read("ai4i2020.csv")
    if _sha(csv_bytes) != AI4I_CSV_SHA256:
        raise ValueError("AI4I CSV checksum changed")
    all_rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    selected: dict[str, dict] = {}
    for mode in FAILURE_MODES:
        candidates = [row for row in all_rows if row[mode] == "1"]
        for index in (0, len(candidates) // 2, len(candidates) - 1):
            selected[candidates[index]["UDI"]] = candidates[index]
    for product_type in ("L", "M", "H"):
        candidates = [row for row in all_rows if row["Type"] == product_type and row["Machine failure"] == "0"]
        candidates.sort(key=lambda row: (int(row["Tool wear [min]"]), int(row["UDI"])))
        for index in (0, len(candidates) // 4, len(candidates) // 2, 3 * len(candidates) // 4, len(candidates) - 1):
            selected[candidates[index]["UDI"]] = candidates[index]
    rows = []
    for row in sorted(selected.values(), key=lambda item: int(item["UDI"])):
        rows.append({
            "source_row_id": row["UDI"], "product_id": row["Product ID"], "product_type": row["Type"],
            "air_temp_k": float(row["Air temperature [K]"]), "process_temp_k": float(row["Process temperature [K]"]),
            "speed_rpm": int(row["Rotational speed [rpm]"]), "torque_nm": float(row["Torque [Nm]"]),
            "tool_wear_min": int(row["Tool wear [min]"]), "machine_failure": int(row["Machine failure"]),
            "failure_modes": [mode for mode in FAILURE_MODES if row[mode] == "1"],
        })
    return rows, {"url": "https://archive.ics.uci.edu/dataset/601/ai4i%2B2020%2B", "download_url": AI4I_URL,
                  "archive_sha256": AI4I_ARCHIVE_SHA256, "source_sha256": AI4I_CSV_SHA256,
                  "license": "CC BY 4.0", "source_rows": len(all_rows), "selected_rows": len(rows),
                  "selection": "每种故障模式按原始顺序取首、中、末 3 条；每种产品类型取 5 个无故障磨损分位点；重叠行去重。",
                  "limitation": "有意分层的演示切片；故障率和规则覆盖率不能外推到总体。"}


def _source_dataset(db, source_id: str) -> tuple[Dataset, DatasetVersion]:
    dataset = next((row for row in db.query(Dataset).all() if (row.schema_json or {}).get("source_id") == source_id), None)
    if not dataset or not dataset.latest_version_id:
        raise RuntimeError(f"Install {source_id} before preparing decision-lab examples")
    version = db.get(DatasetVersion, dataset.latest_version_id)
    if not version or not version.storage_uri:
        raise RuntimeError(f"Missing source version for {source_id}")
    return dataset, version


def prepare_factorynet(db) -> tuple[list[dict], dict]:
    dataset, version = _source_dataset(db, FACTORYNET_SOURCE_ID)
    raw = get_storage_service().get_object(version.storage_uri)
    if _sha(raw) != FACTORYNET_SHA256:
        raise ValueError("FactoryNet source checksum changed")
    columns = ["episode_id", "time_s", "setpoint_vel_0", "feedback_vel_0", "ctx_tool_condition", "ctx_passed_visual_inspection"]
    table = pq.read_table(io.BytesIO(raw), columns=columns)
    by_episode: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for index, row in enumerate(table.to_pylist()):
        if row["episode_id"] in EPISODES:
            by_episode[row["episode_id"]].append((index, row))
    rows = []
    for episode in EPISODES:
        candidates = by_episode[episode]
        if len(candidates) < 64:
            raise ValueError(f"FactoryNet episode {episode} is too short")
        for offset in sorted({round(i * (len(candidates) - 1) / 63) for i in range(64)}):
            source_index, row = candidates[offset]
            rows.append({"source_row_id": str(source_index), "episode_id": episode,
                         "time_s": round(float(row["time_s"]), 4),
                         "setpoint_vel_0": float(row["setpoint_vel_0"] or 0),
                         "feedback_vel_0": float(row["feedback_vel_0"] or 0),
                         "tool_condition": row["ctx_tool_condition"],
                         "visual_inspection": row["ctx_passed_visual_inspection"]})
    return rows, {"url": "https://huggingface.co/datasets/Forgis/FactoryNet", "download_url": FACTORYNET_URL,
                  "original_source_url": "https://www.kaggle.com/datasets/shasun/tool-wear-detection-in-cnc-mill",
                  "source_sha256": FACTORYNET_SHA256, "license": "UMich CNC 原始来源条款（待核验）",
                  "source_id": FACTORYNET_SOURCE_ID, "source_rows": table.num_rows,
                  "selected_rows": len(rows), "episodes": list(EPISODES),
                  "selection": "从 4 个指定 CNC 加工批次各等距抽取 64 个原始采样点。",
                  "limitation": "下采样点不是连续事件窗口；目检结果仅供回看，不是模型预测。"}


def prepare_ibadas(db) -> tuple[list[dict], dict]:
    dataset, version = _source_dataset(db, IBADAS_SOURCE_ID)
    source_samples = (dataset.schema_json or {}).get("samples") or []
    if len(source_samples) != 12:
        raise ValueError("Expected the installed twelve-group I-BADAS subset")
    rows = []
    for sample in source_samples:
        assets = [{key: asset[key] for key in ("role", "source_path", "size", "sha256")}
                  for asset in sample["assets"]]
        rows.append({"sample_key": sample["sample_id"], "scene_id": sample["scene_id"],
                     "label": sample["quality"], "assets": assets})
    return rows, {"url": f"https://huggingface.co/datasets/{IBADAS_REPO}", "revision": IBADAS_REVISION,
                  "source_sha256": version.checksum, "license": "CC BY-NC 4.0",
                  "source_id": IBADAS_SOURCE_ID,
                  "selected_samples": len(rows), "selection": "复用已固定下载的 6 组正常与 6 组异常 RGB-D 样本；媒体仍在对象存储。",
                  "limitation": "官方标签只用于回看覆盖情况，不作为在线预测。"}


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        ai4i, ai4i_source = prepare_ai4i()
        factorynet, factory_source = prepare_factorynet(db)
        ibadas, ibadas_source = prepare_ibadas(db)
    hashes = {"ai4i_examples.json": _write("ai4i_examples.json", ai4i),
              "factorynet_examples.json": _write("factorynet_examples.json", factorynet),
              "ibadas_examples.json": _write("ibadas_examples.json", ibadas)}
    _write("manifest.json", {"schema_version": 1, "sources": {"ai4i": ai4i_source, "factorynet": factory_source,
                                                          "ibadas": ibadas_source}, "file_sha256": hashes})
    print(json.dumps({"files": hashes, "rows": {"ai4i": len(ai4i), "factorynet": len(factorynet), "ibadas": len(ibadas)}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
