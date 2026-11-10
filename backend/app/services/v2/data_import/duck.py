"""DuckDB over pandas frames: DuckDB 1.2 cannot read pandas 3 string columns directly, so frames go in as Arrow tables."""
from __future__ import annotations

import duckdb
import pandas as pd
import pyarrow as pa


def connect(**frames: pd.DataFrame) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for name, frame in frames.items():
        register(con, name, frame)
    return con


def register(con: duckdb.DuckDBPyConnection, name: str, frame: pd.DataFrame) -> None:
    con.register(name, pa.Table.from_pandas(frame.reset_index(drop=True), preserve_index=False))
