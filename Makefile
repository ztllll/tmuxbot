.PHONY: install-dev test lint check py_compile

UV ?= uv

install-dev:
	$(UV) sync --extra dev

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .

py_compile:
	$(UV) run python -m compileall -q tmuxbot tests

check: py_compile test lint
