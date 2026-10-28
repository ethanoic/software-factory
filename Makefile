# AI Software Factory — run from the folder that also holds your existing compose.yml.
COMPOSE = docker compose -f compose.yml -f compose.factory.yml
# DEPS: path to a target repo, for building its runner image
DEPS ?=
# OMLX_MODELS: where your omlx server keeps MLX model folders
OMLX_MODELS ?= $(HOME)/models/mlx
PY ?= python3

.PHONY: help init lock db hoststat runner-py runner-ts up down logs test check status approve

help:
	@echo "init      create folders, secrets placeholder, .env.factory"
	@echo "lock      generate hashed lockfiles (needs uv)"
	@echo "db        create the factory database and role in your postgres"
	@echo "hoststat  install the macOS memory sensor (launchd)"
	@echo "runner-py DEPS=<repo>  build the Python runner image with that repo's deps"
	@echo "runner-ts DEPS=<repo>  build the TypeScript runner image with that repo's deps"
	@echo "up / down / logs       start, stop, follow the factory"
	@echo "test      run the factory's own unit tests (no services needed)"

init:
	mkdir -p workspace/inbox workspace/runs hoststat secrets qualification/code qualification/triage
	@[ -f .env.factory ] || (cp .env.factory.example .env.factory && chmod 600 .env.factory && echo "created .env.factory")
	@[ -f secrets/github_token ] || (touch secrets/github_token && chmod 600 secrets/github_token && echo "put a fine-grained GitHub token in secrets/github_token")

lock:
	cd orchestrator && uv pip compile requirements.in --generate-hashes --python-version 3.12 \
	  --python-platform aarch64-unknown-linux-gnu -o requirements.lock
	cd sandbox_broker && uv pip compile requirements.in --generate-hashes --python-version 3.12 \
	  --python-platform aarch64-unknown-linux-gnu -o requirements.lock

db:
	@. ./.env.factory; \
	printf "CREATE ROLE factory LOGIN PASSWORD '%s';\nCREATE DATABASE factory OWNER factory;\n" "$$FACTORY_DB_PASSWORD" \
	  | $(COMPOSE) exec -T postgres sh -c 'psql -U "$$POSTGRES_USER" -d postgres'

hoststat:
	chmod 755 hoststat/hoststat.sh
	sed -e "s#__FACTORY_DIR__#$(CURDIR)#g" -e "s#__OMLX_MODELS_DIR__#$(OMLX_MODELS)#g" \
	  hoststat/local.factory.hoststat.plist > $(HOME)/Library/LaunchAgents/local.factory.hoststat.plist
	-launchctl bootout gui/$$(id -u) $(HOME)/Library/LaunchAgents/local.factory.hoststat.plist 2>/dev/null
	launchctl bootstrap gui/$$(id -u) $(HOME)/Library/LaunchAgents/local.factory.hoststat.plist

runner-py:
	@test -n "$(DEPS)" || (echo "usage: make runner-py DEPS=/path/to/target-repo" && exit 1)
	rm -rf build/runner-py && mkdir -p build/runner-py/deps
	cp runner-images/entrypoint.sh runner-images/python/Dockerfile build/runner-py/
	-cp $(DEPS)/requirements*.txt build/runner-py/deps/ 2>/dev/null
	docker build -t factory-runner-py:locked build/runner-py

runner-ts:
	@test -n "$(DEPS)" || (echo "usage: make runner-ts DEPS=/path/to/target-repo" && exit 1)
	rm -rf build/runner-ts && mkdir -p build/runner-ts/deps build/runner-ts/typescript
	cp runner-images/entrypoint.sh runner-images/typescript/Dockerfile build/runner-ts/
	cp runner-images/typescript/eslint.security.mjs build/runner-ts/typescript/
	-cp $(DEPS)/package.json $(DEPS)/package-lock.json build/runner-ts/deps/ 2>/dev/null
	docker build -t factory-runner-ts:locked build/runner-ts

up:
	$(COMPOSE) up -d --build orchestrator pool-worker sandbox-broker egress-proxy

down:
	$(COMPOSE) stop orchestrator pool-worker sandbox-broker egress-proxy

logs:
	$(COMPOSE) logs -f orchestrator pool-worker sandbox-broker

status:
	$(COMPOSE) exec orchestrator python -m factory.runner status

approve:
	@test -n "$(RUN)" || (echo "usage: make approve RUN=<run_id>" && exit 1)
	$(COMPOSE) exec orchestrator python -m factory.runner approve $(RUN)

test:
	cd orchestrator && POOL_FILE=../pool.yaml LANGFUSE_DISABLED=1 $(PY) -m pytest -q ../tests
