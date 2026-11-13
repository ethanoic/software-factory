# Godot runner (GDScript, tested with GUT). Build per target repo:
#   make runner-godot DEPS=/path/to/target-repo   (or make project-rebuild REPO=owner/name)
# The repo must commit the GUT addon at addons/gut/ (9.x for Godot 4). The runner has no network.
FROM debian:bookworm-slim

ARG GODOT_VERSION=4.4.1
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip python3 python3-venv libfontconfig1 \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/gdtoolkit && /opt/gdtoolkit/bin/pip install --no-cache-dir "gdtoolkit==4.*" \
 && ln -s /opt/gdtoolkit/bin/gdlint /usr/local/bin/gdlint

# deps/ holds the repo's .godot-version (or is empty): the Godot release to download, 4.3 or later.
COPY deps/ /opt/deps/
RUN set -e; v=$(cat /opt/deps/.godot-version 2>/dev/null | tr -d '[:space:]'); v=${v:-$GODOT_VERSION}; \
    echo "$v" | grep -Eq '^4\.([3-9]|[1-9][0-9])(\.[0-9]+)?$' \
      || { echo "error: .godot-version must be a Godot 4 release, 4.3 or later (got '$v')" >&2; exit 1; }; \
    case "$TARGETARCH" in arm64) a=arm64 ;; *) a=x86_64 ;; esac; \
    curl -fsSL -o /tmp/godot.zip \
      "https://github.com/godotengine/godot/releases/download/${v}-stable/Godot_v${v}-stable_linux.${a}.zip"; \
    mkdir -p /opt/godot && unzip -q /tmp/godot.zip -d /opt/godot \
    && mv "/opt/godot/Godot_v${v}-stable_linux.${a}" /opt/godot/godot && chmod 755 /opt/godot/godot \
    && rm /tmp/godot.zip

COPY entrypoint.sh /opt/factory/entrypoint.sh
ENV FACTORY_RUNNER=godot
# Mount points for the broker's per-run volumes. A new volume takes the owner of the folder it is
# mounted on, so the runner (UID 10001) can write its source and results folders.
RUN mkdir -p /workspace/tests /workspace/.factory && chown -R 10001:10001 /workspace
RUN chmod 755 /opt/factory/entrypoint.sh && useradd --uid 10001 --no-create-home runner
USER 10001
