# Docker-Controller-Bot
[![](https://badgen.net/badge/icon/github?icon=github&label)](https://github.com/dgongut/docker-controller-bot)
[![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/docker-controller-bot)
[![](https://badgen.net/badge/icon/telegram?icon=telegram&label)](https://t.me/dockercontrollerbotnews)
[![Docker Pulls](https://badgen.net/docker/pulls/dgongut/docker-controller-bot?icon=docker&label=pulls)](https://hub.docker.com/r/dgongut/docker-controller-bot/)
[![Docker Stars](https://badgen.net/docker/stars/dgongut/docker-controller-bot?icon=docker&label=stars)](https://hub.docker.com/r/dgongut/docker-controller-bot/)
[![Docker Image Size](https://badgen.net/docker/size/dgongut/docker-controller-bot?icon=docker&label=image%20size)](https://hub.docker.com/r/dgongut/docker-controller-bot/)
![Github stars](https://badgen.net/github/stars/dgongut/docker-controller-bot?icon=github&label=stars)
![Github license](https://badgen.net/github/license/dgongut/docker-controller-bot)

> Lleva el control de tus contenedores Docker desde un único lugar: tu Telegram.

![Docker-Controller-Bot](https://raw.githubusercontent.com/dgongut/pictures/main/Docker-Controller-Bot/mockup.png)

📖 **Documentación completa** (hosts remotos paso a paso, NAS, TLS, FAQ) en [GitHub](https://github.com/dgongut/docker-controller-bot) · 🇬🇧 [README in English](https://github.com/dgongut/docker-controller-bot/blob/main/README_EN.md) · 📰 Novedades en el [canal de Telegram](https://t.me/dockercontrollerbotnews)

---

## 🚀 Empieza en 5 minutos

Solo hay dos pasos: crear tu bot y arrancarlo. Todo lo demás se configura después desde el propio chat.

### 1. Crea tu bot en Telegram

1. Abre [@BotFather](https://t.me/BotFather), envía `/newbot` y sigue las instrucciones. Te devolverá el **token** del bot: irá en `TELEGRAM_TOKEN`.
2. Para conocer tu **chat ID**, habla con [@MissRose_bot](https://t.me/MissRose_bot) y envíale `/id`. Ese número irá en `TELEGRAM_ADMIN`.
3. *(Opcional)* Si lo vas a usar en un grupo, añádelo, hazlo administrador y obtén el ID del grupo de la misma forma: irá en `TELEGRAM_GROUP`.
4. *(Opcional)* El icono oficial en alta resolución está [aquí](https://raw.githubusercontent.com/dgongut/pictures/main/Docker-Controller-Bot/Docker-Controller-Bot.png): envíaselo a BotFather con `/setuserpic`.

### 2. Arranca el contenedor

```yaml
services:
    docker-controller-bot:
        environment:
            - TELEGRAM_TOKEN=
            - TELEGRAM_ADMIN=
            - TZ=Europe/Madrid
            #- TELEGRAM_GROUP=
            #- TELEGRAM_THREAD=1
            #- TELEMETRY=false # Descomenta para desactivar las estadísticas anónimas
        volumes:
            - /var/run/docker.sock:/var/run/docker.sock # NO CAMBIAR
            - /ruta/para/guardar/la/configuracion:/app/config # CAMBIAR LA PARTE IZQUIERDA
            #- ~/.ssh:/root/.ssh:ro # Solo si vas a usar hosts remotos por ssh://
            #- ~/.docker/config.json:/root/.docker/config.json # Solo si necesitas docker login en algún registro
        image: dgongut/docker-controller-bot:latest
        container_name: docker-controller-bot
        restart: always
        network_mode: host
```

```bash
docker compose up -d
```

Abre Telegram, busca tu bot y envíale `/start`. Verás el menú principal con botones. Ya está: `/list` te muestra tus contenedores.

> ⚠️ **Mapea siempre un volumen en `/app/config`**: ahí se guardan los ajustes, las programaciones y la caché de actualizaciones. Sin él lo pierdes todo al recrear el contenedor.

### 🔄 ¿Vienes de la 4.x? No tienes que cambiar nada

- Si tu compose mapea `/app/schedule` (la ruta de la 4.x), el bot lo detecta y sigue usándolo. Cuando quieras, cambia esa línea a `/app/config` con la **misma parte izquierda**: no hace falta mover ningún fichero.
- En el primer arranque, tus variables (`LANGUAGE`, `CHECK_UPDATES`, `EXTENDED_MESSAGES`…) se importan a `/settings`, y desde entonces se cambian ahí. Puedes borrarlas del compose: solo hacen falta las de Telegram y `TZ`.
- **`CONTAINER_NAME` y `tty: true` ya no hacen falta.** El bot averigua solo cuál es su contenedor, y los logs salen al momento sin TTY.
- El mensaje de arranque te dice qué conviene cambiar en tu compose, si es que hay algo.

---

## ✨ ¿Qué puede hacer?

- 📦 **Contenedores:** listar, arrancar, parar, reiniciar y eliminar, de uno en uno o varios seguidos.
- 🧩 **Docker Compose:** detecta tus proyectos y los agrupa (proyecto → contenedores). Puedes actuar sobre un servicio o sobre el proyecto entero, respetando el orden de dependencias.
- 🖥️ **Varios hosts:** gestiona todas tus máquinas desde un solo bot, por `ssh://` o `tcp://`. Con un solo host todo se ve exactamente igual.
- 🔄 **Actualizaciones:** te avisa cuando hay imagen nueva y te dice **de qué versión a qué versión** (`1.43.3 → 1.43.4`), con aviso si es una versión mayor y enlace a sus novedades. Actualiza uno, varios o todos, con un mensaje de progreso y un resumen final. Conserva toda la configuración del contenedor y, si el nuevo no aguanta en marcha, vuelve al original.
- 🏷️ **Cambio de tag:** `/changetag` para hacer rollback o saltar de versión, con Docker Hub, ghcr.io, quay.io y registros privados.
- 🔔 **Avisos:** cuando un contenedor se para te dice por qué (terminó, falló con su código y sus últimas líneas de log, o se quedó sin memoria), cuando su healthcheck falla y cuando se recupera, y agrupa los bucles de reinicio en un solo aviso.
- 📊 **Diagnóstico:** logs en el chat o como fichero, `exec` dentro del contenedor, puertos usados y libres, y un `/info` con todo lo que el bot sabe de un contenedor.
- ⏰ **Automatización:** tareas programadas con cron (`run`, `stop`, `restart`, `exec`, `prune`, `mute`), silencio temporal y limpieza del sistema.
- ⚙️ **Ajustes desde el chat:** idioma, avisos, actualizaciones, hosts… todo con `/settings`, sin tocar el compose ni reiniciar.
- 🌍 **8 idiomas:** español, inglés, neerlandés, alemán, ruso, gallego, italiano y catalán.
- 🧱 **Multiarquitectura:** amd64, arm64, armv7, ppc64le y s390x. Funciona en Raspberry Pi, NAS y servidores.

---

## 📋 Comandos

Casi todos funcionan de dos formas: escribe el comando solo (`/run`) y el bot te muestra un menú con botones, o pásale el nombre (`/run nginx`) para actuar directamente. `/start` abre el menú principal con todo agrupado por categorías.

| Comando | Descripción |
|---|---|
| `/start` | Menú principal con botones |
| `/list` | Listado de contenedores, agrupados por host si tienes varios |
| `/run` `/stop` `/restart` | Arranca / detiene / reinicia un contenedor o un proyecto Compose |
| `/delete` | Elimina un contenedor o un proyecto Compose |
| `/exec` | Ejecuta un comando dentro de un contenedor |
| `/logs` `/logfile` | Logs en mensaje o como fichero |
| `/checkupdate` | Comprueba si un contenedor tiene actualización |
| `/updateall` | Actualiza todos los contenedores, de todos los hosts |
| `/changetag` | Cambia el tag de la imagen (rollback o salto de versión) |
| `/compose` | Extrae el `docker-compose` de un contenedor o proyecto |
| `/info` | Información detallada de un contenedor o proyecto |
| `/ports` | Puertos usados, comprueba uno concreto o genera uno libre |
| `/prune` | Limpia contenedores, imágenes, redes o volúmenes sin usar |
| `/mute <minutos>` | Silencia las notificaciones durante X minutos |
| `/schedule` | Crea, edita y borra tareas programadas |
| `/settings` | Ajustes del bot y hosts de Docker |
| `/version` `/donate` `/donors` | Versión / donar / donantes |

---

## ⚙️ Configuración

En el compose solo van las variables que el bot necesita **antes** de poder leer sus ajustes: cómo llegar a Telegram y quién puede hablarle.

| Variable | Obligatoria | Valor |
|:---|:---:|:---|
| `TELEGRAM_TOKEN` | ✅ | Token del bot |
| `TELEGRAM_ADMIN` | ✅ | Chat ID del administrador. Admite varios separados por comas: `12345,54431` |
| `TZ` | ✅ | Zona horaria, por ejemplo `Europe/Madrid` |
| `TELEGRAM_GROUP` | ❌ | Chat ID del grupo, si el bot va a estar en uno. Tiene que ser administrador del grupo |
| `TELEGRAM_THREAD` | ❌ | Tema dentro de un supergrupo (2, 3, 4…). Por defecto 1. Se usa junto a `TELEGRAM_GROUP` |
| `TELEMETRY` | ❌ | `false` para desactivar las estadísticas anónimas sin pasar por `/settings` |

Todo lo demás se cambia desde **`/settings`** y se aplica al momento: idioma, columnas de botones, mensajes ampliados, selección múltiple, comprobación de actualizaciones (si se hace, cada cuántas horas y si incluye los contenedores parados), canal de notificaciones, hosts de Docker y estadísticas.

**¿Necesitas `docker login`?** Mapea tu `~/.docker/config.json` en `/root/.docker/config.json` y el bot usará esas credenciales para descargar imágenes.

### 🏷️ Labels en tus otros contenedores

| Label | Efecto |
|:---|:---|
| `DCB-Auto-Update` | El contenedor se actualiza solo, sin preguntar |
| `DCB-Ignore-Check-Updates` | No se comprueban sus actualizaciones |

Con el valor `false` (`DCB-Auto-Update=false`) cuentan como desactivadas. Ejemplo:

```yaml
services:
  homeassistant:
    image: lscr.io/linuxserver/homeassistant:latest
    labels:
      - "DCB-Auto-Update"
```

El propio bot también se puede actualizar desde el chat, como cualquier otro contenedor, o solo con esa label.

---

## 🖥️ Varios hosts Docker

Los hosts se añaden desde el propio bot: `/settings` → **🖥️ Hosts de Docker** → **➕ Añadir host**, y le mandas la URL con el nombre que quieras delante (`nas ssh://usuario@nas`). El bot prueba la conexión antes de guardarla y, si falla, te dice por qué.

| Forma | Qué necesita | Cuándo |
|:---|:---|:---|
| `ssh://usuario@maquina` | Solo el `sshd` de siempre en la máquina remota | **La recomendada** |
| `tcp://maquina:2375` | Un socket proxy en la máquina remota | Si tus máquinas ya están en una red privada (Tailscale, WireGuard, una VLAN) |

**Con `ssh://`, en la máquina donde corre el bot (no dentro del contenedor):**

```bash
# Una sola vez: una clave sin passphrase, que vale para todos tus servidores
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519

# Por cada servidor: autorizar la clave y comprobar que responde
ssh-copy-id -i ~/.ssh/id_ed25519.pub usuario@maquina
ssh usuario@maquina docker version
```

Si el último comando te devuelve las versiones de cliente y servidor, el bot va a poder conectarse. Después mapea `~/.ssh:/root/.ssh:ro` en el compose del bot y añade el host desde `/settings`. Se respeta tu `~/.ssh/config`, así que los alias, puertos y claves que tengas ahí funcionan tal cual.

> ⚠️ `tcp://` sin TLS **no tiene autenticación**: cualquiera que llegue a ese puerto controla el Docker de esa máquina. Úsalo solo en una red de confianza, nunca expuesto a internet. Y no actualices desde el bot el socket proxy por el que llega a un host: se quedaría sin conexión a mitad (márcalo con `DCB-Ignore-Check-Updates`).

Un host caído se marca en 🔴 y no frena a los demás, y puedes **pausar** el que sepas que va a estar apagado. El paso a paso completo, con Synology, UnRAID y TLS, está en [GitHub](https://github.com/dgongut/docker-controller-bot).

---

## 📊 Estadísticas anónimas

Hasta la 5.0.0 no tenía ni idea de cuánta gente usa el bot ni de qué funciones le sirven. Cada mejora era una apuesta: ¿merece la pena pulir `/schedule`? ¿Cuántos tenéis más de un servidor? ¿Hay alguna arquitectura para la que se compila la imagen y no usa nadie?

Ahora el bot me lo cuenta con **unas pocas cifras anónimas una vez al día**, y con ellas sé a qué dedicar mi tiempo libre. Dejarlas activadas es la forma más sencilla de ayudar al proyecto sin hacer nada.

- **Qué se envía:** cuántos hosts y contenedores tienes (los contenedores por tramos, como «11-25»), qué ajustes están activados y en qué idioma, cuántas veces se usa cada comando, la arquitectura y las versiones del bot y de Docker.
- **Qué no se envía nunca:** nombres de contenedores, imágenes, hosts o proyectos, direcciones, IDs de Telegram ni nada de lo que escribes. Tu IP no se guarda.
- **No hay nada escondido:** las cifras son públicas, así que ves lo mismo que yo en [stats.dgongut.com](https://stats.dgongut.com/docker-controller-bot), con la [lista de lo que se envía, campo a campo](https://stats.dgongut.com/docker-controller-bot/privacy). El código del servidor está en [GitHub](https://github.com/dgongut/telemetry), y los envíos se borran a los 90 días.

Si aun así prefieres no enviarlas, se desactivan desde `/settings` → *Estadísticas anónimas* o con `TELEMETRY=false`. Pero si te gusta el bot, **déjalas activadas** 🙏

---

## ❓ Preguntas frecuentes

**¿Desde dónde puedo manejar el bot?** Desde tu chat privado con él y desde el grupo o tema configurado en `TELEGRAM_GROUP` y `TELEGRAM_THREAD`. Contesta siempre donde le escribes, y cualquier otro chat se ignora.

**Si configuro un canal de notificaciones, ¿se duplican los avisos?** No. Los cambios de estado de los contenedores (arranque, parada, caída y actualizaciones automáticas) van **solo** al canal. Los menús, los resultados de los comandos y los avisos de actualización con sus botones siguen en el chat.

**Un host aparece en 🔴, ¿qué hago?** Púlsalo en `/settings` → **🖥️ Hosts de Docker**: te dice el motivo exacto y puedes reintentar. Para diagnosticarlo, `ssh usuario@maquina docker version` desde la máquina del bot te da el mismo error con todo el detalle.

**Tengo varios hosts, ¿cómo sé dónde está cada contenedor?** `/list` los agrupa por máquina, cada aviso dice de qué host habla, y los menús te preguntan primero el host. Si dos máquinas tienen un contenedor con el mismo nombre, el bot te pregunta cuál.

**¿Hay algo que el bot no pueda actualizar?** Los contenedores lanzados con `--rm` y el que da red al propio bot (por ejemplo, el bot detrás de una VPN). En los dos casos te explica por qué.

---

## 🙏 Agradecimientos

Traducciones de [ManCaveMedia](https://github.com/ManCaveMedia) (neerlandés), [shedowe19](https://github.com/shedowe19) (alemán), [leyalton](https://github.com/leyalton) (ruso), [monfero](https://github.com/monfero) (gallego), [zichichi](https://github.com/zichichi) (italiano) y [flancky](https://t.me/flancky) (catalán). Pruebas del Docker Login de [garanda](https://github.com/garanda21) y README en inglés de [phampyk](https://github.com/phampyk).

Si el bot te resulta útil, puedes invitarme a un café con `/donate` ☕
