.PHONY: dev test lint validate release clean clean-caches help

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
	ruff check __main__.py tests/ scripts/

# validate depends on clean-caches: test/lint runs recreate
# __pycache__/.pytest_cache, which trip the validator's top-level layout
# check (v10.0.12). Cache-only so a standalone `make validate` doesn't
# delete a previously built dist/ zip.
# Two layers (v11.0.0): scripts/validate_plugin.py is the repo-hygiene layer
# (SQL safety, proxy domains, layout); `yourbot validate` is the SDK's
# platform publish gate (reserved names, handler consistency, option shapes).
validate: clean-caches
	python scripts/validate_plugin.py .
	yourbot validate --path .

release: clean lint validate test
	python scripts/build_release.py --output dist/
	python scripts/validate_zip.py dist/

clean: clean-caches
	rm -rf dist/ htmlcov/ .coverage

clean-caches:
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
