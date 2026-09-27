#!/usr/bin/env python3
"""Export the local FastAPI tool contracts without starting a server."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import ApiDeps, create_app


class _TraceSink:
    def write(self, _row):
        pass


class _UnusedSecProvider:
    pass


class _UnusedSearchRepository:
    pass


class _UnusedEmbedder:
    pass


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    client = TestClient(create_app(ApiDeps(
        sec_provider_factory=_UnusedSecProvider,
        search_repository=_UnusedSearchRepository(),
        embedder_factory=_UnusedEmbedder,
        trace_writer=_TraceSink(),
    )))
    response = client.get("/tools")
    response.raise_for_status()
    target = root / "tests/fixtures/api/tools.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(response.json(), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target.relative_to(root)}")


if __name__ == "__main__":
	main()
