# Java runner (Maven). Build per target repo so its dependencies are resolved in (no network at test time):
#   make runner-java DEPS=/path/to/target-repo    (or make project-rebuild REPO=owner/name)
# Gradle builds are not supported yet.
FROM maven:3.9-eclipse-temurin-21

# deps/ holds the target repo's pom.xml (or is empty). Its dependencies and plugins are resolved into
# /opt/m2, then a stand-in test is compiled and run once so Surefire's test-framework provider is there too.
COPY deps/ /opt/deps/src/
RUN set -e; mkdir -p /opt/m2; \
    if [ -f /opt/deps/src/pom.xml ]; then \
      mkdir -p /opt/deps/probe/src/test/java && cd /opt/deps/probe && cp /opt/deps/src/pom.xml . \
      && mvn -B -ntp -Dmaven.repo.local=/opt/m2 dependency:go-offline dependency:resolve-plugins \
      && if grep -q 'junit-jupiter' pom.xml; then \
           printf 'class FactoryProbeTest { @org.junit.jupiter.api.Test void ok() {} }\n' > src/test/java/FactoryProbeTest.java; \
         elif grep -q '<artifactId>junit</artifactId>' pom.xml; then \
           printf 'public class FactoryProbeTest { @org.junit.Test public void ok() {} }\n' > src/test/java/FactoryProbeTest.java; \
         fi \
      && (mvn -B -ntp -Dmaven.repo.local=/opt/m2 -DfailIfNoTests=false test || true) \
      && cd / && rm -rf /opt/deps/probe; \
    fi; chmod -R a+rX /opt/m2

COPY entrypoint.sh /opt/factory/entrypoint.sh
ENV FACTORY_RUNNER=java
# Mount points for the broker's per-run volumes. A new volume takes the owner of the folder it is
# mounted on, so the runner (UID 10001) can write its source and results folders.
RUN mkdir -p /workspace/tests /workspace/.factory && chown -R 10001:10001 /workspace
RUN chmod 755 /opt/factory/entrypoint.sh && (useradd --uid 10001 --no-create-home runner || true)
USER 10001
