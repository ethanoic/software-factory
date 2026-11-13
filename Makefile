# AI Software Factory — one service on this computer, with its own database. Run make from this folder.
# .env.factory feeds ${VAR} substitution in compose.factory.yml (compose reads only .env by default).
ENV_FILES = $(if $(wildcard .env.factory),--env-file .env.factory)
COMPOSE = docker compose $(ENV_FILES) -f compose.factory.yml
export COMPOSE
# DEPS: path to a target repo, for building its runner image
DEPS ?=
# OMLX_MODELS: where your omlx server keeps MLX model folders
OMLX_MODELS ?= $(HOME)/models/mlx
PY ?= python3

.PHONY: help init lock db hoststat runner-py runner-ts runner-rs runner-java runner-godot up down logs test test-sandbox sandbox-check runs-clean check status approve diff pool claude-token claude-token-paste claude-token-check board db-board backup restore project-add project-list project-rebuild project-rebuild-all project-pause project-resume project-limit project-labelers project-remove

help:
	@echo "init      create folders, secrets placeholder, .env.factory (with generated database passwords)"
	@echo "lock      generate hashed lockfiles (needs uv)"
	@echo ""
	@echo "Projects (the repos the factory works on):"
	@echo "project-add REPO=owner/name LANGUAGE=python|node|rust|java|godot [BRANCH=main] [MAX_RUNS=n]"
	@echo "project-list                    every project, its state and runner image"
	@echo "project-rebuild REPO=...        rebuild a project's runner image (after its dependencies change)"
	@echo "project-rebuild-all             rebuild every project's image (after updating the factory)"
	@echo "project-pause / project-resume REPO=...   stop or restart taking its tickets"
	@echo "project-limit REPO=... N=2|none at most N runs a day for this project"
	@echo "project-labelers REPO=... LOGINS='a b'    extra GitHub logins allowed to label its issues"
	@echo "project-remove REPO=...         forget a project (its history stays)"
	@echo ""
	@echo "diff RUN=<run_id>      print the change a run made; read it before you approve"
	@echo "backup / restore FILE=...      dump or restore the factory's database"
	@echo "hoststat  install the macOS memory sensor (launchd)"
	@echo "runner-py DEPS=<repo>  build the Python runner image with that repo's deps"
	@echo "runner-ts DEPS=<repo>  build the Node (JavaScript or TypeScript) runner image with that repo's deps"
	@echo "runner-rs DEPS=<repo>  build the Rust runner image (vendors and compiles Cargo.lock's crates)"
	@echo "runner-java DEPS=<repo>    build the Java runner image (resolves pom.xml offline; Maven only)"
	@echo "runner-godot DEPS=<repo>   build the Godot runner image (.godot-version picks the release; needs GUT in the repo)"
	@echo "up / down / logs       start, stop, follow the factory"
	@echo "test      run the factory's own unit tests (no services needed)"
	@echo "test-sandbox           prove the runner is sealed: real container, no network, no .git, safe results"
	@echo "sandbox-check          list runner containers or volumes left behind (should be none)"
	@echo "runs-clean DAYS=14     delete clones of finished runs older than DAYS from the runs volume"
	@echo "pool ARGS=...          model pool: refresh, catalog, list, trust, promote, once"
	@echo "claude-token           get a Claude plan token with your current login (claude setup-token)"
	@echo "claude-token-check     show the stored token's expiry and test it from the factory"
	@echo "board                  open the factory board (what is where, and what needs you)"
	@echo "db-board               recreate the board's read-only database role (new databases get it automatically)"

init:
	mkdir -p workspace/inbox hoststat secrets qualification/code qualification/triage backups
	@scripts/env-check.sh
	@[ -f secrets/github_token ] || (touch secrets/github_token && chmod 600 secrets/github_token && echo "put a fine-grained GitHub token in secrets/github_token")
	@[ -f secrets/claude_oauth_token ] || (touch secrets/claude_oauth_token && chmod 600 secrets/claude_oauth_token && echo "run make claude-token to store your Claude plan token")

lock:
	cd orchestrator && uv pip compile requirements.in --generate-hashes --python-version 3.12 \
	  --python-platform aarch64-unknown-linux-gnu -o requirements.lock
	cd sandbox_broker && uv pip compile requirements.in --generate-hashes --python-version 3.12 \
	  --python-platform aarch64-unknown-linux-gnu -o requirements.lock

db:
	@echo "Nothing to do: the factory now has its own database, created by make up on first start."

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

runner-rs:
	@test -n "$(DEPS)" || (echo "usage: make runner-rs DEPS=/path/to/target-repo" && exit 1)
	rm -rf build/runner-rs && mkdir -p build/runner-rs/deps build/runner-rs/rust
	cp runner-images/entrypoint.sh runner-images/rust/Dockerfile build/runner-rs/
	cp runner-images/rust/nextest.toml build/runner-rs/rust/
	-cp $(DEPS)/Cargo.toml $(DEPS)/Cargo.lock build/runner-rs/deps/ 2>/dev/null
	docker build -t factory-runner-rs:locked build/runner-rs

runner-java:
	@test -n "$(DEPS)" || (echo "usage: make runner-java DEPS=/path/to/target-repo" && exit 1)
	rm -rf build/runner-java && mkdir -p build/runner-java/deps
	cp runner-images/entrypoint.sh runner-images/java/Dockerfile build/runner-java/
	-cp $(DEPS)/pom.xml build/runner-java/deps/ 2>/dev/null
	docker build -t factory-runner-java:locked build/runner-java

runner-godot:
	@test -n "$(DEPS)" || (echo "usage: make runner-godot DEPS=/path/to/target-repo" && exit 1)
	rm -rf build/runner-godot && mkdir -p build/runner-godot/deps
	cp runner-images/entrypoint.sh runner-images/godot/Dockerfile build/runner-godot/
	-cp $(DEPS)/.godot-version build/runner-godot/deps/ 2>/dev/null
	docker build -t factory-runner-godot:locked build/runner-godot

SERVICES = postgres orchestrator pool-worker sandbox-broker docker-proxy egress-proxy board

up:
	@scripts/env-check.sh
	$(COMPOSE) up -d --build $(SERVICES)

down:
	$(COMPOSE) stop $(SERVICES)

logs:
	$(COMPOSE) logs -f orchestrator pool-worker sandbox-broker

status:
	$(COMPOSE) exec orchestrator python -m factory.runner status

claude-token:
	scripts/claude-token.sh new
	@$(MAKE) --no-print-directory _restart-if-running

claude-token-paste:
	scripts/claude-token.sh paste
	@$(MAKE) --no-print-directory _restart-if-running

claude-token-check:
	scripts/claude-token.sh status
	$(COMPOSE) exec orchestrator python -m factory.claude_token check

_restart-if-running:
	@if [ -n "$$($(COMPOSE) ps -q orchestrator 2>/dev/null)" ]; then \
	  echo "restarting the factory to load the new token"; $(COMPOSE) restart orchestrator pool-worker; fi

board:
	@url=http://localhost:8090; (command -v open >/dev/null && open $$url) || echo "Open $$url"

db-board:
	@. ./.env.factory; test -n "$$FACTORY_BOARD_DB_PASSWORD" || (echo "set FACTORY_BOARD_DB_PASSWORD in .env.factory first" && exit 1); \
	$(COMPOSE) exec -T -e PW="$$FACTORY_BOARD_DB_PASSWORD" postgres \
	  sh -c 'psql -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d postgres -v pw="$$PW"' < orchestrator/sql/board_role.sql

pool:
	@test -n "$(ARGS)" || (echo 'usage: make pool ARGS="refresh | catalog | list | trust <model> [roles] | promote <model> <role> | once"' && exit 1)
	$(COMPOSE) exec pool-worker python -m factory.pool_worker $(ARGS)

approve:
	@test -n "$(RUN)" || (echo "usage: make approve RUN=<run_id>" && exit 1)
	$(COMPOSE) exec orchestrator python -m factory.runner approve $(RUN)

diff:
	@test -n "$(RUN)" || (echo "usage: make diff RUN=<run_id>" && exit 1)
	@$(COMPOSE) exec -T orchestrator python -m factory.runner diff $(RUN)

# ---------------------------------------------------------------- projects
project-add:
	@scripts/project.sh add "$(REPO)" "$(LANGUAGE)" "$(or $(BRANCH),main)" "$(MAX_RUNS)"
project-rebuild:
	@scripts/project.sh rebuild "$(REPO)"
project-rebuild-all:
	@scripts/project.sh rebuild-all
project-list:
	@$(COMPOSE) exec -T orchestrator python -m factory.projects list
project-pause:
	@$(COMPOSE) exec -T orchestrator python -m factory.projects pause "$(REPO)"
project-resume:
	@$(COMPOSE) exec -T orchestrator python -m factory.projects resume "$(REPO)"
project-limit:
	@$(COMPOSE) exec -T orchestrator python -m factory.projects limit "$(REPO)" "$(N)"
project-labelers:
	@$(COMPOSE) exec -T orchestrator python -m factory.projects labelers "$(REPO)" $(LOGINS)
project-remove:
	@$(COMPOSE) exec -T orchestrator python -m factory.projects remove "$(REPO)"

# ---------------------------------------------------------------- the factory's database
backup:
	@mkdir -p backups
	@f=backups/factory-$$(date +%Y%m%d-%H%M%S).sql.gz; \
	$(COMPOSE) exec -T postgres pg_dump -U factory -d factory --clean --if-exists | gzip > $$f && \
	chmod 600 $$f && echo "saved $$f"

restore:
	@test -f "$(FILE)" || (echo "usage: make restore FILE=backups/factory-<date>.sql.gz" && exit 1)
	@echo "This replaces the factory's database with $(FILE). Stopping the factory first."
	$(COMPOSE) stop orchestrator pool-worker board
	gunzip -c "$(FILE)" | $(COMPOSE) exec -T postgres psql -q -v ON_ERROR_STOP=1 -U factory -d factory
	$(COMPOSE) start orchestrator pool-worker board

test:
	cd orchestrator && POOL_FILE=../pool.yaml LANGFUSE_DISABLED=1 $(PY) -m pytest -q ../tests

# Builds a test-only runner that probes its sandbox and plants the review's attacks, then runs it through
# the real broker code against Docker. Needs Docker running and `pip install docker fastapi httpx`.
test-sandbox:
	docker build -q -t factory-runner-test:local tests/runner-probe
	cd orchestrator && FACTORY_TEST_RUNNER_IMAGE=factory-runner-test:local POOL_FILE=../pool.yaml \
	  LANGFUSE_DISABLED=1 $(PY) -m pytest -q -rs ../tests/test_security.py

sandbox-check:
	@echo "Runner and loader containers (should be empty unless a run is in progress):"
	@docker ps -a --filter label=factory.run_id --format '  {{.Names}}  {{.Status}}'
	@echo "Per-run volumes (should be empty unless a run is in progress):"
	@docker volume ls --filter label=factory.run_id --format '  {{.Name}}'

DAYS ?= 14
runs-clean:
	$(COMPOSE) exec orchestrator python -m factory.runner clean --days $(DAYS)
