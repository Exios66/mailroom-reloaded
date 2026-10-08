# mailroom-reloaded developer entry points. Every target is a thin wrapper.
# `make` with no target prints the list.
.DEFAULT_GOAL := help

.PHONY: help dev dev-down dev-logs dev-ps dev-status dev-reset dev-test lint test smoke eval gmail

help:
	@echo "mailroom-reloaded targets:"
	@echo "  make dev         build + start the dev server stack (scripts/dev.sh up)"
	@echo "  make dev-down    stop the dev stack (scripts/dev.sh down)"
	@echo "  make dev-logs    follow dev stack logs (scripts/dev.sh logs)"
	@echo "  make dev-ps      show dev stack container status"
	@echo "  make dev-status  health-check the dev stack endpoints"
	@echo "  make dev-reset   stop the dev stack and delete ./data + volumes"
	@echo "  make dev-test    run the dev test suite (scripts/dev_test.sh)"
	@echo "  make lint        ruff check the tree"
	@echo "  make test        run the unit tests (live deselected)"
	@echo "  make smoke       end-to-end smoke against a running stack"
	@echo "  make eval        run an evaluation posture (uv run mailroom eval)"
	@echo "  make gmail       manage Gmail intake (uv run mailroom gmail)"

dev:
	scripts/dev.sh up

dev-down:
	scripts/dev.sh down

dev-logs:
	scripts/dev.sh logs

dev-ps:
	scripts/dev.sh ps

dev-status:
	scripts/dev.sh status

dev-reset:
	scripts/dev.sh reset

dev-test:
	scripts/dev_test.sh

lint:
	uv run ruff check .

test:
	uv run pytest

smoke:
	scripts/dev.sh smoke

eval:
	uv run mailroom eval

gmail:
	uv run mailroom gmail
