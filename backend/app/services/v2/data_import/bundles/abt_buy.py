"""One product catalogue from two web shops: Abt.com and Buy.com list many of the same products under different names.

The Leipzig entity-resolution benchmark gives both catalogues and the gold list of which Abt product is which Buy product (1,097 pairs).
The two catalogues become one Product type (each product keeps the shop it came from), and the gold pairs become a "same product" group on
every product, so entity resolution can be scored against it. Brands are read from the names.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd

from app.services.v2.data_import import duck
from app.services.v2.data_import.model import Bundle, Derived, Link, Source, Table
import hashlib

ZIP = Path(__file__).resolve().parents[5] / "examples" / "data_import" / "abt_buy" / "Abt-Buy.zip"
SHA = "b9ff2937d97371b7a00b0a97033213bfbdee24e8f1a0718a41ea8364472a8ff8"


def _read(archive: zipfile.ZipFile, name: str) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(archive.read(name)), encoding="latin-1")


PRODUCTS_SQL = """SELECT 'abt-' || CAST(id AS VARCHAR) AS product_id, 'abt' AS catalog_id, CAST(id AS VARCHAR) AS shop_product_id, name, description, NULL AS manufacturer,
       TRY_CAST(REPLACE(REPLACE(price, '$', ''), ',', '') AS DOUBLE) AS price FROM abt_raw
UNION ALL
SELECT 'buy-' || CAST(id AS VARCHAR), 'buy', CAST(id AS VARCHAR), name, description, manufacturer, TRY_CAST(REPLACE(REPLACE(price, '$', ''), ',', '') AS DOUBLE) FROM buy_raw"""
PRODUCTS_COLUMNS = {"product_id": ["abt_raw.id", "buy_raw.id"], "catalog_id": ["abt_raw.id", "buy_raw.id"], "shop_product_id": ["abt_raw.id", "buy_raw.id"], "name": ["abt_raw.name", "buy_raw.name"],
                    "description": ["abt_raw.description", "buy_raw.description"], "manufacturer": ["buy_raw.manufacturer"], "price": ["abt_raw.price", "buy_raw.price"]}
# the model number printed in a name: the last word that mixes letters and digits (or is a long number), spelled without dashes
MODEL_NO_SQL = r"""regexp_replace(list_extract(list_filter(regexp_extract_all(lower(p.name), '[a-z0-9][a-z0-9/.\-]*[a-z0-9]'),
    x -> length(x) >= 4 AND length(regexp_replace(x, '[^0-9]', '', 'g')) >= 2
         AND (length(regexp_replace(x, '[^a-z]', '', 'g')) >= 1 OR length(regexp_replace(x, '[^0-9]', '', 'g')) >= 6)
         AND NOT regexp_matches(x, '^[0-9.,]+(in|inch|mm|cm|ft|gb|mb|tb|hz|mhz|ghz|w|v|a|lb|lbs|oz|mp|x|p|i|k|ch)$')), -1), '[^a-z0-9]', '', 'g')"""
GROUPED_SQL = """WITH pairs AS (SELECT 'abt-' || CAST(idAbt AS VARCHAR) AS a, 'buy-' || CAST(idBuy AS VARCHAR) AS b FROM gold_raw),
 groups AS (SELECT a AS group_id, a AS product_id FROM pairs UNION SELECT a, b FROM pairs)
SELECT p.product_id, p.catalog_id, p.shop_product_id, p.name, p.description, p.manufacturer, p.price,
       COALESCE(MIN(g.group_id) OVER (PARTITION BY COALESCE(g.group_id, p.product_id)), p.product_id) AS true_entity,
       lower(split_part(regexp_replace(trim(p.name), '^[^A-Za-z0-9]+', ''), ' ', 1)) AS brand_id,
       __MODEL_NO__ AS model_no
FROM products_all p LEFT JOIN groups g ON g.product_id = p.product_id""".replace("__MODEL_NO__", MODEL_NO_SQL)
BRANDS_SQL = """SELECT brand_id, COUNT(*) AS product_count, SUM(CASE WHEN catalog_id = 'abt' THEN 1 ELSE 0 END) AS abt_products, SUM(CASE WHEN catalog_id = 'buy' THEN 1 ELSE 0 END) AS buy_products,
       ROUND(AVG(price), 2) AS avg_price FROM products GROUP BY brand_id"""
CATALOGS_SQL = """SELECT catalog_id, COUNT(*) AS product_count, ROUND(AVG(price), 2) AS avg_price, COUNT(price) AS priced_products FROM products GROUP BY catalog_id"""
MATCHES_SQL = """SELECT 'abt-' || CAST(idAbt AS VARCHAR) AS abt_product, 'buy-' || CAST(idBuy AS VARCHAR) AS buy_product FROM gold_raw"""


def load() -> Bundle:
    archive = zipfile.ZipFile(ZIP)
    abt, buy, gold = _read(archive, "Abt.csv"), _read(archive, "Buy.csv"), _read(archive, "abt_buy_perfectMapping.csv")
    con = duck.connect(abt_raw=abt, buy_raw=buy, gold_raw=gold)
    products_all = con.execute(PRODUCTS_SQL).df()
    duck.register(con, "products_all", products_all)
    products = con.execute(GROUPED_SQL).df().drop_duplicates("product_id").reset_index(drop=True)
    duck.register(con, "products", products)
    brands = con.execute(BRANDS_SQL).df()
    catalogs = con.execute(CATALOGS_SQL).df()
    catalogs["name"] = catalogs["catalog_id"].map({"abt": "Abt.com", "buy": "Buy.com"})
    matches = con.execute(MATCHES_SQL).df()
    sha = hashlib.sha256(ZIP.read_bytes()).hexdigest()
    sources = [Source(id="abt-buy", title="Abt-Buy.zip（Abt.csv、Buy.csv、abt_buy_perfectMapping.csv）", kind="file", location="backend/examples/data_import/abt_buy/Abt-Buy.zip", license="知识共享（莱比锡大学数据库组）",
                      url="https://dbs.uni-leipzig.de/research/projects/benchmark-datasets-for-entity-resolution", bytes=ZIP.stat().st_size, sha256=sha,
                      note="Köpcke, Thor, Rahm: Evaluation of entity resolution approaches on real-world match problems, VLDB 2010")]
    tables = [
        Table(name="abt_raw", frame=abt, type_name="AbtRaw", label="Abt.com 商品目录（原始）", description="Abt.com 的 1081 件商品", source="abt-buy", kind="stage"),
        Table(name="buy_raw", frame=buy, type_name="BuyRaw", label="Buy.com 商品目录（原始）", description="Buy.com 的 1092 件商品", source="abt-buy", kind="stage"),
        Table(name="gold_raw", frame=gold, type_name="GoldRaw", label="已知同款（原始）", description="基准数据集给出的 1097 对 Abt 与 Buy 的同款商品", source="abt-buy", kind="stage"),
        Table(name="products_all", frame=products_all, type_name="ProductsAll", label="两个目录合并", description="把两家网店的商品并成一张表，价格去掉美元符号", kind="stage",
              derived=Derived(sql=PRODUCTS_SQL, inputs=["abt_raw", "buy_raw"], columns=PRODUCTS_COLUMNS, description="两个目录并成一张表，统一列名，价格转成数字")),
        Table(name="products", frame=products, type_name="Product", label="商品", description="两家网店的全部商品。同一件商品在两个目录里各有一条；true_entity 是基准数据集给出的标准答案，用来给实体消歧打分",
              primary_key=["product_id"], column_labels={"product_id": "商品编号", "catalog_id": "来源网店", "shop_product_id": "网店里的编号", "name": "名称", "description": "描述", "manufacturer": "厂商",
                                                         "price": "价格", "true_entity": "已知同款分组", "brand_id": "品牌", "model_no": "型号"},
              derived=Derived(sql=GROUPED_SQL, inputs=["products_all", "gold_raw"], description="给每件商品标上已知同款的分组，并从名称里取出品牌和型号",
                              columns={"product_id": ["products_all.product_id"], "catalog_id": ["products_all.catalog_id"], "shop_product_id": ["products_all.shop_product_id"], "name": ["products_all.name"],
                                       "description": ["products_all.description"], "manufacturer": ["products_all.manufacturer"], "price": ["products_all.price"],
                                       "true_entity": ["gold_raw.idAbt", "gold_raw.idBuy", "products_all.product_id"], "brand_id": ["products_all.name"], "model_no": ["products_all.name"]}),
              constraints={"price": {"minimum": 0.01, "nullable": False}}),
        Table(name="brands", frame=brands, type_name="Brand", label="品牌", description="从商品名称第一个词取出的品牌，以及它在两家网店各有多少商品", primary_key=["brand_id"],
              column_labels={"brand_id": "品牌", "product_count": "商品数", "abt_products": "Abt 商品数", "buy_products": "Buy 商品数", "avg_price": "平均价格"},
              derived=Derived(sql=BRANDS_SQL, inputs=["products"], description="按品牌汇总商品",
                              columns={"brand_id": ["products.brand_id"], "product_count": ["products.product_id"], "abt_products": ["products.catalog_id"], "buy_products": ["products.catalog_id"], "avg_price": ["products.price"]})),
        Table(name="catalogs", frame=catalogs, type_name="Catalog", label="网店目录", description="商品来自哪家网店，各有多少商品、多少有价格", primary_key=["catalog_id"],
              column_labels={"catalog_id": "网店", "product_count": "商品数", "avg_price": "平均价格", "priced_products": "有价格的商品数", "name": "名称"},
              derived=Derived(sql=CATALOGS_SQL, inputs=["products"], description="按来源网店汇总商品",
                              columns={"catalog_id": ["products.catalog_id"], "product_count": ["products.product_id"], "avg_price": ["products.price"], "priced_products": ["products.price"]})),
        Table(name="known_matches", frame=matches, type_name="KnownMatch", label="已知同款", description="基准数据集给出的 Abt 与 Buy 的同款商品对", kind="bridge",
              derived=Derived(sql=MATCHES_SQL, inputs=["gold_raw"], description="把已知同款对的编号改成商品编号", columns={"abt_product": ["gold_raw.idAbt"], "buy_product": ["gold_raw.idBuy"]})),
    ]
    L = lambda name, st, sc, tt, tc, label: Link(name, st, sc, tt, tc, "declared_fk", label)  # noqa: E731
    links = [L("products_catalog_id", "products", "catalog_id", "catalogs", "catalog_id", "商品→网店"), L("products_brand_id", "products", "brand_id", "brands", "brand_id", "商品→品牌"),
             L("same_product_abt", "known_matches", "abt_product", "products", "product_id", "已知同款：Abt 一侧"), L("same_product_buy", "known_matches", "buy_product", "products", "product_id", "已知同款：Buy 一侧")]
    return Bundle(id="demo-abt-buy", title="商品主数据合并（Abt-Buy）", domain="供应链", kind="multisource", ontology_name="商品主数据合并（Abt-Buy）",
                  summary="两家网店各有一份商品目录，同一件商品常常在两边各有一条、名称写法不同。两份目录合成一个商品类型，每件商品带着基准数据集给出的“已知同款分组”，实体消歧可以对着它打分。",
                  sources=sources, tables=tables, links=links, tags=["多来源合并", "实体消歧", "真实数据", "有标准答案"],
                  notes=["Abt 1081 件、Buy 1092 件，已知同款 1097 对；真实的商品名称写法差别很大，所以这份数据的消歧难度高。", "品牌是商品名称的第一个词，型号是名称里最后一个字母数字混合的词，都只做了最简单的清洗。"])
