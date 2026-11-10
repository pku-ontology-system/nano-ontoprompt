"""What an import is made of: sources, tables (read or derived), links between them, and an optional replay of an event table."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Source:
    """Where data came from, as shown on the lineage and on the data-source page."""
    id: str
    title: str
    kind: str = "file"                # file | generated | rdf | media | upload
    location: str | None = None
    license: str | None = None
    url: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    note: str | None = None


@dataclass
class Derived:
    """A table computed from other tables. `columns` says which input columns each output column is computed from (the lineage)."""
    sql: str
    inputs: list[str]
    columns: dict[str, list[str]] = field(default_factory=dict)
    description: str = ""


@dataclass
class Table:
    name: str
    frame: pd.DataFrame
    type_name: str
    label: str
    description: str = ""
    source: str | None = None
    primary_key: list[str] = field(default_factory=list)
    column_labels: dict[str, str] = field(default_factory=dict)
    constraints: dict[str, dict] = field(default_factory=dict)   # column -> {minimum, maximum, enum, pattern}: rules a data steward states
    derived: Derived | None = None
    kind: str = "entity"              # entity | event | asset | bridge (a link table: no object type, only relationships) | stage (read or built on the way: in the lineage, not in the ontology)
    order_by: str | None = None


@dataclass
class Link:
    name: str
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    basis: str = "declared_fk"        # declared_fk | semantic_fk
    label: str = ""
    coverage: float | None = None     # share of the source rows whose key is found in the target (inferred links)


@dataclass
class Replay:
    """An event table that is loaded in steps, in the order of `order_by`, instead of all at once."""
    table: str
    initial: int
    batch: int
    total: int
    order_by: str = ""


@dataclass
class Bundle:
    id: str
    title: str
    summary: str
    domain: str
    kind: str                         # relational | timeseries | events | multimodal | multisource
    ontology_name: str
    sources: list[Source]
    tables: list[Table]
    links: list[Link] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    replay: Replay | None = None

    def table(self, name: str) -> Table:
        return next(t for t in self.tables if t.name == name)


def snake(text: str) -> str:
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(text).strip()).strip("_").lower()
    return text if text and not text[0].isdigit() else f"t_{text}"


def pascal(text: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^0-9A-Za-z]+", str(text)) if part) or "Item"
