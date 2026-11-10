"""A building and its sensors: the BTS Building TimeSeries dataset (Site B), whose metadata is written in the Brick Schema.

Brick is the open ontology for buildings: spaces, HVAC equipment, and the points (sensors, setpoints, commands) that belong to them, linked
by feeds / isPartOf / hasLocation. The RDF is read into a triples table, and typed tables are computed from it with SQL; the sensor
readings (49 of the 851 points have data, one year, spaced evenly) become observations linked to their point.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import rdflib
from rdflib.namespace import RDF

from app.services.v2.data_import import duck
from app.services.v2.data_import.model import Bundle, Derived, Link, Source, Table

DATA = Path(__file__).resolve().parents[5] / "data" / "bts_demo"
BRICK = rdflib.Namespace("https://brickschema.org/schema/Brick#")
SENAPS_STREAM = rdflib.URIRef("http://senaps.io/schema/1.0/senaps#stream_id")
PER_STREAM = 150
SPACES = {"HVAC_Zone", "Conference_Room", "Hallway", "Enclosed_space", "Restroom", "Open_space", "space_Kitchen", "Lobby", "Storage_Room", "Server_Room", "Floor", "Breakroom", "Building", "Copy_Room",
          "Janitor_Room", "space", "Shared_space", "Service_Room", "Room", "Library", "Basement"}
RELATIONS = ["isPointOf", "feeds", "isFedBy", "hasPart", "isPartOf", "hasLocation", "isLocationOf"]


def _domain(cls: str) -> str:
    if any(k in cls for k in ("Electrical", "PV_", "Solar", "Inverter", "Circuit", "Meter", "Generation")):
        return "电气"
    if any(k in cls for k in ("Water", "Boiler", "Chiller", "Pump", "Tank")):
        return "水系统"
    if any(k in cls for k in ("Fan", "Coil", "Valve", "Damper", "Air", "HVAC", "Filter", "Unit")):
        return "暖通"
    return "其他"


def _read_metadata(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Typed things (with a readable id each), the relations between them, and the stream each point reads."""
    g = rdflib.Graph()
    g.parse(str(path), format="turtle")
    points = set(g.subjects(BRICK.isPointOf, None))
    classes: dict[rdflib.term.Node, str] = {}
    for subject in set(g.subjects(RDF.type, None)):
        if not isinstance(subject, rdflib.URIRef):
            continue
        names = sorted(str(o).split("#")[-1] for o in g.objects(subject, RDF.type))
        classes[subject] = next((n for n in names if n != "Point"), names[0])
    counter: dict[str, int] = {}
    ids: dict[rdflib.term.Node, str] = {}
    for subject in sorted(classes, key=str):
        counter[classes[subject]] = counter.get(classes[subject], 0) + 1
        ids[subject] = f"{classes[subject]}-{counter[classes[subject]]:03d}"
    things = []
    for subject, cls in classes.items():
        category = "point" if subject in points else "space" if cls in SPACES else "equipment"
        things.append({"subject_id": ids[subject], "brick_class": cls, "category": category, "domain": "测点" if category == "point" else "空间" if category == "space" else _domain(cls)})
    relations, streams = [], []
    for name in RELATIONS:
        for s, o in g.subject_objects(BRICK[name]):
            if s in ids and o in ids:
                relations.append({"subject_id": ids[s], "predicate": name, "object_id": ids[o]})
    for s, o in g.subject_objects(SENAPS_STREAM):
        if s in ids:
            streams.append({"subject_id": ids[s], "stream_id": str(o)})
    return pd.DataFrame(things).sort_values("subject_id"), pd.DataFrame(relations), pd.DataFrame(streams)


def _observations(path: Path) -> pd.DataFrame:
    obs = pd.read_csv(path, usecols=["stream_id", "event_time", "value"])
    obs["event_time"] = pd.to_datetime(obs["event_time"])
    parts = []
    for _, group in obs.sort_values("event_time").groupby("stream_id"):
        if len(group) > PER_STREAM:
            group = group.iloc[[round(i * (len(group) - 1) / (PER_STREAM - 1)) for i in range(PER_STREAM)]]
        parts.append(group)
    return pd.concat(parts).reset_index(drop=True)


SQL = {
    "spaces": ("SELECT subject_id AS space_id, brick_class FROM brick_entities WHERE category = 'space'", ["brick_entities"], {"space_id": ["brick_entities.subject_id"], "brick_class": ["brick_entities.brick_class"]}),
    "equipment": ("SELECT subject_id AS equipment_id, brick_class, domain FROM brick_entities WHERE category = 'equipment'", ["brick_entities"],
                  {"equipment_id": ["brick_entities.subject_id"], "brick_class": ["brick_entities.brick_class"], "domain": ["brick_entities.domain"]}),
    "points": ("""SELECT e.subject_id AS point_id, e.brick_class, st.stream_id, COALESCE(st.stream_id IN (SELECT stream_id FROM observations_raw), FALSE) AS has_data
FROM brick_entities e LEFT JOIN brick_streams st ON st.subject_id = e.subject_id WHERE e.category = 'point'""", ["brick_entities", "brick_streams", "observations_raw"],
               {"point_id": ["brick_entities.subject_id"], "brick_class": ["brick_entities.brick_class"], "stream_id": ["brick_streams.stream_id"], "has_data": ["brick_streams.stream_id", "observations_raw.stream_id"]}),
    "equipment_points": ("""SELECT DISTINCT r.object_id AS equipment_id, r.subject_id AS point_id FROM brick_relations r JOIN brick_entities t ON t.subject_id = r.object_id AND t.category = 'equipment'
WHERE r.predicate = 'isPointOf'""", ["brick_relations", "brick_entities"], {"equipment_id": ["brick_relations.object_id"], "point_id": ["brick_relations.subject_id"]}),
    "space_points": ("""SELECT DISTINCT r.object_id AS space_id, r.subject_id AS point_id FROM brick_relations r JOIN brick_entities t ON t.subject_id = r.object_id AND t.category = 'space'
WHERE r.predicate = 'isPointOf'""", ["brick_relations", "brick_entities"], {"space_id": ["brick_relations.object_id"], "point_id": ["brick_relations.subject_id"]}),
    "equipment_feeds": ("""SELECT DISTINCT from_equipment, to_equipment FROM (
  SELECT subject_id AS from_equipment, object_id AS to_equipment FROM brick_relations WHERE predicate = 'feeds'
  UNION ALL SELECT object_id, subject_id FROM brick_relations WHERE predicate = 'isFedBy') f
WHERE from_equipment IN (SELECT subject_id FROM brick_entities WHERE category = 'equipment') AND to_equipment IN (SELECT subject_id FROM brick_entities WHERE category = 'equipment')""",
                        ["brick_relations", "brick_entities"], {"from_equipment": ["brick_relations.subject_id", "brick_relations.object_id"], "to_equipment": ["brick_relations.object_id", "brick_relations.subject_id"]}),
    "equipment_parts": ("""SELECT DISTINCT part_equipment, whole_equipment FROM (
  SELECT subject_id AS part_equipment, object_id AS whole_equipment FROM brick_relations WHERE predicate = 'isPartOf'
  UNION ALL SELECT object_id, subject_id FROM brick_relations WHERE predicate = 'hasPart') f
WHERE part_equipment IN (SELECT subject_id FROM brick_entities WHERE category = 'equipment') AND whole_equipment IN (SELECT subject_id FROM brick_entities WHERE category = 'equipment')""",
                        ["brick_relations", "brick_entities"], {"part_equipment": ["brick_relations.subject_id", "brick_relations.object_id"], "whole_equipment": ["brick_relations.object_id", "brick_relations.subject_id"]}),
    "space_parts": ("""SELECT DISTINCT part_space, whole_space FROM (
  SELECT subject_id AS part_space, object_id AS whole_space FROM brick_relations WHERE predicate = 'isPartOf'
  UNION ALL SELECT object_id, subject_id FROM brick_relations WHERE predicate = 'hasPart') f
WHERE part_space IN (SELECT subject_id FROM brick_entities WHERE category = 'space') AND whole_space IN (SELECT subject_id FROM brick_entities WHERE category = 'space')""",
                    ["brick_relations", "brick_entities"], {"part_space": ["brick_relations.subject_id", "brick_relations.object_id"], "whole_space": ["brick_relations.object_id", "brick_relations.subject_id"]}),
    "equipment_location": ("""SELECT DISTINCT equipment_id, space_id FROM (
  SELECT subject_id AS equipment_id, object_id AS space_id FROM brick_relations WHERE predicate = 'hasLocation'
  UNION ALL SELECT object_id, subject_id FROM brick_relations WHERE predicate = 'isLocationOf') f
WHERE equipment_id IN (SELECT subject_id FROM brick_entities WHERE category = 'equipment') AND space_id IN (SELECT subject_id FROM brick_entities WHERE category = 'space')""",
                           ["brick_relations", "brick_entities"], {"equipment_id": ["brick_relations.subject_id", "brick_relations.object_id"], "space_id": ["brick_relations.object_id", "brick_relations.subject_id"]}),
    "observations": ("""SELECT 'O' || LPAD(CAST(ROW_NUMBER() OVER (ORDER BY stream_id, event_time) AS VARCHAR), 6, '0') AS observation_id, stream_id,
       strftime(event_time, '%Y-%m-%dT%H:%M:%S') AS event_time, value FROM observations_raw""", ["observations_raw"],
                     {"observation_id": ["observations_raw.stream_id", "observations_raw.event_time"], "stream_id": ["observations_raw.stream_id"], "event_time": ["observations_raw.event_time"], "value": ["observations_raw.value"]}),
}
STATS_SQL = """SELECT stream_id, COUNT(*) AS n_observations, MIN(event_time) AS first_time, MAX(event_time) AS last_time, ROUND(MIN(value), 3) AS min_value, ROUND(MAX(value), 3) AS max_value,
       ROUND(AVG(value), 3) AS mean_value, ROUND(STDDEV_POP(value), 3) AS std_value FROM observations GROUP BY stream_id"""
SUMMARY_SQL = """SELECT e.equipment_id, e.brick_class, e.domain, COUNT(p.point_id) AS point_count, SUM(CASE WHEN p.has_data THEN 1 ELSE 0 END) AS instrumented_points, COALESCE(SUM(ps.n_observations), 0) AS observation_count
FROM equipment e LEFT JOIN equipment_points ep ON ep.equipment_id = e.equipment_id LEFT JOIN points p ON p.point_id = ep.point_id LEFT JOIN point_stats ps ON ps.stream_id = p.stream_id
GROUP BY e.equipment_id, e.brick_class, e.domain"""

LABELS = {"space_id": "空间编号", "brick_class": "Brick 类别", "equipment_id": "设备编号", "domain": "系统", "point_id": "测点编号", "stream_id": "数据流编号", "space_id_": "所在空间", "has_data": "有读数",
          "observation_id": "观测编号", "event_time": "时间", "value": "读数", "n_observations": "观测数", "first_time": "最早时间", "last_time": "最晚时间", "min_value": "最小值", "max_value": "最大值",
          "mean_value": "平均值", "std_value": "标准差", "point_count": "测点数", "instrumented_points": "有读数的测点", "observation_count": "观测总数", "from_equipment": "上游设备", "to_equipment": "下游设备",
          "part_equipment": "部件", "whole_equipment": "整体", "part_space": "组成空间", "whole_space": "所属空间"}


def load() -> Bundle:
    ttl, csv = DATA / "Site_B.ttl", DATA / "observations.csv"
    things, relations, streams = _read_metadata(ttl)
    observations_raw = _observations(csv)
    con = duck.connect(brick_entities=things, brick_relations=relations, brick_streams=streams, observations_raw=observations_raw)
    frames: dict[str, pd.DataFrame] = {}
    for name in ("spaces", "equipment", "points", "equipment_points", "space_points", "equipment_feeds", "equipment_parts", "space_parts", "equipment_location", "observations"):
        sql = SQL[name][0]
        frames[name] = con.execute(sql).df()
        duck.register(con, name, frames[name])
    frames["point_stats"] = con.execute(STATS_SQL).df()
    duck.register(con, "point_stats", frames["point_stats"])
    frames["equipment_summary"] = con.execute(SUMMARY_SQL).df()
    labels = lambda frame: {c: LABELS.get(c, c) for c in frame.columns}  # noqa: E731
    stage = lambda name, frame, label, description, source: Table(name=name, frame=frame, type_name=name.title().replace("_", ""), label=label, description=description, source=source, kind="stage")  # noqa: E731

    def derived(name: str, type_name: str, label: str, description: str, key: list[str], kind: str = "entity", **extra) -> Table:
        sql, inputs, columns = SQL[name]
        return Table(name=name, frame=frames[name], type_name=type_name, label=label, description=description, primary_key=key, column_labels=labels(frames[name]), kind=kind,
                     derived=Derived(sql=sql, inputs=inputs, columns=columns, description=description), **extra)

    tables = [
        stage("brick_entities", things, "Brick 实体", "元数据里每个有类型的东西：类别、种类（空间、设备、测点）和所属系统", "brick-ttl"),
        stage("brick_relations", relations, "Brick 关系", "元数据里的关系：isPointOf、feeds、isFedBy、hasPart、isPartOf、hasLocation、isLocationOf", "brick-ttl"),
        stage("brick_streams", streams, "测点的数据流", "每个测点读取哪一条数据流", "brick-ttl"),
        stage("observations_raw", observations_raw, "原始观测（每条数据流均匀抽取 150 个）", "BTS 数据集里 49 条数据流一年的读数，每条流均匀抽取 150 个", "bts-observations"),
        derived("spaces", "Space", "空间", "楼层、会议室、走廊、暖通分区等", ["space_id"]),
        derived("equipment", "Equipment", "设备", "风机盘管、空气处理机、冷热水盘管、阀门、风机、水泵、电表等", ["equipment_id"]),
        derived("points", "Point", "测点", "传感器、设定值、状态和命令；一个测点可以属于几台设备或几个空间，有的有数据流", ["point_id"],
                constraints={"has_data": {"enum": [True]}}),
        derived("observations", "Observation", "观测", "一条数据流在某个时刻的读数", ["observation_id"], kind="event", order_by="event_time"),
        Table(name="point_stats", frame=frames["point_stats"], type_name="PointStats", label="测点统计", description="每条数据流的观测数、时间范围、最小值、最大值、平均值和标准差", primary_key=["stream_id"],
              column_labels=labels(frames["point_stats"]), derived=Derived(sql=STATS_SQL, inputs=["observations"], description="按数据流汇总观测",
                                                                         columns={"stream_id": ["observations.stream_id"], "n_observations": ["observations.observation_id"], "first_time": ["observations.event_time"],
                                                                                  "last_time": ["observations.event_time"], "min_value": ["observations.value"], "max_value": ["observations.value"],
                                                                                  "mean_value": ["observations.value"], "std_value": ["observations.value"]}),
              constraints={"std_value": {"minimum": 0}}),
        Table(name="equipment_summary", frame=frames["equipment_summary"], type_name="EquipmentSummary", label="设备概况", description="每台设备的测点数、有读数的测点数和观测总数", primary_key=["equipment_id"],
              column_labels=labels(frames["equipment_summary"]), derived=Derived(sql=SUMMARY_SQL, inputs=["equipment", "equipment_points", "points", "point_stats"], description="把测点和它们的统计汇总到设备",
                                                                               columns={"equipment_id": ["equipment.equipment_id"], "brick_class": ["equipment.brick_class"], "domain": ["equipment.domain"],
                                                                                        "point_count": ["equipment_points.point_id"], "instrumented_points": ["points.has_data"], "observation_count": ["point_stats.n_observations"]})),
        derived("equipment_points", "EquipmentPoint", "设备的测点", "设备和它的测点（isPointOf）：一个测点可以属于几台设备", [], kind="bridge"),
        derived("space_points", "SpacePoint", "空间的测点", "空间和它的测点（isPointOf）", [], kind="bridge"),
        derived("equipment_feeds", "EquipmentFeed", "供给关系", "设备之间的 feeds：谁给谁供水、供风、供电", [], kind="bridge"),
        derived("equipment_parts", "EquipmentPart", "部件关系", "设备之间的 isPartOf / hasPart", [], kind="bridge"),
        derived("space_parts", "SpacePart", "空间组成", "空间之间的 isPartOf / hasPart", [], kind="bridge"),
        derived("equipment_location", "EquipmentLocation", "设备位置", "设备所在的空间（hasLocation / isLocationOf）", [], kind="bridge"),
    ]
    L = lambda name, st, sc, tt, tc, label: Link(name, st, sc, tt, tc, "declared_fk", label)  # noqa: E731
    links = [L("equipment_points_equipment", "equipment_points", "equipment_id", "equipment", "equipment_id", "设备"), L("equipment_points_point", "equipment_points", "point_id", "points", "point_id", "测点"),
             L("space_points_space", "space_points", "space_id", "spaces", "space_id", "空间"), L("space_points_point", "space_points", "point_id", "points", "point_id", "测点"),
             L("observations_stream_id", "observations", "stream_id", "points", "stream_id", "观测→测点"), L("point_stats_stream_id", "point_stats", "stream_id", "points", "stream_id", "测点统计→测点"),
             L("equipment_summary_equipment_id", "equipment_summary", "equipment_id", "equipment", "equipment_id", "设备概况→设备"),
             L("feeds_from", "equipment_feeds", "from_equipment", "equipment", "equipment_id", "供给：上游"), L("feeds_to", "equipment_feeds", "to_equipment", "equipment", "equipment_id", "供给：下游"),
             L("parts_part", "equipment_parts", "part_equipment", "equipment", "equipment_id", "部件"), L("parts_whole", "equipment_parts", "whole_equipment", "equipment", "equipment_id", "整体"),
             L("space_parts_part", "space_parts", "part_space", "spaces", "space_id", "组成空间"), L("space_parts_whole", "space_parts", "whole_space", "spaces", "space_id", "所属空间"),
             L("location_equipment", "equipment_location", "equipment_id", "equipment", "equipment_id", "位于：设备"), L("location_space", "equipment_location", "space_id", "spaces", "space_id", "位于：空间")]
    sources = [Source(id="brick-ttl", title="Site_B.ttl（Brick 元数据）", kind="rdf", location="backend/data/bts_demo/Site_B.ttl", license="BTS 数据集（Cruise Research Group）", url="https://github.com/cruiseresearchgroup/DIEF_BTS",
                      bytes=ttl.stat().st_size, sha256=hashlib.sha256(ttl.read_bytes()).hexdigest()),
               Source(id="bts-observations", title="observations.csv（49 条数据流，已降采样）", kind="file", location="backend/data/bts_demo/observations.csv", license="BTS 数据集（Cruise Research Group）",
                      url="https://github.com/cruiseresearchgroup/DIEF_BTS", bytes=csv.stat().st_size, sha256=hashlib.sha256(csv.read_bytes()).hexdigest())]
    return Bundle(id="demo-brick-site-b", title="楼宇设备与传感时序（Brick · BTS Site B）", domain="制造", kind="timeseries", ontology_name="楼宇设备与传感时序（Brick · BTS Site B）",
                  summary="一栋办公楼的暖通和电气系统：空间、设备、851 个测点按 Brick 本体连在一起（供给、组成、位置），其中 49 个测点有一年的传感器读数。设备、空间、测点由元数据计算出来，读数按测点汇总。",
                  sources=sources, tables=tables, links=links, tags=["时序数据", "Brick 本体", "真实数据", "关系复杂"],
                  notes=["元数据是 RDF（Brick Schema），先读成三元组，再用 SQL 算出空间、设备、测点和它们之间的关系。", "851 个测点里只有 49 个有读数：测点覆盖率这条业务规则会指出其余的。",
                         "读数每条数据流均匀抽取 150 个（原数据每条约 600 个），保证整个本体不超过 1 万个对象。"])
