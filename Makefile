.PHONY: dev test validate release clean help

help:
	@echo "Targets:"
	@echo "  make dev       - local hot-reload loop (mmo dev --watch)"
	@echo "  make test      - run pytest"
	@echo "  make validate  - pre-flight validator (manifest, caps, SQL safety, layout)"
	@echo "  make release   - validate + test, then build dist/<plugin_id>-<version>.zip"
	@echo "  make clean     - remove caches and build artifacts"

dev:
	mmo dev --watch

test:
	python -m pytest -q

validate:
	python scripts/validate_plugin.py .

release: validate test
	python scripts/build_release.py --output dist/

clean:
	rm -rf dist/ .pytest_cache/ .mypy_cache/ .ruff_cache/ htmlcov/ .coverage
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
