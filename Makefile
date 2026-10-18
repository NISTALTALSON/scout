.PHONY: test lint check config

test:
	python scripts/test.py

lint:
	python -m ruff check scout tests scripts

config:
	python -c "from scout.config import Configuration; print('validated', len(Configuration().sources), 'source records')"

check: config lint test
