# Named once, for FROM and for the label that says which base this is.
ARG BASE_IMAGE=alpine:3.24.2
FROM ${BASE_IMAGE}
ARG BASE_IMAGE

ARG VERSION=5.0.0_RC9

# Standard OCI labels. The version comes from the same VERSION that picks the
# source tag below, so it cannot say one thing and contain another; it is
# what lets the bot — and Watchtower, Diun, Portainer… — announce an update
# of this image with numbers instead of dates.
LABEL org.opencontainers.image.title="docker-controller-bot" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/dgongut/docker-controller-bot" \
      org.opencontainers.image.url="https://hub.docker.com/r/dgongut/docker-controller-bot" \
      org.opencontainers.image.licenses="GPL-3.0" \
      org.opencontainers.image.description="Control all your Docker containers from a single place: your Telegram." \
      org.opencontainers.image.documentation="https://github.com/dgongut/docker-controller-bot#readme" \
      org.opencontainers.image.authors="dgongut" \
      org.opencontainers.image.base.name="${BASE_IMAGE}"

ENV TZ=UTC \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies and download source
RUN apk add --no-cache python3 py3-pip tzdata curl unzip py3-paramiko openssh-client && \
    curl -fsSL https://github.com/dgongut/docker-controller-bot/archive/refs/tags/v${VERSION}.zip -o /tmp/app.zip && \
    unzip -q /tmp/app.zip -d /tmp && \
    mv /tmp/docker-controller-bot-${VERSION}/docker-controller-bot.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/core.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/commands.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/callbacks.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/config.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/store.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/migration.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/callback_registry.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/host_registry.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/i18n.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/docker_update.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/docker_compose_manager.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/compose_generator.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/formatting.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/own_container.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/schedule_manager.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/port_manager.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/logger.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/message_queue.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/telemetry.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/locale /app && \
    mv /tmp/docker-controller-bot-${VERSION}/requirements.txt /app && \
    rm -rf /tmp/app.zip /tmp/docker-controller-bot-${VERSION}/ && \
    apk del --no-cache curl unzip && \
    export PIP_BREAK_SYSTEM_PACKAGES=1 && \
    pip3 install --no-cache-dir -Ur /app/requirements.txt

# A remote ssh:// host that stops answering has to fail, not hang: docker-py
# reads the ssh pipe with no timeout of its own, so these are the only bound.
# Written as the system defaults, so a ~/.ssh/config mapped in still wins.
RUN mkdir -p /etc/ssh/ssh_config.d && \
    printf 'Host *\n    BatchMode yes\n    ConnectTimeout 10\n    ServerAliveInterval 10\n    ServerAliveCountMax 3\n' \
        > /etc/ssh/ssh_config.d/docker-controller-bot.conf

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)" || exit 1

ENTRYPOINT ["python3", "docker-controller-bot.py"]

# When and from which commit, passed at build time:
#   --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) --build-arg VCS_REF=$(git rev-parse HEAD)
# Last on purpose: they change on every build, and anything after them would
# be rebuilt every time instead of coming from the cache.
ARG BUILD_DATE
ARG VCS_REF
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}"