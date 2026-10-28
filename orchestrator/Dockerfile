# Orchestrator + pool worker image. No Docker socket, read-only rootfs at runtime (see compose).
FROM python:3.12-slim

# git: clone and push target repos. The Claude Code CLI that the Agent SDK drives (Pro route) is
# bundled inside the claude-agent-sdk wheel, so no Node.js is needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY factory ./factory
COPY migrations ./migrations

RUN useradd --uid 10001 --create-home --home-dir /home/factory factory
USER 10001
# Read-only rootfs: the bundled Claude Code CLI writes its config under $HOME, so HOME is the tmpfs.
ENV HOME=/tmp PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "-m"]
CMD ["factory.runner", "serve"]
