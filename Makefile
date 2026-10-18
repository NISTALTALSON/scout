.PHONY: test lint check config demo

test:
	uv run python scripts/test.py

lint:
	uv run ruff check scout tests scripts

config:
	uv run python -c "from scout.config import Configuration; print('validated', len(Configuration().sources), 'source records')"

demo:
	uv run python scripts/demo.py

check: config lint test
