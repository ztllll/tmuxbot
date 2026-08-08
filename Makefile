.PHONY: install-dev install-web test lint check check-web release-check py_compile version

UV ?= uv

install-dev:
	$(UV) sync --extra dev --extra web --extra feishu

install-web:
	npm --prefix webui ci

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .

py_compile:
	$(UV) run python -m compileall -q tmuxbot tests

version:
	$(UV) run python -c "import tmuxbot; print(tmuxbot.__version__)"

check: py_compile test lint

check-web: install-web
	npm --prefix webui test -- --run
	npm --prefix webui run build

release-check: check check-web
	$(UV) run tmuxbot doctor
