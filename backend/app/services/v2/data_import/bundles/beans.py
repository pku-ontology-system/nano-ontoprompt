"""Photographs of bean leaves with their disease labels: images and a table about them, imported together.

Sixty photographs (twenty of each class) from the Hugging Face dataset AI-Lab-Makerere/beans, which is a field survey of bean plants in Uganda.
Each photograph becomes a file asset whose bytes are kept in the platform's storage; the labels table describes it and is linked to it by file name.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from app.services.v2.data_import import duck, uploads
from app.services.v2.data_import.model import Bundle, Derived, Link, Source, Table

DIR = Path(__file__).resolve().parents[5] / "examples" / "data_import" / "beans"
DISEASES = [("angular_leaf_spot", "角斑病", "叶片上出现被叶脉限制的多角形褐斑，由细菌或真菌引起"), ("bean_rust", "锈病", "叶片背面出现红褐色粉状孢子堆，由锈菌引起"), ("healthy", "健康", "没有病斑的健康叶片")]
STATS_SQL = """SELECT o.label AS disease, COUNT(*) AS image_count, ROUND(AVG(o.width), 1) AS avg_width, ROUND(AVG(o.height), 1) AS avg_height, MIN(o.source_row) AS first_source_row
FROM leaf_observations o GROUP BY o.label"""
JOIN_SQL = """SELECT l.file_name, l.label, l.split, l.source_row, l.width, l.height FROM labels_raw l"""


def load() -> Bundle:
    labels = pd.read_csv(DIR / "labels.csv")
    manifest = json.loads((DIR / "manifest.json").read_text(encoding="utf-8"))
    files = [(name, (DIR / name).read_bytes()) for name in labels["file_name"]]
    uris = uploads.store("demo-beans", files)
    assets = pd.DataFrame([{**uploads.describe_asset(name, content), "storage_uri": uris[name]} for name, content in files])
    con = duck.connect(labels_raw=labels)
    observations = con.execute(JOIN_SQL).df()
    duck.register(con, "leaf_observations", observations)
    stats = con.execute(STATS_SQL).df()
    diseases = pd.DataFrame([{"disease": key, "name": cn, "description": text} for key, cn, text in DISEASES])
    sha = hashlib.sha256((DIR / "labels.csv").read_bytes()).hexdigest()
    sources = [Source(id="beans-images", title="60 张豆叶照片（JPEG）", kind="file", location="backend/examples/data_import/beans/", license="MIT", url=manifest["url"], bytes=manifest["bytes"],
                      note=manifest["selection"]), Source(id="beans-labels", title="labels.csv（类别、尺寸、在原数据集里的行号）", kind="file", location="backend/examples/data_import/beans/labels.csv", license="MIT",
                                                         url=manifest["url"], sha256=sha)]
    tables = [
        Table(name="labels_raw", frame=labels, type_name="LabelsRaw", label="标签表（原始）", description="每张照片的类别标签", source="beans-labels", kind="stage"),
        Table(name="media_assets", frame=assets, type_name=uploads.ASSET_TYPE, label="照片文件", description="60 张照片文件：文件名、尺寸、字节数、SHA-256 和存放位置；点预览地址能看到照片", source="beans-images",
              primary_key=["asset_id"], kind="asset", column_labels={"asset_id": "资产编号", "file_name": "文件名", "path": "路径", "media_kind": "类别", "mime_type": "媒体类型", "size_bytes": "字节数",
                                                                "sha256": "SHA-256", "width": "宽", "height": "高", "storage_uri": "存放位置", "preview_url": "预览地址"}),
        Table(name="leaf_observations", frame=observations, type_name="LeafObservation", label="叶片观测", description="一张照片对应的一次叶片观测：病害类别、来源数据集里的行号、照片尺寸",
              primary_key=["file_name"], column_labels={"file_name": "照片文件名", "label": "病害类别", "split": "数据集划分", "source_row": "原数据集行号", "width": "宽", "height": "高"},
              derived=Derived(sql=JOIN_SQL, inputs=["labels_raw"], description="取出每张照片的标签和尺寸", columns={c: [f"labels_raw.{c}"] for c in ("file_name", "label", "split", "source_row", "width", "height")})),
        Table(name="diseases", frame=diseases, type_name="Disease", label="病害类别", description="数据集里的三个类别：角斑病、锈病、健康", source=None, primary_key=["disease"],
              column_labels={"disease": "类别", "name": "名称", "description": "说明"}),
        Table(name="disease_stats", frame=stats, type_name="DiseaseStats", label="病害统计", description="每个类别的照片数、平均尺寸和最早的原数据集行号", primary_key=["disease"],
              column_labels={"disease": "类别", "image_count": "照片数", "avg_width": "平均宽", "avg_height": "平均高", "first_source_row": "最早行号"},
              derived=Derived(sql=STATS_SQL, inputs=["leaf_observations"], description="按类别汇总观测", columns={"disease": ["leaf_observations.label"], "image_count": ["leaf_observations.file_name"],
                                                                                                     "avg_width": ["leaf_observations.width"], "avg_height": ["leaf_observations.height"],
                                                                                                     "first_source_row": ["leaf_observations.source_row"]})),
    ]
    links = [Link("leaf_observations_file_name", "leaf_observations", "file_name", "media_assets", "file_name", "declared_fk", "观测→照片文件"), Link("leaf_observations_label", "leaf_observations", "label", "diseases", "disease", "declared_fk", "观测→病害类别"),
             Link("disease_stats_disease", "disease_stats", "disease", "diseases", "disease", "declared_fk", "统计→病害类别")]
    return Bundle(id="demo-beans", title="豆叶病害照片（图像 + 标签）", domain="其他", kind="multimodal", ontology_name="豆叶病害照片（图像 + 标签）",
                  summary="60 张真实的豆叶照片（角斑病、锈病、健康各 20 张），照片文件和它们的标签表一起导入：照片是文件资产，标签表按文件名和它们连在一起，再按类别汇总。不限工业数据，任何类型的文件都能这样导入。",
                  sources=sources, tables=tables, links=links, tags=["多模态", "图片", "真实数据", "非工业"],
                  notes=["照片取自 Hugging Face 的 AI-Lab-Makerere/beans 训练集，每个类别均匀取 20 张；字节存在平台的对象存储里。"])
