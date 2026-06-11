.PHONY: dev test lint validate release clean help

help:
	@echo "Targets:"
	@echo "  make dev       - local hot-reload loop (yourbot dev --watch)"
	@echo "  make test      - run pytest"
	@echo "  make lint      - ruff check (auto-fix safe issues)"
	@echo "  make validate  - pre-flight validator (manifest, caps, SQL safety, layout)"
	@echo "  make release   - lint + validate + test, then build dist/<plugin_id>-<version>.zip"
	@echo "  make clean     - remove caches and build artifacts"

dev:
	yourbot dev --watch

test:
	python -m pytest -q

lint:
	ruff check __main__.py tests/

validate:
	python scripts/validate_plugin.py .

release: clean lint validate test
	python scripts/build_release.py --output dist/

clean:
	rm -rf dist/ .pytest_cache/ .mypy_cache/ .ruff_cache/ htmlcov/ .coverage
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
