# ADR 0002 — Pin accession numbers for every fact, chunk, citation and eval item

- **Status:** Accepted (2026-09-25). Implemented — see Evidence below.
- **Date:** 2026-09-25
- **Handoff refs:** §3.4, §3.7, §7 坑1, 坑4, 坑9, Q5, Q12, Q15

## Context

The same company and metric are re-reported, and sometimes restated, in later filings. `companyfacts` holds values from many filings, periods, units and forms. Querying by ticker + fiscal year alone mixes versions and leaks post-cutoff data into historical answers.

The two pinned inputs behave differently upstream:

- **The 10-K primary document** at `https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc>` is immutable once filed. The same URL always returns the same bytes.
- **`companyfacts`** (`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`) is a live aggregate. Its bytes change every time the company files anything new (a 10-Q, an 8-K with XBRL, an amendment). Each fact in it still carries its own `accn` (source accession) and `filed` date, so the facts reported by the pinned 10-K remain present and identifiable in later snapshots.

The raw cache (`data/raw/`) is gitignored (large raw caches are not committed). A fresh clone therefore has no local copy of the original companyfacts snapshot. Re-downloading it will almost always produce bytes whose sha256 differs from the one recorded in the manifest.

## Decision

1. **Pin one original 10-K per company in a versioned manifest.** `data/manifests/filings.v1.json` records, per company: CIK, ticker, company name, form, accession, filing date, report date, primary document, source URL, `document_sha256`, `companyfacts_url`, `companyfacts_sha256`, `retrieved_at` and the selection rule. The selection rule is "most recent original 10-K by filing date; amendments excluded".
2. **Manifests are immutable.** A manifest file is never overwritten or edited in place. Every new version is created exclusively (it fails if the file already exists), written via temp file + atomic rename, and carries its own `manifest_version`.
3. **Re-selecting filings requires an explicit `--repin`.** It selects afresh and writes the next free version `filings.v{N+1}.json`. Earlier versions stay untouched.
4. **The document hash mismatch is always a hard failure.** EDGAR documents are immutable, so a mismatch means corruption, tampering or a wrong URL. There is no override flag.
5. **A companyfacts hash mismatch is a hard failure by default.** The error must name the ticker, the pinned hash and the observed hash, and point to the flag below. It must never be silently accepted, and the new bytes must never be written over the pinned record.
6. **An explicit flag accepts a new companyfacts snapshot, and only by writing a new manifest version.** `scripts/ingest.py --accept-new-companyfacts-snapshot`:
   - keeps the **same accessions** as the latest manifest. It does not re-select filings; that is `--repin`'s job, and the two flags are mutually exclusive.
   - still verifies every `document_sha256` (rule 4 applies).
   - writes `filings.v{N+1}.json` with the new `companyfacts_sha256` and `retrieved_at`, plus `supersedes: "v{N}"` and `change_reason: "companyfacts_snapshot_refresh"`.
   - leaves every earlier manifest byte-identical.
7. **Downstream code must not depend on companyfacts bytes; it depends on pinned accession + cutoff.** Fact selection only admits facts whose `accn` equals the pinned accession **and** whose `filed` date is on or before the cutoff (default: the pinned filing date). The accession check comes first; the cutoff only filters *within* the pinned accession and never admits another filing. *(Corrected 2026-09-25: the original wording said "or", which would have admitted other accessions; the error surfaced while implementing fact selection.)* That is why accepting a newer snapshot cannot change a pinned answer. A test (`tests/unit/test_fact_selection.py`) shows that the same pinned facts give the same metric results from two different snapshots. If a pinned fact's value differs between snapshots, that is a typed error, never a silent choice.
8. **Everything downstream carries the accession.** Facts, chunks, evidence, metric results and eval questions all carry the pinned accession. Cross-accession mixing is a tested invariant.
9. **Eval items carry the pin.** Each has `accession_number` and `cutoff_date`. Only facts filed on or before the cutoff are eligible.

## Alternatives considered

- **Treat a companyfacts mismatch as a warning and continue.** Rejected. It silently changes the data snapshot behind every numeric answer and eval ground truth, and violates §3.7 ("failures must be explicit").
- **Drop the companyfacts hash from the manifest.** Rejected. It loses the ability to tell which snapshot a run used (each eval run's `environment.json` records the data manifest hash).
- **Commit the raw companyfacts snapshots to git.** Not adopted for the MVP. The files are multi-megabyte and the DoD forbids a large raw cache in the repo. This can be revisited, for example with a compressed release asset, if fresh-clone reproducibility of the *exact* snapshot becomes necessary.
- **Rebuild companyfacts from the 10-K's own XBRL instance.** Out of scope for this project (it amounts to a full XBRL platform). Listed as future work.

## Consequences

- (+) Numbers, text evidence, citations and eval all point to one filing version. Runs are reproducible offline from a warm cache.
- (+) Any snapshot change is visible in git history as a new manifest version with a stated reason, and every eval run records which manifest version it used.
- (+) Answers stay stable across snapshot refreshes because selection keys on accession + cutoff, not on file bytes (rule 7, enforced by a test).
- (−) Answers are "as of the pinned filing", not "latest". This must be stated in outputs and the README.
- (−) A fresh clone must run `--accept-new-companyfacts-snapshot` once before ingesting, producing a new manifest version. The README's fresh-clone instructions must say so. Results from different manifest versions must be reported as different runs.

## Evidence (fill when implemented)

- Manifest immutability, exclusive/atomic versioned writes, `--repin`, and the hard failure on hash mismatch: `scripts/ingest.py`, `app/ingestion/manifest.py`; tests in `tests/unit/test_manifest.py` and `tests/unit/test_ingest.py`.
- `--accept-new-companyfacts-snapshot`: `scripts/ingest.py`, tested in `tests/unit/test_ingest.py`.
- Snapshot-invariance test for pinned facts: `tests/unit/test_fact_selection.py`.
- Top-k retrieval never crosses accession: `tests/integration/test_search_repository.py`.
