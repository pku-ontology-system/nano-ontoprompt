"""Extract the full run-to-failure trajectories of NASA C-MAPSS FD001 units 1-5 for the factory sensor playback.

    python scripts/extract_cmapss_trajectories.py <NASA_C-MAPSS.zip> [--out examples/factory_twin]

The archive is the public NASA Prognostics Data Repository download (a zip that contains CMAPSSData.zip).  The factory
twin fixture's equipment EQ001-EQ005 are FD001 units 1-5, so the playback replays exactly those engines, from their first
cycle to the failure cycle.  Two channels are kept: sensor 4 (T50, low-pressure-turbine outlet temperature, degrees Rankine)
and sensor 11 (Ps30, static pressure at the high-pressure-compressor outlet, psia).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

UNITS = (1, 2, 3, 4, 5)
CHANNELS = {"sensor_4": 4, "sensor_11": 11}  # C-MAPSS columns after unit, cycle and three operating settings
SOURCE_URL = "https://phm-datasets.s3.amazonaws.com/NASA/6.+Turbofan+Engine+Degradation+Simulation+Data+Set.zip"
MEMBER = "train_FD001.txt"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "examples" / "factory_twin")
    args = parser.parse_args()
    outer = zipfile.ZipFile(args.archive)
    nested = next(name for name in outer.namelist() if name.endswith("CMAPSSData.zip"))
    inner = zipfile.ZipFile(io.BytesIO(outer.read(nested)))
    rows = [line.split() for line in inner.read(MEMBER).decode().splitlines() if line.strip()]
    selected = [row for row in rows if int(row[0]) in UNITS]
    selected.sort(key=lambda row: (int(row[0]), int(row[1])))

    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / "sensor_trajectories.csv"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["unit", "cycle", *CHANNELS])
        for row in selected:
            writer.writerow([int(row[0]), int(row[1]), *(row[4 + index] for index in CHANNELS.values())])
    lengths = {unit: sum(1 for row in selected if int(row[0]) == unit) for unit in UNITS}
    manifest = {
        "source": "NASA C-MAPSS FD001 train_FD001.txt (Prognostics Data Repository)", "source_url": SOURCE_URL, "source_member": MEMBER,
        "license": "NASA public dataset; follow the source dataset terms",
        "selection": "units 1-5, every cycle from the first to the failure cycle; the factory twin's EQ001-EQ005 are these units",
        "channels": {"sensor_4": "T50, low-pressure-turbine outlet temperature (degrees Rankine)",
                     "sensor_11": "Ps30, static pressure at the high-pressure-compressor outlet (psia)"},
        "units": lengths, "rows": len(selected),
        "sha256": {"source_archive": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
                   "sensor_trajectories.csv": hashlib.sha256(target.read_bytes()).hexdigest()},
    }
    (args.out / "sensor_trajectories.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(selected), "units": lengths, "csv": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
