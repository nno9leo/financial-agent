# PowerShell equivalents: install: .venv\Scripts\python -m pip install -e ".[test]"; test: .venv\Scripts\python -m pytest -q; ingest: .venv\Scripts\python scripts\ingest.py
.PHONY: install test ingest
install:
	.venv/bin/python -m pip install -e '.[test]'
test:
	.venv/bin/python -m pytest -q
ingest:
	.venv/bin/python scripts/ingest.py
