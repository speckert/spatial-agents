# ===========================================================================
# Makefile — Spatial Agents development commands
#
# Version History:
#     0.1.0  2026-03-28  Initial Makefile with install, test, demo, serve,
#                        lint, typecheck, and clean targets
# ===========================================================================

.PHONY: install test demo serve lint typecheck clean all

# Default: install + test + demo
all: install test demo

# Install with dev dependencies
install:
	pip install -e ".[dev]"

# Run test suite
test:
	pytest tests/ -v --tb=short

# Run full pipeline demo (no server)
demo:
	python scripts/demo.py

# Run demo and start server on port 8012
serve:
	python scripts/demo.py --serve

# Lint with ruff
lint:
	ruff check spatial_agents/ tests/ scripts/
	ruff format --check spatial_agents/ tests/ scripts/

# Type check with mypy
typecheck:
	mypy spatial_agents/

# Clean generated files
clean:
	rm -rf __pycache__ .pytest_cache .mypy_cache .ruff_cache
	rm -rf spatial_agents.egg-info dist build
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true

# Format code
format:
	ruff format spatial_agents/ tests/ scripts/

# Run demo with verbose output
demo-verbose:
	python scripts/demo.py --verbose

# Docker build (cloud deployment)
docker-build:
	docker build --platform linux/arm64 -t spatial-agents -f spatial_agents/deploy/Dockerfile .

# Docker run
docker-run:
	docker run -p 8012:8012 -e SPATIAL_AGENTS_MODE=cloud spatial-agents
