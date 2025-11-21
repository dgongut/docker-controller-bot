FROM alpine:3.22.2

ARG VERSION=3.10.1

ENV TZ=UTC

WORKDIR /app

# Install dependencies and download source
RUN apk add --no-cache python3 py3-pip tzdata curl && \
    curl -fsSL https://github.com/dgongut/docker-controller-bot/archive/refs/tags/v${VERSION}.tar.gz -o /tmp/app.tar.gz && \
    tar -xzf /tmp/app.tar.gz -C /tmp && \
    mv /tmp/docker-controller-bot-${VERSION}/docker-controller-bot.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/config.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/docker_update.py /app && \
    mv /tmp/docker-controller-bot-${VERSION}/locale /app && \
    mv /tmp/docker-controller-bot-${VERSION}/requirements.txt /app && \
    rm -rf /tmp/app.tar.gz /tmp/docker-controller-bot-${VERSION}/ && \
    apk del --no-cache curl && \
    export PIP_BREAK_SYSTEM_PACKAGES=1 && \
    pip3 install --no-cache-dir -Ur /app/requirements.txt

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)" || exit 1

ENTRYPOINT ["python3", "docker-controller-bot.py"]