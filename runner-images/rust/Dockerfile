# Rust runner. Build per target repo so its crates are vendored and compiled in (no network at test time):
#   make runner-rs DEPS=/path/to/target-repo      (or make project-rebuild REPO=owner/name)
# Supports one crate at the repo root (Cargo.toml + Cargo.lock). Cargo workspaces are not supported yet.
FROM rust:1-slim-bookworm

RUN rustup component add clippy \
 && cargo install --locked cargo-nextest --version '^0.9' \
 && rm -rf /usr/local/cargo/registry /usr/local/cargo/git
COPY rust/nextest.toml /opt/factory/nextest.toml

# deps/ holds the target repo's Cargo.toml and Cargo.lock (or is empty). A stand-in crate with the same
# manifest is used to vendor every dependency and compile it once, so a run compiles only the repo's code.
COPY deps/ /opt/deps/src/
RUN set -e; mkdir -p /opt/deps/vendor; : > /opt/factory/cargo-config.toml; \
    if [ -f /opt/deps/src/Cargo.toml ]; then \
      mkdir -p /opt/deps/crate/src && cp /opt/deps/src/Cargo.* /opt/deps/crate/ && cd /opt/deps/crate \
      && echo 'fn main() {}' > src/main.rs && : > src/lib.rs \
      && locked=$([ -f Cargo.lock ] && echo --locked || true) \
      && { cargo vendor $locked /opt/deps/vendor > /opt/factory/cargo-config.toml \
           || { echo "error: cargo vendor failed. Workspaces and custom [lib]/[[bin]] paths are not supported yet." >&2; exit 1; }; } \
      && cp /opt/factory/cargo-config.toml /usr/local/cargo/config.toml \
      && (CARGO_TARGET_DIR=/opt/deps/target cargo build --offline --tests --all-targets || true) \
      && rm /usr/local/cargo/config.toml; \
    fi; chmod -R a+rX /opt/deps /opt/factory

COPY entrypoint.sh /opt/factory/entrypoint.sh
ENV FACTORY_RUNNER=rust
# Mount points for the broker's per-run volumes. A new volume takes the owner of the folder it is
# mounted on, so the runner (UID 10001) can write its source and results folders.
RUN mkdir -p /workspace/tests /workspace/.factory && chown -R 10001:10001 /workspace
RUN chmod 755 /opt/factory/entrypoint.sh && useradd --uid 10001 --no-create-home runner
USER 10001
