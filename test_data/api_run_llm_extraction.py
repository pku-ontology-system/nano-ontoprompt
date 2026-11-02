"""Compatibility entry point for the retired API-only extraction demo.

The v1 /execute writer has been retired. This script now exercises only the
private, deterministic structured Dataset Construction path; it makes no LLM
calls and must not be presented as a semantic extraction benchmark.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from construction_api_flow import (
    build_private_structured_dataset,
    first_structured_file,
    login_reviewer_session,
    login_session,
)


API = os.environ.get("PROJECT4_API_BASE", "http://localhost:8000").rstrip("/")
FIXTURES = Path(__file__).parent
DOMAINS = ("供应链", "医疗", "财务")


def main() -> int:
    session = login_session(API)
    reviewer = login_reviewer_session(API)
    print("Dataset Construction mode: private deterministic mapping; no LLM requests")
    results = []
    for domain in DOMAINS:
        source = first_structured_file(FIXTURES / domain)
        result = build_private_structured_dataset(
            session,
            API,
            source,
            reviewer_session=reviewer,
            domain=domain,
            ontology_name=f"{domain}-DatasetConstruction-{int(time.time())}",
        )
        results.append(result)
        print(
            f"{domain}: {result['status']} · {source.name} · "
            f"entity types={result['entities']} · instances={result['instances']} · "
            f"edges={result['edges']} · ontology={result['ontology_id']}"
        )
    return 0 if all(item.get("status") == "completed" for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
