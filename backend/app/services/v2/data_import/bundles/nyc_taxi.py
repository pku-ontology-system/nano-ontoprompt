"""Taxi trips as a stream of events: the NYC TLC yellow-cab sample (10,000 real trips of January 2019) with the TLC's own code tables.

The trips are loaded in the order they began, a few hundred at a time, so the ontology grows the way an event stream does (start, step,
pause). The raw rows are a stage table: a computed trip table adds the trip's duration, speed and tip rate with their source columns.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.services.v2.data_import import duck
from app.services.v2.data_import.model import Bundle, Derived, Link, Replay, Source, Table
from app.services.v2.analysis import demos

FILE = "yellow_tripdata_sample_2019-01.csv"
ZONES = Path(demos.DEMO_DIR).parent / "data_import" / "nyc_taxi" / "taxi_zone_lookup.csv"
INITIAL, BATCH = 400, 400

VENDORS = {1: "Creative Mobile Technologies", 2: "VeriFone", 4: "未登记的服务商（编号 4）"}
RATE_CODES = {1: "标准费率", 2: "肯尼迪机场（JFK）", 3: "纽瓦克机场", 4: "拿骚或威彻斯特", 5: "协商价", 6: "拼车", 99: "未登记（编号 99）"}
PAYMENTS = {1: "信用卡", 2: "现金", 3: "免费", 4: "争议", 5: "未知", 6: "作废的行程"}

TRIP_SQL = """SELECT 'T' || LPAD(CAST(ROW_NUMBER() OVER (ORDER BY pickup_datetime, dropoff_datetime, pickup_location_id, dropoff_location_id, fare_amount) AS VARCHAR), 5, '0') AS trip_id,
       vendor_id, strftime(pickup_datetime, '%Y-%m-%dT%H:%M:%S') AS pickup_time, strftime(dropoff_datetime, '%Y-%m-%dT%H:%M:%S') AS dropoff_time,
       passenger_count, trip_distance, rate_code_id, store_and_fwd_flag, pickup_location_id AS pickup_zone_id, dropoff_location_id AS dropoff_zone_id, payment_type,
       fare_amount, extra, mta_tax, tip_amount, tolls_amount, improvement_surcharge, total_amount, congestion_surcharge,
       ROUND(DATEDIFF('second', pickup_datetime, dropoff_datetime) / 60.0, 2) AS duration_minutes,
       ROUND(CASE WHEN DATEDIFF('second', pickup_datetime, dropoff_datetime) > 0 THEN trip_distance / (DATEDIFF('second', pickup_datetime, dropoff_datetime) / 3600.0) END, 1) AS speed_mph,
       ROUND(CASE WHEN fare_amount > 0 THEN tip_amount / fare_amount END, 3) AS tip_rate
FROM raw_trips"""
TRIP_COLUMNS = {"vendor_id": ["raw_trips.vendor_id"], "pickup_time": ["raw_trips.pickup_datetime"], "dropoff_time": ["raw_trips.dropoff_datetime"], "passenger_count": ["raw_trips.passenger_count"],
                "trip_distance": ["raw_trips.trip_distance"], "rate_code_id": ["raw_trips.rate_code_id"], "store_and_fwd_flag": ["raw_trips.store_and_fwd_flag"], "pickup_zone_id": ["raw_trips.pickup_location_id"],
                "dropoff_zone_id": ["raw_trips.dropoff_location_id"], "payment_type": ["raw_trips.payment_type"], "fare_amount": ["raw_trips.fare_amount"], "extra": ["raw_trips.extra"], "mta_tax": ["raw_trips.mta_tax"],
                "tip_amount": ["raw_trips.tip_amount"], "tolls_amount": ["raw_trips.tolls_amount"], "improvement_surcharge": ["raw_trips.improvement_surcharge"], "total_amount": ["raw_trips.total_amount"],
                "congestion_surcharge": ["raw_trips.congestion_surcharge"], "duration_minutes": ["raw_trips.pickup_datetime", "raw_trips.dropoff_datetime"],
                "speed_mph": ["raw_trips.trip_distance", "raw_trips.pickup_datetime", "raw_trips.dropoff_datetime"], "tip_rate": ["raw_trips.tip_amount", "raw_trips.fare_amount"],
                "trip_id": ["raw_trips.pickup_datetime", "raw_trips.dropoff_datetime"]}
TRIP_LABELS = {"trip_id": "行程编号", "vendor_id": "服务商", "pickup_time": "上车时间", "dropoff_time": "下车时间", "passenger_count": "乘客数", "trip_distance": "里程（英里）", "rate_code_id": "费率代码",
               "store_and_fwd_flag": "离线暂存标记", "pickup_zone_id": "上车区域", "dropoff_zone_id": "下车区域", "payment_type": "支付方式", "fare_amount": "车费", "extra": "附加费", "mta_tax": "州税",
               "tip_amount": "小费", "tolls_amount": "过路费", "improvement_surcharge": "改善附加费", "total_amount": "总金额", "congestion_surcharge": "拥堵附加费", "duration_minutes": "时长（分钟）",
               "speed_mph": "平均时速", "tip_rate": "小费占车费比例"}
# What the TLC's data dictionary says a trip should look like, stated as constraints on the properties.
TRIP_RULES = {"passenger_count": {"minimum": 1, "maximum": 6}, "trip_distance": {"minimum": 0.01, "maximum": 100}, "fare_amount": {"minimum": 0, "maximum": 500}, "total_amount": {"minimum": 0, "maximum": 500},
              "mta_tax": {"enum": [0, 0.5]}, "tip_amount": {"minimum": 0}, "duration_minutes": {"minimum": 0.5, "maximum": 360}}


def _dimension(name: str, key: str, values: dict[int, str], observed: pd.Series, type_name: str, label: str, description: str) -> Table:
    ids = sorted({int(v) for v in observed.dropna().unique()} | set(values))
    frame = pd.DataFrame({key: ids, "name": [values.get(i, f"未登记（编号 {i}）") for i in ids]})
    return Table(name=name, frame=frame, type_name=type_name, label=label, description=description, source="tlc-dictionary", primary_key=[key], column_labels={key: "编号", "name": "名称"})


def load() -> Bundle:
    path = Path(demos.DEMO_DIR) / FILE
    raw = pd.read_csv(path, parse_dates=["pickup_datetime", "dropoff_datetime"])
    raw.columns = [c.lower() for c in raw.columns]
    sha = demos.sha256_of(path)
    con = duck.connect(raw_trips=raw)
    trips = con.execute(TRIP_SQL).df().sort_values("pickup_time", kind="stable").reset_index(drop=True)
    zone_frame = pd.read_csv(ZONES).rename(columns={"LocationID": "zone_id", "Zone": "name", "Borough": "borough"})[["zone_id", "name", "borough", "service_zone"]]
    duck.register(con, "trips", trips)
    activity_sql = """SELECT z.zone_id, COALESCE(p.pickups, 0) AS pickups, COALESCE(d.dropoffs, 0) AS dropoffs, ROUND(p.avg_fare, 2) AS avg_fare_from_zone, ROUND(p.avg_tip_rate, 3) AS avg_tip_rate
FROM zones z LEFT JOIN (SELECT pickup_zone_id, COUNT(*) AS pickups, AVG(fare_amount) AS avg_fare, AVG(tip_rate) AS avg_tip_rate FROM trips GROUP BY pickup_zone_id) p ON p.pickup_zone_id = z.zone_id
LEFT JOIN (SELECT dropoff_zone_id, COUNT(*) AS dropoffs FROM trips GROUP BY dropoff_zone_id) d ON d.dropoff_zone_id = z.zone_id"""
    duck.register(con, "zones", zone_frame)
    activity = con.execute(activity_sql).df()
    tables = [
        Table(name="raw_trips", frame=raw, type_name="RawTrip", label="原始行程记录", description="TLC 样本文件里的原始行", source="tlc-trips", kind="stage"),
        Table(name="trips", frame=trips, type_name="Trip", label="行程", description="一次出租车行程：上下车时间和区域、里程、车费、小费，以及由原始记录算出的时长、时速和小费比例",
              source=None, primary_key=["trip_id"], column_labels=TRIP_LABELS, constraints=TRIP_RULES, kind="event", order_by="pickup_time",
              derived=Derived(sql=TRIP_SQL, inputs=["raw_trips"], columns=TRIP_COLUMNS, description="给每条原始记录编号，改成统一的列名，算出时长、时速和小费比例")),
        _dimension("vendors", "vendor_id", VENDORS, trips["vendor_id"], "Vendor", "服务商", "提供计价器和结算的技术服务商"),
        _dimension("rate_codes", "rate_code_id", RATE_CODES, trips["rate_code_id"], "RateCode", "费率代码", "行程结束时计价器记录的费率"),
        _dimension("payment_types", "payment_type", PAYMENTS, trips["payment_type"], "PaymentType", "支付方式", "乘客的支付方式"),
        Table(name="zones", frame=zone_frame, type_name="Zone", label="区域", description="TLC 划分的 265 个出租车区域（上下车地点），带名称、行政区和服务区", source="tlc-zones", primary_key=["zone_id"],
              column_labels={"zone_id": "区域编号", "name": "区域名称", "borough": "行政区", "service_zone": "服务区"}),
        Table(name="zone_activity", frame=activity, type_name="ZoneActivity", label="区域活跃度", description="每个区域的上车次数、下车次数、上车车费均值和小费比例均值（由行程计算）", primary_key=["zone_id"],
              column_labels={"zone_id": "区域", "pickups": "上车次数", "dropoffs": "下车次数", "avg_fare_from_zone": "上车平均车费", "avg_tip_rate": "平均小费比例"},
              derived=Derived(sql=activity_sql, inputs=["trips", "zones"], description="按上车区域和下车区域分别统计行程",
                              columns={"zone_id": ["zones.zone_id"], "pickups": ["trips.pickup_zone_id"], "dropoffs": ["trips.dropoff_zone_id"], "avg_fare_from_zone": ["trips.fare_amount", "trips.pickup_zone_id"],
                                       "avg_tip_rate": ["trips.tip_rate", "trips.pickup_zone_id"]})),
    ]
    links = [Link("trips_vendor_id", "trips", "vendor_id", "vendors", "vendor_id", "declared_fk", "行程→服务商"), Link("trips_rate_code_id", "trips", "rate_code_id", "rate_codes", "rate_code_id", "declared_fk", "行程→费率"),
             Link("trips_payment_type", "trips", "payment_type", "payment_types", "payment_type", "declared_fk", "行程→支付方式"),
             Link("trips_pickup_zone_id", "trips", "pickup_zone_id", "zones", "zone_id", "declared_fk", "行程→上车区域"), Link("trips_dropoff_zone_id", "trips", "dropoff_zone_id", "zones", "zone_id", "declared_fk", "行程→下车区域"),
             Link("zone_activity_zone_id", "zone_activity", "zone_id", "zones", "zone_id", "declared_fk", "区域活跃度→区域")]
    sources = [Source(id="tlc-trips", title=FILE, kind="file", location=f"backend/examples/analysis/{FILE}", license="NYC TLC 公开数据（样本取自 Great Expectations 教程仓库）", sha256=sha,
                      url="https://github.com/great-expectations/gx_tutorials", bytes=path.stat().st_size),
               Source(id="tlc-zones", title="taxi_zone_lookup.csv（出租车区域对照表）", kind="file", location="backend/examples/data_import/nyc_taxi/taxi_zone_lookup.csv", license="NYC TLC 公开数据",
                      url="https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page", bytes=ZONES.stat().st_size, sha256=demos.sha256_of(ZONES)),
               Source(id="tlc-dictionary", title="TLC 黄色出租车数据字典：服务商、费率代码、支付方式", kind="file", license="NYC TLC 公开资料", url="https://www.nyc.gov/assets/tlc/downloads/pdf/data_dictionary_trip_records_yellow.pdf")]
    return Bundle(id="demo-nyc-taxi", title="出租车行程事件流（纽约 TLC）", domain="其他", kind="events", ontology_name="出租车行程事件流（纽约 TLC）",
                  summary="2019 年 1 月纽约黄色出租车的 1 万次真实行程，按上车时间一批一批载入：先出现第一批行程和它们的区域、服务商、费率、支付方式，再逐步推进。数据里有乘客数为 0、车费为负这类真实的脏数据。",
                  sources=sources, tables=tables, links=links, tags=["动态数据", "事件流", "真实数据", "脏数据"], replay=Replay(table="trips", initial=INITIAL, batch=BATCH, total=len(trips), order_by="pickup_time"),
                  notes=["行程按上车时间排序，先载入前 400 条，每次推进 400 条；区域活跃度按全部行程统计，推进时不重算。"])
