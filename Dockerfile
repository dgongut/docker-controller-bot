FROM alpine:3.18.6

ENV TELEGRAM_TOKEN=abc
ENV TELEGRAM_ADMIN=abc
ENV TELEGRAM_GROUP=abc
ENV TELEGRAM_NOTIFICATION_CHANNEL=abc
ENV TELEGRAM_THREAD=1
ENV CHECK_UPDATES=1
ENV CHECK_UPDATE_EVERY_HOURS=4
ENV CHECK_UPDATE_STOPPED_CONTAINERS=1
ENV CONTAINER_NAME=abc
ENV BUTTON_COLUMNS=2
ENV LANGUAGE=ES
ENV EXTENDED_MESSAGES=0
ENV TZ=UTC

ARG VERSION=3.5.1

WORKDIR /app
RUN wget https://github.com/dgongut/docker-controller-bot/archive/refs/tags/v${VERSION}.tar.gz -P /tmp
RUN tar -xf /tmp/v${VERSION}.tar.gz
RUN mv docker-controller-bot-${VERSION}/* /app
RUN rm /tmp/v${VERSION}.tar.gz
RUN rm -rf docker-controller-bot-${VERSION}/
RUN apk add --no-cache python3 py3-pip tzdata
RUN pip3 install pyparsing==3.2.1
RUN pip3 install requests==2.32.3
RUN pip3 install pyTelegramBotAPI==4.26.0
RUN pip3 install docker==7.1.0
RUN pip install PyYAML==6.0.2
RUN pip install croniter==6.0.0

WORKDIR /app
COPY . .

ENTRYPOINT ["python3", "docker-controller-bot.py"]
