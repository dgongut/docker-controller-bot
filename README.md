# docker-controller-bot
[![](https://badgen.net/badge/icon/github?icon=github&label)](https://github.com/dgongut/docker-controller-bot)
[![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/docker-controller-bot)
[![Docker Pulls](https://badgen.net/docker/pulls/dgongut/docker-controller-bot?icon=docker&label=pulls)](https://hub.docker.com/r/dgongut/docker-controller-bot/)
[![Docker Stars](https://badgen.net/docker/stars/dgongut/docker-controller-bot?icon=docker&label=stars)](https://hub.docker.com/r/dgongut/docker-controller-bot/)
[![Docker Image Size](https://badgen.net/docker/size/dgongut/docker-controller-bot?icon=docker&label=image%20size)](https://hub.docker.com/r/dgongut/docker-controller-bot/)
![Github stars](https://badgen.net/github/stars/dgongut/docker-controller-bot?icon=github&label=stars)
![Github forks](https://badgen.net/github/forks/dgongut/docker-controller-bot?icon=github&label=forks)
![Github last-commit](https://img.shields.io/github/last-commit/dgongut/docker-controller-bot)
![Github last-commit](https://badgen.net/github/license/dgongut/docker-controller-bot)
![alt text](https://github.com/dgongut/pictures/blob/main/Docker-Controller-Bot/mockup.png)

Lleva el control de tus contenedores docker desde un único lugar.
 - ✅ Listar contenedores
 - ✅ Arrancar, parar y eliminar contenedores
 - ✅ Obtener los logs tanto de manera directa como a través de fichero
 - ✅ Extraer el docker-compose de tus contenedores
 - ✅ Notificaciones cuando un contenedor se cae o se inicia
 - ✅ Notificaciones cuando un contenedor tiene una actualización pendiente
 - ✅ Actualizaciones de los contenedores
 - ✅ Soporte de idiomas (Spanish, English, Dutch)

¿Lo buscas en [docker](https://hub.docker.com/r/dgongut/docker-controller-bot)?

## Configuración en config.py

| CLAVE  | OBLIGATORIO | VALOR |
|:------------- |:---------------:| :-------------|
|TELEGRAM_TOKEN |✅| Token del bot |
|TELEGRAM_ADMIN |✅| ChatId del administrador (se puede obtener hablándole al bot Rose escribiendo /id) |
|TELEGRAM_GROUP |❌| ChatId del grupo. Si este bot va a formar parte de un grupo, es necesario especificar el chatId de dicho grupo |
|TELEGRAM_THREAD |❌| Thread del tema dentro de un supergrupo; valor numérico (2,3,4..). Por defecto 1 |
|CONTAINER_NAME |✅| Nombre del contenedor, lo que se le ponga en container_name en el docker-compose ha de ir aquí también |
|CHECK_UPDATES |❌| Si se desea que compruebe actualizaciones. 0 no - 1 sí. Por defecto 1|
|CHECK_UPDATE_EVERY_HOURS |❌| Tiempo de espera en horas entre chequeo de actualizaciones (4 horas por defecto) | 
|BUTTON_COLUMNS |❌| Numero de columnas de botones en las listas de contenedores (2 columnas por defecto) | 
|LANGUAGE |❌| Idioma, puede ser ES / EN / NL. Por defecto es ES (Spanish) | 
|EXTENDED_MESSAGES |❌| Si se desea que muestre más mensajes de información. 0 no - 1 sí. Por defecto 0 | 

### Anotaciones
La función de extracción de docker-compose se encuentra en una fase temprana de desarrollo y puede contener errores.

### Ejemplo de Docker-Compose
```yaml
version: '3.3'
services:
    docker-controller-bot:
        environment:
            - TELEGRAM_TOKEN=
            - TELEGRAM_ADMIN=
            - CONTAINER_NAME=docker-controller-bot
            #- TELEGRAM_GROUP=
            #- TELEGRAM_THREAD=1
            #- CHECK_UPDATES=1
            #- CHECK_UPDATE_EVERY_HOURS=4
            #- BUTTON_COLUMNS=2
            #- LANGUAGE=ES
            #- EXTENDED_MESSAGES=0
        volumes:
            - /var/run/docker.sock:/var/run/docker.sock
        image: dgongut/docker-controller-bot:latest
        container_name: docker-controller-bot
        restart: always
        network_mode: host
        tty: true
```

### Agradecimientos
Traducción al neerlandés: [ManCaveMedia](https://github.com/ManCaveMedia)

## Como ejecutarlo en local
Para su ejecución en local se necesitan crear 2 ficheros llamados respectivamente Dockerfile_local y docker-compose.yaml

La estructura de carpetas debe quedar:
```
docker-controller-bot/
├── Dockerfile_local
├── docker-compose.yaml
└── src
    ├── LICENSE
    ├── README.md
    ├── config.py
    ├── docker-controller-bot.py
    └── locale
        ├── en.json
        ├── es.json
        └── nl.json
```

Dockerfile_local:
```
FROM alpine:3.18.6

ENV TELEGRAM_TOKEN abc
ENV TELEGRAM_ADMIN abc
ENV TELEGRAM_GROUP abc
ENV TELEGRAM_THREAD 1
ENV CHECK_UPDATES 1
ENV CHECK_UPDATE_EVERY_HOURS 4
ENV CONTAINER_NAME abc
ENV BUTTON_COLUMNS 2
ENV LANGUAGE ES
ENV EXTENDED_MESSAGES 0

WORKDIR /app
COPY src/ .

RUN apk add --no-cache python3 py3-pip
RUN pip3 install pyTelegramBotAPI
RUN pip3 install docker
RUN pip install PyYAML

ENTRYPOINT ["python3", "docker-controller-bot.py"]
```

docker-compose.yaml
```yaml
version: '3.3'
services:
    TEST-docker-controller-bot:
        container_name: TEST-docker-controller-bot
        environment:
            - TELEGRAM_TOKEN=
            - TELEGRAM_ADMIN=
            - CONTAINER_NAME=TEST-docker-controller-bot
            #- TELEGRAM_GROUP=
            #- TELEGRAM_THREAD=1
            #- CHECK_UPDATES=1
            #- CHECK_UPDATE_EVERY_HOURS=4
            #- BUTTON_COLUMNS=2
            #- LANGUAGE=ES
            #- EXTENDED_MESSAGES=0
        volumes:
            - /var/run/docker.sock:/var/run/docker.sock:ro
        build:
          context: .
          dockerfile: ./Dockerfile_local
        tty: true
```

Es necesario establecer un `TELEGRAM_TOKEN` y un `TELEGRAM_ADMIN` correctos y diferentes al de la ejecución normal.

Para levantarlo habría que ejecutar en esa ruta: `docker compose up -d`
Para detenerlo y probar nuevos cambios habría que ejecutar en esa ruta: `docker compose down --rmi`