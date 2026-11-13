# TypeScript runner. Build per target repo so node_modules are baked in (no network at test time):
#   make runner-ts DEPS=/path/to/target-repo
FROM node:20-slim

WORKDIR /opt/factory
RUN npm init -y > /dev/null \
 && npm install --no-audit --no-fund "eslint@^9" "eslint-plugin-security@^3" \
 && npm cache clean --force
COPY typescript/eslint.security.mjs /opt/factory/eslint.security.mjs

# deps/ holds the target repo's package.json + lockfile.
COPY deps/ /opt/deps/
RUN cd /opt/deps && if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
    elif [ -f package.json ]; then npm install --no-audit --no-fund; fi; npm cache clean --force

COPY entrypoint.sh /opt/factory/entrypoint.sh
ENV FACTORY_RUNNER=node
# Mount points for the broker's per-run volumes. A new volume takes the owner of the folder it is
# mounted on, so the runner (UID 10001) can write its source and results folders.
RUN mkdir -p /workspace/tests /workspace/.factory && chown -R 10001:10001 /workspace
RUN chmod 755 /opt/factory/entrypoint.sh && useradd --uid 10001 --no-create-home runner || true
USER 10001
