# Test-only runner image for `make test-sandbox`. Same mount points and user as the real runners.
FROM busybox:1.36.1
RUN mkdir -p /opt/factory /workspace/tests /workspace/.factory && chown -R 10001:10001 /workspace
COPY probe.sh /opt/factory/entrypoint.sh
RUN chmod 755 /opt/factory/entrypoint.sh
USER 10001
