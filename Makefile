SHELL := /bin/bash
.DEFAULT_GOAL := help

# Compose reads .env by itself; make does not. Without this, `make migrate` and
# `make check-db` run with an empty environment and fail with a message that
# looks like a code problem rather than a missing variable.
ifneq (,$(wildcard .env))
include .env
export
endif

# Compose reads .env by itself. `make migrate` and `make check-db` run on the
# host and do not, so they are given it explicitly. Without this they fail with
# a missing-variable error that looks like a code fault.
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: help lock lock-local fmt lint test up down logs migrate check-db verify-secrets gate

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

lock:  ## compile the .in files into hash-pinned lock files
	# --generate-hashes is required: the Dockerfile installs with
	# --require-hashes and will refuse a lock file without them.
	# Compiled inside the runtime image so the resolution matches Linux,
	# not whatever macOS happens to resolve to.
	docker run --rm -v "$(PWD)":/w -w /w $(PYTHON_BASE_IMAGE) sh -c "\
	  pip install --quiet pip-tools && \
	  pip-compile --generate-hashes --output-file=requirements.txt requirements.in && \
	  pip-compile --generate-hashes --output-file=requirements-dev.txt requirements-dev.in"

lock-local:  ## same, resolved on this machine. Faster, but macOS-resolved.
	pip-compile --generate-hashes --output-file=requirements.txt requirements.in
	pip-compile --generate-hashes --output-file=requirements-dev.txt requirements-dev.in

fmt:  ## format
	ruff format .

lint:  ## check formatting and lint
	ruff format --check .
	ruff check .

test:  ## run the non-live suite
	pytest -m "not live"

up:  ## start the stack
	docker compose up -d --build

down:  ## stop the stack, keep the data volume
	docker compose down

logs:  ## follow the service log
	docker compose logs -f policy_service

migrate:  ## apply migrations as bpr_owner
	python -m policy_service.db.migrate

check-db:  ## assert the n8n boundary holds against the running container
	python scripts/check_db_boundaries.py

verify-secrets:  ## local equivalent of the CI secret scan
	./scripts/verify_no_secrets.sh

gate: lint test check-db verify-secrets  ## the completion gate for this volume
	@echo "gate: passed"