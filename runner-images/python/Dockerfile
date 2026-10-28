# Python runner. Build per target repo so its locked dependencies are baked in (no network at test time):
#   make runner-py DEPS=/path/to/target-repo
FROM python:3.12-slim

RUN pip install --no-cache-dir "ruff>=0.6" "pytest>=8" "pytest-timeout>=2.3" "bandit>=1.7"

# deps/ holds the target repo's requirements*.txt (or is empty). A lockfile change in a patch
# halts the run as ENVIRONMENT, so a human rebuilds this image.
COPY deps/ /opt/deps/
RUN set -e; for f in /opt/deps/requirements*.txt; do [ -f "$f" ] && pip install --no-cache-dir -r "$f"; done; true

COPY entrypoint.sh /opt/factory/entrypoint.sh
RUN chmod 755 /opt/factory/entrypoint.sh && useradd --uid 10001 --no-create-home runner
USER 10001
