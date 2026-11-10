"""The prepared bundles on the data-source page: what each is, and how to load it. Loading runs only when the bundle is imported."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable

from app.services.v2.data_import.model import Bundle, Source


@dataclass
class Entry:
    id: str
    module: str
    title: str
    summary: str
    domain: str
    kind: str
    tags: list[str]
    sources: list[Source]
    shows: list[str]
    replay: bool = False

    def load(self) -> Bundle:
        return importlib.import_module(f"app.services.v2.data_import.bundles.{self.module}").load()


ENTRIES: list[Entry] = [
    Entry("demo-tpch", "tpch", "供应链订单履约（TPC-H）", "批发供应商的订单履约：客户、订单、订单行、零件、供应商、国家、地区八张表，加四张按供应商、客户、订单、零件汇总的计算表。", "供应链", "relational",
          ["关系型", "多表", "计算表", "血缘较深"],
          [Source(id="tpch", title="TPC-H 基准（DuckDB 生成）", kind="generated", license="TPC 基准规范", url="https://duckdb.org/docs/stable/core_extensions/tpch.html")],
          ["12 个对象类型、13 个关系", "准时签收率、供应覆盖倍数两条业务目标，没达到的对象可以点开", "血缘：数据源 → 读取 → 8 张表 → 4 个计算 → 本体 → 检查和消歧"]),
    Entry("demo-nyc-taxi", "nyc_taxi", "出租车行程事件流（纽约 TLC）", "2019 年 1 月纽约黄色出租车的 1 万次真实行程，按上车时间逐批载入；数据里有乘客数为 0、车费为负这类真实的脏数据。", "其他", "events",
          ["动态数据", "事件流", "真实数据", "脏数据"],
          [Source(id="tlc-trips", title="yellow_tripdata_sample_2019-01.csv", kind="file", license="NYC TLC 公开数据", url="https://github.com/great-expectations/gx_tutorials")],
          ["行程、区域、服务商、费率、支付方式、区域活跃度 6 个类型", "先载入前 400 条，点“推进”再来 400 条，对象和关系随之增长", "数据健康会指出乘客数、车费等不合 TLC 数据字典的行程"], replay=True),
    Entry("demo-brick-site-b", "brick", "楼宇设备与传感时序（Brick · BTS Site B）", "一栋办公楼的暖通和电气系统：空间、设备、851 个测点按 Brick 本体连在一起，其中 49 个测点有一年的传感器读数。", "制造", "timeseries",
          ["时序数据", "Brick 本体", "真实数据", "关系复杂"],
          [Source(id="brick-ttl", title="Site_B.ttl（Brick 元数据）", kind="rdf", license="BTS 数据集（Cruise Research Group）", url="https://github.com/cruiseresearchgroup/DIEF_BTS"),
           Source(id="bts-observations", title="observations.csv（49 条数据流）", kind="file", license="BTS 数据集（Cruise Research Group）", url="https://github.com/cruiseresearchgroup/DIEF_BTS")],
          ["空间、设备、测点、观测、测点统计、设备概况 6 个类型和 6 种多对多关系", "RDF 元数据 → 三元组 → SQL → 类型：血缘有 4 层", "测点覆盖率：851 个测点里只有 49 个有读数"]),
    Entry("demo-abt-buy", "abt_buy", "商品主数据合并（Abt-Buy）", "两家网店各有一份商品目录，同一件商品常在两边各有一条、写法不同。合成一个商品类型，带着基准数据集给出的已知同款分组，实体消歧可以对着它打分。", "供应链", "multisource",
          ["多来源合并", "实体消歧", "真实数据", "有标准答案"],
          [Source(id="abt-buy", title="Abt-Buy.zip", kind="file", license="知识共享（莱比锡大学）", url="https://dbs.uni-leipzig.de/research/projects/benchmark-datasets-for-entity-resolution")],
          ["Abt 1081 件 + Buy 1092 件商品合成 2173 个商品，其中 1097 对是已知同款", "商品、品牌、网店目录三个类型，加“已知同款”的多对多关系", "实体消歧可以对着标准答案打分；血缘有两个来源汇合"]),
    Entry("demo-beans", "beans", "豆叶病害照片（图像 + 标签）", "60 张真实的豆叶照片（角斑病、锈病、健康各 20 张）和标签表一起导入：照片是文件资产，标签按文件名连到照片，再按类别汇总。", "其他", "multimodal",
          ["多模态", "图片", "真实数据", "非工业"],
          [Source(id="beans-images", title="60 张豆叶照片（Hugging Face AI-Lab-Makerere/beans）", kind="file", license="MIT", url="https://huggingface.co/datasets/AI-Lab-Makerere/beans")],
          ["照片文件、叶片观测、病害类别、病害统计 4 个类型", "每张照片都能在“照片文件”对象的预览地址里打开", "证明导入不限工业数据：任何类型的文件都可以"]),
]


def get(bundle_id: str) -> Entry | None:
    return next((e for e in ENTRIES if e.id == bundle_id), None)
