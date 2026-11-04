# Vixy developer entry points. Everything delegates to scripts/ and `python -m vixy`;
# test, replay and diagnostics output is also written to logs/.
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -ec

VIXY_USER ?=
PORT ?= 8765
HOST ?= 127.0.0.1
SCENARIO ?=
SPEED ?=
REPLAY_ARGS ?=
FALKORDB_URL ?= redis://localhost:6390
export FALKORDB_URL

.PHONY: help install install-perception falkordb serve demo-data test test-integration smoke-av test-all replay diagnose

help:
	@echo "make install                 uv env + .env + FalkorDB + smoke test (scripts/bootstrap.sh)"
	@echo "make install-perception      same, plus the MP4 extra (YOLO/Whisper; needs ffmpeg)"
	@echo "make falkordb                start the shared FalkorDB server on localhost:6390"
	@echo "make demo-data VIXY_USER=u   load the two-day M2 demo into a new user (never 'demo')"
	@echo "make serve VIXY_USER=u       backend API on http://$(HOST):$(PORT)/v1"
	@echo "make test                    unit suites: context-engine, dreamer-model, runtime (offline)"
	@echo "make test-integration        FalkorDB server variants, agent scenarios, M1/M2 demos, HTTP smoke"
	@echo "make smoke-av                M3 audiovisual: MP4 -> YOLO + Whisper -> replay acceptance"
	@echo "make test-all                all of the above"
	@echo "make replay SCENARIO=path/scenario.json [SPEED=4] [REPLAY_ARGS=--fresh]   process + replay a local MP4"
	@echo "make diagnose                versions, FalkorDB, graphs and env (no secret values) -> logs/"

install:
	./scripts/bootstrap.sh

install-perception:
	./scripts/bootstrap.sh --perception

falkordb:
	docker compose up -d --wait falkordb

define require_user
	@if [ -z "$(VIXY_USER)" ]; then echo "set VIXY_USER=<user id>"; exit 2; fi
	@if [ "$(VIXY_USER)" = demo ]; then echo "VIXY_USER=demo is the preserved vixy_demo graph; pick another id"; exit 2; fi
endef

demo-data:
	$(require_user)
	uv run --frozen python -m vixy --user $(VIXY_USER) --state-dir data/$(VIXY_USER) demo-m2 --fresh

serve:
	$(require_user)
	uv run --frozen python -m vixy --user $(VIXY_USER) --state-dir data/$(VIXY_USER) serve --host $(HOST) --port $(PORT)

test:
	./scripts/test.sh unit

test-integration:
	./scripts/test.sh integration

smoke-av:
	./scripts/test.sh av

test-all:
	./scripts/test.sh all

replay:
	@if [ -z "$(SCENARIO)" ]; then echo "set SCENARIO=demo/inputs/<name>/scenario.json (see demo/README.md)"; exit 2; fi
	@mkdir -p logs
	uv run --frozen --extra perception python -m vixy process --scenario $(SCENARIO) 2>&1 | tee logs/process-$$(date +%Y%m%d-%H%M%S).log
	uv run --frozen --extra perception python -m vixy replay --scenario $(SCENARIO) $(if $(SPEED),--speed $(SPEED)) $(REPLAY_ARGS) 2>&1 | tee logs/replay-$$(date +%Y%m%d-%H%M%S).log

diagnose:
	./scripts/diagnose.sh
