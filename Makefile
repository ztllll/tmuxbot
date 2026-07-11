.PHONY: install-dev test lint check py_compile version

UV ?= uv

install-dev:
	$(UV) sync --extra dev --extra web --extra feishu

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .

py_compile:
	$(UV) run python -m compileall -q tmuxbot tests

version:
	$(UV) run python -c "import tmuxbot; print(tmuxbot.__version__)"

check: py_compile test lint
