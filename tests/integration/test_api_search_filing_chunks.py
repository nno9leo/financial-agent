"""Real API search smoke test; requires the pinned AAPL filing to be indexed."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import ApiDeps, create_app
from app.citations.ledger import EvidenceLedger
from app.config import Settings
from app.ingestion.manifest import load_manifest


@pytest.mark.db
@pytest.mark.model
def test_api_search_filing_chunks_returns_pinned_aapl_evidence():
    settings = Settings(_env_file=None)
    manifest = load_manifest(Path("data/manifests/filings.v1.json"))

    class OfflineProvider:
        """The search endpoint needs a provider lifecycle, but no SEC requests."""

        def close(self):
            pass

    app = create_app(ApiDeps(
        settings=settings,
        sec_provider_factory=OfflineProvider,
        evidence_ledger=EvidenceLedger({}, manifest),
    ))
    response = TestClient(app).post("/tools/search_filing_chunks", json={
        "company": "AAPL",
        "question": "What drove iPhone net sales growth in fiscal 2025?",
    })

    assert response.status_code == 200, response.text
    evidence = response.json()["evidence"]
    assert len(evidence) >= 1
    assert any(row["accession_number"] == "0000320193-25-000079" for row in evidence)
