# Docker-Controller-Bot
[![](https://badgen.net/badge/icon/github?icon=github&label)](https://github.com/dgongut/docker-controller-bot)
[![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/docker-controller-bot)
[![](https://badgen.net/badge/icon/telegram?icon=telegram&label)](https://t.me/dockercontrollerbotnews)
[![Docker Pulls](https://badgen.net/docker/pulls/dgongut/docker-controller-bot?icon=docker&label=pulls)](https://hub.docker.com/r/dgongut/docker-controller-bot/)
[![Docker Stars](https://badgen.net/docker/stars/dgongut/docker-controller-bot?icon=docker&label=stars)](https://hub.docker.com/r/dgongut/docker-controller-bot/)
[![Docker Image Size](https://badgen.net/docker/size/dgongut/docker-controller-bot?icon=docker&label=image%20size)](https://hub.docker.com/r/dgongut/docker-controller-bot/)
![Github stars](https://badgen.net/github/stars/dgongut/docker-controller-bot?icon=github&label=stars)
![Github forks](https://badgen.net/github/forks/dgongut/docker-controller-bot?icon=github&label=forks)
![Github last-commit](https://img.shields.io/github/last-commit/dgongut/docker-controller-bot)
![Github last-commit](https://badgen.net/github/license/dgongut/docker-controller-bot)

<h3 align="center">
  ReadMe en Español
  <span> | </span>
  <a href="./README_EN.md">ReadMe in English</a>
  <span> | </span>
  <a href="https://t.me/dockercontrollerbotnews">Canal de Noticias en Telegram</a>
</h3>

> Lleva el control de tus contenedores Docker desde un único lugar: tu Telegram.

![Docker-Controller-Bot](https://github.com/dgongut/pictures/blob/main/Docker-Controller-Bot/mockup.png)

¿Lo buscas en [![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/docker-controller-bot)?

**NUEVO** Canal de novedades en [![](https://badgen.net/badge/icon/telegram?icon=telegram&label)](https://t.me/dockercontrollerbotnews)

## 🚀 Empieza en 5 minutos

Solo hay dos pasos: crear tu bot y arrancarlo. Todo lo demás se configura después desde el propio chat.

### 1. Crea tu bot en Telegram (2 min)

1. Abre [@BotFather](https://t.me/BotFather) en Telegram y envía `/newbot`. Sigue las instrucciones (un nombre y un username acabado en `bot`).
2. BotFather te devolverá el token del bot. Guárdalo: irá en la variable `TELEGRAM_TOKEN`.
3. Para conocer tu propio chat ID (lo necesitas para `TELEGRAM_ADMIN`), habla con [@MissRose_bot](https://t.me/MissRose_bot) y envíale `/id`. Te responderá con un número, ese es tu ID.
4. *(Opcional)* Si vas a usar el bot dentro de un grupo, añádelo, hazlo administrador y obtén el chat ID del grupo de la misma forma; ese valor irá en `TELEGRAM_GROUP`.
5. *(Opcional)* Si quieres ponerle el icono oficial al bot, descarga la imagen en alta resolución [aquí](https://raw.githubusercontent.com/dgongut/pictures/main/Docker-Controller-Bot/Docker-Controller-Bot.png) y envíasela a [@BotFather](https://t.me/BotFather) usando la opción `/setuserpic`.

### 2. Arranca el contenedor (2 min)

Copia este `docker-compose.yml`, rellena las 3 variables y levántalo:

```yaml
services:
    docker-controller-bot:
        environment:
            - TELEGRAM_TOKEN=
            - TELEGRAM_ADMIN=
            - TZ=Europe/Madrid
            #- TELEGRAM_GROUP=
            #- TELEGRAM_THREAD=1
        volumes:
            - /var/run/docker.sock:/var/run/docker.sock # NO CAMBIAR
            - /ruta/para/guardar/la/configuracion:/app/config # CAMBIAR LA PARTE IZQUIERDA
            #- ~/.ssh:/root/.ssh:ro # Solo si vas a usar hosts remotos por ssh://
            #- ~/.docker/config.json:/root/.docker/config.json # Solo si se requiere iniciar sesión en algún registro
        image: dgongut/docker-controller-bot:latest
        container_name: docker-controller-bot
        restart: always
        network_mode: host
        tty: true
```

```bash
docker compose up -d
```

Abre Telegram, busca tu bot y envíale `/start`. Verás el menú principal con botones. Ya está: `/list` te muestra tus contenedores.

> [!WARNING]
> Es obligatorio mapear un volumen en `/app/config`: ahí se guardan los ajustes, las programaciones y la caché de actualizaciones. Sin ese volumen lo pierdes todo al recrear el contenedor.

<details>
<summary>🔄 ¿Vienes de la 4.x? No tienes que cambiar nada</summary>

- Si tu docker-compose mapea `/app/schedule` (la ruta que se usaba hasta la 4.x) el bot lo detecta y sigue usándolo, así que **actualizar no requiere cambiar nada** y no perderás ni un ajuste.
- Cuando quieras pasarlo a `/app/config`, es cambiar una letra de la línea del volumen:

  ```diff
  -  - /tu/ruta:/app/schedule
  +  - /tu/ruta:/app/config
  ```

  **La misma parte izquierda, y no se mueve ningún fichero**: los ficheros ya están en tu volumen y se llaman igual. Después, `docker compose up -d` para recrear el contenedor. Mientras no lo hagas, el bot te lo recuerda en su mensaje de arranque.
- **`CONTAINER_NAME` ya no hace falta.** Hasta la 4.x había que decirle al bot cómo se llamaba su propio contenedor, para que supiera no pararse ni eliminarse a sí mismo. Ahora lo averigua solo: lee su id en `/proc/self/mountinfo` y le pregunta a Docker cuál es. Si la tienes puesta el bot arranca igual y te avisa en el log de que puedes quitarla.
- En el primer arranque el bot importa los valores de tus variables de entorno a `settings.json` y los conserva tal cual. A partir de ahí manda `settings.json`, y esas variables ya no se leen; el log te avisa de las que puedes borrar del docker-compose.

</details>

<details>
<summary>🔐 ¿Necesitas <code>docker login</code> (DockerHub, GHCR, registro privado)?</summary>

Si se requiere tener la sesión iniciada en algún registro, traslada ese login al contenedor mapeando tu `~/.docker/config.json` a `/root/.docker/config.json`:

```yaml
volumes:
    - ~/.docker/config.json:/root/.docker/config.json
```

</details>

## ✨ ¿Qué puede hacer?

- 📦 **Contenedores:** listar, arrancar, parar, reiniciar y eliminar. Con selección múltiple en `/run`, `/stop` y `/restart`.
- 🧩 **Compose:** detecta tus proyectos y los agrupa (proyecto → contenedores).
- 📊 **Diagnóstico:** logs en el chat o como fichero, `exec` dentro del contenedor, puertos e info detallada.
- 🔄 **Actualizaciones:** te avisa si hay imagen nueva, actualiza uno o todos, cambia de tag (rollback) y auto-actualiza con labels.
- ⏰ **Automatización:** tareas programadas con cron + silencio temporal con `/mute` + limpieza con `/prune`.
- 🖥️ **Multi-host y ajustes:** varios servidores desde un solo bot y todo configurable con `/settings`, sin reiniciar.

<details>
<summary>📋 Ver lista completa de funciones</summary>

- ✅ Listar contenedores
- ✅ Arrancar, parar y eliminar contenedores
- ✅ Selección múltiple en `/run`, `/stop` y `/restart`: el menú se queda abierto y se va actualizando para encadenar varios contenedores
- ✅ Soporte para proyectos Docker Compose con navegación jerárquica (proyecto → contenedores)
- ✅ Obtener los logs tanto de manera directa como a través de fichero
- ✅ Extraer el docker-compose de tus contenedores
- ✅ Notificaciones cuando un contenedor se cae o se inicia
- ✅ Notificaciones cuando un contenedor tiene una actualización pendiente
- ✅ Actualizaciones de los contenedores
- ✅ Cambiar el tag (rollback o actualización)
- ✅ Limpia el sistema, eliminando contenedores, imágenes y otros objetos no utilizados
- ✅ Ejecuta comandos dentro de contenedores
- ✅ Visualiza puertos usados por contenedores, comprueba si un puerto concreto está libre y genera puertos aleatorios disponibles
- ✅ Muestra información detallada de un contenedor o de un proyecto Compose completo
- ✅ Programación de tareas con expresiones cron: run, stop, restart, exec, prune y mute
- ✅ Varios hosts Docker desde un solo bot, por `ssh://`, `tcp://` o TLS: se añaden desde el propio chat, y con un único host todo se ve exactamente igual que siempre
- ✅ Se identifica a sí mismo sin ayuda: no hay que decirle cómo se llama su contenedor para que sepa no pararse ni eliminarse
- ✅ Ajustes editables desde el propio bot con `/settings`, sin tocar el docker-compose ni reiniciar
- ✅ Menú principal por botones en `/start`, agrupado en categorías; los comandos escritos siguen funcionando igual
- ✅ Silencia las notificaciones de forma temporal
- ✅ Imagen multiarquitectura (amd64, arm64, armv7…) compatible con Raspberry Pi, NAS y servidores estándar
- ✅ Soporte de idiomas (Spanish, English, Dutch, German, Russian, Galician, Italian, Catalan)

</details>

## 📋 Comandos

Casi todos funcionan de dos formas: escribe el comando solo (`/run`) y el bot te muestra un menú con botones, o pásale el nombre directamente (`/run nginx`) para actuar sin menús.

`/start` abre el menú principal, con todo agrupado por categorías (Contenedores, Diagnóstico, Actualizaciones, Sistema, Automatización, Ajustes y Acerca de). Pulsar un botón hace exactamente lo mismo que escribir el comando a secas.

| Comando | Descripción |
|---|---|
| `/start` | Menú principal con botones, agrupados por tipo de acción |
| `/list` | Listado completo de contenedores, agrupados por host si tienes varios |
| `/run` `/stop` `/restart` | Arranca / detiene / reinicia un contenedor o un proyecto Compose entero |
| `/delete` | Elimina un contenedor o un proyecto Compose entero |
| `/exec` | Ejecuta un comando dentro de un contenedor |
| `/logs` `/logfile` | Logs en mensaje o como fichero |
| `/checkupdate` | Comprueba si un contenedor tiene actualización |
| `/updateall` | Actualiza todos los contenedores, de todos los hosts a la vez |
| `/changetag` | Cambia el tag de la imagen (rollback o salto de versión) |
| `/compose` | Extrae el `docker-compose` de un contenedor o proyecto |
| `/info` | Muestra información detallada de un contenedor o de un proyecto |
| `/ports` | Lista puertos usados, comprueba uno concreto o genera uno libre |
| `/prune` | Limpia contenedores, imágenes, redes o volúmenes no usados |
| `/mute <minutos>` | Silencia las notificaciones durante X minutos |
| `/schedule` | Menú para crear, editar y borrar tareas programadas |
| `/settings` | Ajustes del bot: idioma, columnas, comprobación de actualizaciones, canal de notificaciones y **hosts de Docker** |
| `/version` `/donate` `/donors` | Versión actual / donar / lista de donantes |

<details>
<summary>🧩 Mis contenedores son de <code>docker compose</code>, ¿cómo se ven?</summary>

Si tus contenedores fueron creados con `docker compose`, el bot los reconoce automáticamente como un **proyecto** y los presenta agrupados.

En comandos como `/run`, `/stop`, `/restart`, `/delete`, `/info` o `/compose` verás primero la lista de proyectos y, al pulsar uno, sus contenedores. En `/run` y `/stop` solo se ofrecen los servicios sobre los que la acción tiene sentido (parados y arrancados respectivamente), igual que ya indicaba el contador del botón del proyecto. Las acciones de inicio, parada, reinicio y borrado se pueden aplicar al **proyecto entero** o a un contenedor individual.

</details>

<details>
<summary>⏰ Automatizar tareas con <code>/schedule</code></summary>

Desde `/schedule` puedes crear tareas que se ejecuten en cron.

- Acciones soportadas: `run`, `stop`, `restart`, `exec`, `prune` y `mute`.
- Acepta expresiones cron estándar (`0 */4 * * *`) y atajos: `@yearly`, `@monthly`, `@weekly`, `@daily`, `@hourly` y `@reboot`.
- Si tienes varios hosts, el bot te pregunta en cuál se ejecuta la tarea, y el host aparece en el resumen y en el listado. `mute` es la excepción: silencia las notificaciones del propio bot, así que no pertenece a ninguna máquina.
- Las programaciones se persisten en `/app/config` (recuerda mapear ese volumen).

</details>

<details>
<summary>⚙️ Configuración avanzada y <code>/settings</code></summary>

Aquí solo quedan las variables que el bot necesita **antes** de poder leer sus propios ajustes: cómo llegar a Telegram y quién puede hablarle. La regla es sencilla: si ponerla mal te puede dejar sin acceso al bot, va en el docker-compose, porque lo que impide que el chat funcione no se puede arreglar desde el chat.

| CLAVE  | OBLIGATORIO | VALOR |
|:------------- |:---------------:| :-------------|
|TELEGRAM_TOKEN |✅| Token del bot |
|TELEGRAM_ADMIN |✅| ChatId del administrador (se puede obtener hablándole al bot [Rose](https://t.me/MissRose_bot) escribiendo /id). Admite múltiples administradores separados por comas. Por ejemplo 12345,54431,55944 |
|TELEGRAM_GROUP |❌| ChatId del grupo. Si este bot va a formar parte de un grupo, es necesario especificar el chatId de dicho grupo. Es necesario que el bot sea administrador del grupo |
|TELEGRAM_THREAD |❌| Thread del tema dentro de un supergrupo; valor numérico (2,3,4..). Por defecto 1. Se utiliza en conjunción con la variable TELEGRAM_GROUP |
|TZ |✅| Timezone (Por ejemplo Europe/Madrid) |

Todo lo demás se configura desde el propio bot y se guarda en `settings.json`, dentro del volumen mapeado:

| AJUSTE | VALOR |
|:------------- | :-------------|
|Idioma| ES / EN / NL / DE / RU / GL / IT / CAT. Por defecto ES |
|Columnas de botones| Número de columnas en las listas de contenedores. Por defecto 2 |
|Mensajes ampliados| Muestra más mensajes de información. Por defecto desactivado |
|Selección múltiple| En `/run`, `/stop` y `/restart` el menú se queda abierto para actuar sobre varios contenedores seguidos. Por defecto activado |
|Comprobar actualizaciones| Por defecto activado |
|Intervalo de comprobación| Horas entre comprobaciones. Acepta decimales. Por defecto 4 |
|Contenedores parados| Si comprueba también las actualizaciones de los contenedores detenidos. Por defecto activado |
|Canal de notificaciones| Canal donde se publicarán exclusivamente los cambios de estado de los contenedores (arranque, parada, creación y actualizaciones automáticas). La gestión se sigue haciendo desde el chat privado con el bot o desde TELEGRAM_GROUP. El bot comprueba que puede publicar ahí antes de guardarlo |
|Hosts de Docker| Las máquinas que gestiona el bot. Se añaden, se prueban, se renombran y se quitan desde aquí. Ver desplegable de hosts remotos |

Los cambios hechos desde `/settings` se aplican al momento, sin reiniciar el contenedor. Si prefieres editar `settings.json` a mano, hará falta reiniciar para que los lea.

</details>

<details>
<summary>🖥️ ¿Tienes más de un servidor? Gestiona varios hosts Docker</summary>

A partir de la 5.0.0 el bot puede gestionar varios hosts Docker. Se definen en los ajustes, no en variables de entorno, así que se añaden desde el propio bot sin tocar el docker-compose.

Con **un solo host** —el caso normal— nada de esto aparece: el bot se ve exactamente igual que en la 4.x.

### Cómo conectar con un host remoto

Hay dos formas, y para la mayoría de la gente la primera es la respuesta:

| Forma | Qué necesita | Cuándo |
|:---|:---|:---|
| `ssh://usuario@maquina` | Nada en el host remoto salvo su `sshd` de siempre | **La recomendada.** Si ya entras por ssh a esa máquina, ya está medio hecho |
| `tcp://maquina:2375` | Un socket proxy en la máquina remota | Si tus máquinas ya están en una red privada (Tailscale, WireGuard, una VLAN aislada) |

> ⚠️ **Aviso:** `tcp://` sin TLS **no tiene autenticación de ningún tipo**: cualquiera que alcance ese puerto controla el Docker de esa máquina por completo, con permisos equivalentes a root. Solo dentro de una red en la que confíes, nunca expuesto a internet.

> ℹ️ **Nota:** Docker también admite `tcp://` cifrado con TLS. Es bastante más trabajo y con `ssh://` disponible no le veo la necesidad, pero si tu caso lo pide está explicado en la [FAQ](#-preguntas-frecuentes-faq), en «*Quiero usar `tcp://` pero cifrado con TLS*».

### La prueba que lo decide

Antes de configurar nada en el bot, comprueba desde una terminal **de la máquina donde corre el bot**:

```bash
ssh usuario@maquina docker version
```

Si eso te devuelve las versiones de cliente y servidor, `ssh://usuario@maquina` va a funcionar. Si falla, el mensaje te dice exactamente qué arreglar y no hace falta tocar el bot para nada.

El motivo es que el bot no habla un protocolo propio: abre una sesión ssh y ejecuta `docker system dial-stdio` en la máquina remota. Así que lo único que hace falta allí es que el usuario tenga el binario `docker` en su PATH y permiso sobre el socket.

| Lo que ves | Qué falta |
|:---|:---|
| `Permission denied (publickey)` | La clave no está autorizada. Repite el `ssh-copy-id` |
| `command not found: docker` | El `docker` no está en el PATH de ese usuario |
| `permission denied ... /var/run/docker.sock` | El usuario no está en el grupo `docker` |
| `Host key verification failed` | Falta la máquina en `known_hosts` |

<details>
<summary>🔑 Paso a paso con <code>ssh://</code> (recomendado)</summary>

> ❗ **Importante — una sola clave vale para todos tus servidores.** No generes una por máquina: creas la clave una vez y la autorizas en cada máquina que quieras añadir. Abajo cada paso lleva marcado si es *una vez* o *por cada máquina*, para que no lo repitas de más.

**1. Habilita SSH en la máquina remota.** *(por cada máquina)* En un Linux normal ya está. En un NAS suele ser un interruptor en su panel (ver más abajo).

**2. Genera una clave sin passphrase** *(una vez, y ya no se vuelve a tocar)*, en la máquina donde corre el bot. Sin passphrase porque no va a haber nadie para teclearla:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
```

**3. Autorízala en la máquina remota.** *(por cada máquina)* Este es el único paso que se repite de verdad:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub usuario@maquina
```

Para el segundo servidor, y el tercero, es esta misma orden apuntando a la máquina nueva:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@unraid
ssh-copy-id -i ~/.ssh/id_ed25519.pub admin@synology
```

**4. Conéctate una vez a mano** *(por cada máquina)*, desde la máquina donde corre el bot, para que la huella de la remota quede en tu `known_hosts`. Aprovecha para hacer la prueba de arriba:

```bash
ssh usuario@maquina docker version
```

> ❗ **Importante:** los pasos 2, 3 y 4 se hacen **en la máquina del bot, no dentro del contenedor**. El bot no puede aceptar una huella nueva por ti: no hay terminal donde responder «yes», y el `.ssh` se mapea en solo lectura, así que no puede escribir en `known_hosts`. Si la máquina remota no está ya en tu `known_hosts`, el bot dirá que no responde con un `Host key verification failed`.

**5. Da acceso al socket al usuario remoto** *(por cada máquina)*, si el paso 4 se quejó de permisos:

```bash
sudo usermod -aG docker usuario   # y vuelve a entrar por ssh
```

**6. Mapea tu `.ssh` al contenedor** *(una vez)* y reinicia el bot:

```yaml
volumes:
    - /var/run/docker.sock:/var/run/docker.sock # NO CAMBIAR
    - /ruta/para/guardar/la/configuracion:/app/config
    - ~/.ssh:/root/.ssh:ro # Solo si vas a usar hosts ssh://
```

El bot corre como `root` dentro del contenedor, así que lee `/root/.ssh`. Tiene que ser el `.ssh` del usuario con el que hiciste el `ssh-copy-id`: ahí están la clave privada y el `known_hosts` que el paso 4 rellenó. En solo lectura a propósito, porque el bot no necesita escribir nada ahí.

La conexión la abre el cliente `ssh` del sistema, no una implementación propia, así que se respeta tu `~/.ssh/config` entero: alias de host, puertos, `IdentityFile`, `User`. Si en tu config tienes un `Host nas`, la URL puede ser simplemente `ssh://nas`.

**7. Añade el host en el bot.** Ya no hace falta tocar el docker-compose otra vez: abre `/settings`, entra en **🖥️ Hosts de Docker**, pulsa **➕ Añadir host** y mándale el host en un solo mensaje, con el nombre que quieras darle delante:

```
nas ssh://usuario@nas
```

El nombre es opcional; si no lo pones, el bot lo saca de la URL. Antes de guardarlo prueba la conexión, así que si algo falla te lo dice en el momento y no lo guarda:

| Lo que responde el bot | Qué pasa |
|:---|:---|
| `✅ Host nas añadido` | Ya está. Sus contenedores aparecen en `/list` desde ese instante |
| `❌ No he podido conectar` | Trae el motivo debajo. Es el mismo que te daría la prueba del paso 4 |
| `❌ No sé conectarme a...` | La URL no empieza por `ssh://`, `tcp://` o `unix://` |
| `❌ ya está registrado` | Esa máquina ya está añadida; añadirla dos veces duplicaría sus contenedores |

Después, pulsando el host en esa misma pantalla puedes **🔄 Probar de nuevo**, **✏️ Renombrar** o **🗑️ Quitar host**. El 🟢 y el 🔴 de la lista te dicen de un vistazo cuál responde.

</details>

<details>
<summary>🗂️ Tengo varios servidores, ¿repito todo?</summary>

No: repites el **paso 3** apuntando a cada máquina y listo. Una sola clave autoriza en todas las que quieras, y es lo que yo haría en una red doméstica: menos ficheros que gestionar y menos sitios donde equivocarse.

Comprueba cada una antes de añadirla al bot:

```bash
ssh root@unraid docker version
ssh admin@synology docker version
```

<details>
<summary>Prefiero una clave distinta por servidor</summary>

Merece la pena si quieres que una clave comprometida no abra todas tus máquinas, o si alguna es de otra persona y prefieres poder revocarla por separado. Generas una por máquina:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_unraid   -C "dcb-unraid"
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_synology -C "dcb-synology"

ssh-copy-id -i ~/.ssh/id_unraid.pub   root@unraid
ssh-copy-id -i ~/.ssh/id_synology.pub admin@synology
```

Y le dices a `ssh` cuál usar con cada una, en `~/.ssh/config`:

```
Host unraid
    HostName unraid.lan
    User root
    IdentityFile ~/.ssh/id_unraid

Host synology
    HostName 192.168.1.20
    User admin
    Port 2222
    IdentityFile ~/.ssh/id_synology
```

Eso tiene dos ventajas más allá de las claves. La primera es que las URLs se quedan en **`ssh://unraid`** y **`ssh://synology`** a secas: el usuario, el puerto y la clave los pone la config. La segunda es que `ssh unraid docker version` sigue siendo la prueba, así que lo que verificas en la terminal es exactamente lo que va a hacer el bot.

</details>

> ℹ️ **Nota:** el fichero `~/.ssh/config` también se mapea al contenedor, porque ya estás mapeando el directorio entero. No hace falta añadir nada al docker-compose.

</details>

<details>
<summary>🔌 Prefiero <code>tcp://</code> con socket proxy</summary>

Si tus máquinas ya están en una red privada, esta es la vía más corta: en lugar de exponer el socket entero, se expone un proxy que solo deja pasar lo que le indiques. En la **máquina remota**:

```yaml
services:
    docker-socket-proxy:
        image: tecnativa/docker-socket-proxy
        container_name: docker-socket-proxy
        environment:
            - CONTAINERS=1
            - IMAGES=1
            - NETWORKS=1
            - VOLUMES=1
            - POST=1 # necesario para arrancar, parar y actualizar
        volumes:
            - /var/run/docker.sock:/var/run/docker.sock:ro
        ports:
            - 2375:2375
        labels:
            - "DCB-Ignore-Check-Updates" # El bot llega a esta máquina A TRAVÉS del proxy no debe actualizarlo
        restart: unless-stopped
```

> ⚠️ **Aviso:** no actualices nunca este proxy desde el bot. El update pararía el proxy a mitad de la operación y se quedaría sin conexión para terminarla ni para deshacerla: el host quedaría en 🔴 y habría que rearrancarlo a mano por SSH (`docker start docker-socket-proxy`). Actualízalo siempre desde la propia máquina remota.

La URL para el bot es entonces `tcp://maquina:2375`. Compruébalo antes desde la máquina del bot:

```bash
docker -H tcp://maquina:2375 version
```

Si responde, añádelo en el bot igual que uno por ssh —`/settings` → **🖥️ Hosts de Docker** → **➕ Añadir host**— mandándole `nas tcp://maquina:2375`.

> ⚠️ **Aviso:** esto **no lleva TLS ni autenticación**: el proxy acota qué se puede hacer, no quién puede hacerlo. Publica el puerto solo en una red en la que confíes —Tailscale, WireGuard, una VLAN aislada—, nunca en internet. Si necesitas exponerlo fuera, usa TLS (ver FAQ).

</details>

<details>
<summary>💾 Synology, UnRAID y otros NAS</summary>

**Ninguno expone el puerto de Docker por defecto**, y es lo correcto: hacerlo sin TLS sería dejar la máquina abierta. Así que la vía es `ssh://` en los dos casos.

**UnRAID** suele ser el fácil: el SSH viene habilitado, se entra como `root` y el `docker` está en el PATH. Normalmente basta con:

```bash
ssh root@unraid docker version
```

Si eso responde, tu URL es `ssh://root@unraid`.

**Synology** es el incómodo, y cuánto depende de tu versión de DSM:

1. Habilita SSH en *Panel de control → Terminal y SNMP → Activar servicio SSH*.
2. Prueba `ssh tu_usuario@synology docker version`.

Los dos tropiezos habituales son que el `docker` de Container Manager no está en el PATH de una sesión ssh, y que el socket es de `root` y tu usuario administrador no lo alcanza. En DSM moderno el login directo como root está deshabilitado, así que si te topas con eso la salida práctica es un contenedor **socket proxy** en el propio Synology, que además te deja limitar qué puede hacer el bot.

En mi caso no conseguí dejarlo fino por SSH en Synology, así que opté por levantar el **docker-socket-proxy** en el propio Synology y conectarlo con `tcp://...:2375` sin TLS, dentro de la red local de confianza (ver desplegable de `tcp://` de arriba). Es la opción que me funciona en el día a día.

> 💡 **Consejo:** si tienes tus máquinas en una red privada tipo Tailscale o WireGuard, `tcp://` a secas sobre esa red te evita tanto los certificados como el ssh, porque el cifrado y la autenticación los pone la malla. Es lo más cómodo cuando ya la tienes montada.

</details>

### Las credenciales nunca se guardan en los ajustes

En `settings.json` solo va la URL y, para TLS, las rutas de los certificados. Ni claves ni contraseñas: el material sensible son ficheros que tú mapeas, y así ningún mensaje de Telegram acaba llevándolo dentro.

</details>

<details>
<summary>🏷️ Funciones extra con labels en otros contenedores</summary>

- Añadiendo la etiqueta `DCB-Ignore-Check-Updates` a un contenedor, no se comprobarán actualizaciones para él.
- Añadiendo la etiqueta `DCB-Auto-Update` a un contenedor, se actualizará automáticamente sin preguntar.

Ver ejemplo completo en la FAQ: *«He visto que se pueden añadir labels…»*.

</details>

## Agradecimientos

- Traducción al neerlandés: [ManCaveMedia](https://github.com/ManCaveMedia)
- Traducción al alemán: [shedowe19](https://github.com/shedowe19)
- Traducción al ruso: [leyalton](https://github.com/leyalton)
- Traducción al gallego: [monfero](https://github.com/monfero)
- Traducción al italiano: [zichichi](https://github.com/zichichi)
- Traducción al catalán: [flancky](https://t.me/flancky)
- Pruebas del Docker Login: [garanda](https://github.com/garanda21)
- Readme en inglés: [phampyk](https://github.com/phampyk)

## ❓ Preguntas Frecuentes (FAQ)

> Pulsa cada pregunta para ver la respuesta.

<details>
<summary>🧭 ¿Puede el programa decirme de qué versión a qué versión se actualizó una imagen?</summary>

**Respuesta corta:** No, eso no es posible de forma automática.

**Respuesta explicada:**

El programa no se basa en "versiones", sino en comprobar si una imagen Docker ha cambiado.  
Esto se hace comparando el **hash (identificador único)** de la imagen local con el hash remoto.

- En Docker, el **tag** (como `latest`, `v1.2`, etc.) es solo una etiqueta.
- Esa etiqueta **no siempre representa una versión real** del software dentro de la imagen.
- Algunos desarrolladores usan etiquetas que coinciden con la versión (como `v1.2.3`), pero no es obligatorio ni automático.
- Por ejemplo, el tag `latest` puede apuntar a una imagen completamente distinta en cualquier momento.

🔍 Por eso, aunque sepamos que una imagen cambió, **no podemos decir automáticamente "pasaste de la versión X a la Y"**.

**¿Por qué no se muestra el changelog o la lista de cambios?**

Mostrar un changelog requeriría:

- Saber de qué versión venías y a cuál fuiste (lo cual no es posible automáticamente).
- Que el desarrollador del contenedor publique esa información en un lugar conocido (como GitHub o Docker Hub).
- Que haya una forma estándar de obtenerlo, cosa que no siempre ocurre.

📦 Cada contenedor es diferente, y no todos publican cambios de forma clara o accesible.

**Entonces, ¿cómo puedo saber qué cambió?**

Puedes hacerlo manualmente:

1. El programa puede mostrarte el **hash anterior** y el **nuevo hash** de la imagen.
2. Con esos datos, puedes ir al repositorio del contenedor (GitHub, Docker Hub, etc.).
3. Busca allí el historial de versiones, el changelog o las notas de lanzamiento si están disponibles.

</details>

<details>
<summary>🛠️ He visto que se pueden añadir labels para controlar ciertas cosas de los contenedores, ¿cómo lo hago?</summary>

Efectivamente, actualmente hay dos etiquetas (*labels*) que puedes añadir a los contenedores para controlarlos:  
- `DCB-Ignore-Check-Updates`  
- `DCB-Auto-Update`

Para añadirlas a un contenedor, basta con editar el archivo `docker-compose.yml` y agregarlas bajo la clave `labels`.  
A continuación se muestra un ejemplo con **Home Assistant**:

```yaml
services:
  homeassistant:
    image: lscr.io/linuxserver/homeassistant:latest
    container_name: homeassistant
    network_mode: host
    environment:
      - PUID=1026
      - PGID=100
      - TZ=Etc/Madrid
    volumes:
      - /volume2/docker/homeassistant/config:/config
      - /volume2/temp/ha:/tmp
    labels:
      - "DCB-Auto-Update"
    restart: unless-stopped
```
</details>

<details>
<summary>🧩 He creado mis contenedores con docker-compose y aparecen agrupados, ¿puedo gestionar uno solo?</summary>

Sí. Cuando entras en un proyecto verás cada contenedor por separado con su estado y podrás actuar sobre él individualmente, igual que sobre los contenedores standalone.

Las acciones globales (arrancar, parar, reiniciar o eliminar el proyecto entero) están disponibles como un botón adicional dentro del menú del proyecto.
</details>

<details>
<summary>📢 Si configuro el canal de notificaciones, ¿se duplican las notificaciones?</summary>

No. Cuando se define ese canal, los avisos de cambio de estado de los contenedores (arranque, parada, caída y actualizaciones automáticas) van **solo** a ese canal y dejan de aparecer en el chat principal.

El resto de mensajes (resultados de comandos, menús interactivos, avisos de actualización disponible con sus botones, etc.) **nunca** van a ese canal: el bot siempre responde en el sitio desde el que le hablas, sea el chat privado o el grupo/tema configurado en `TELEGRAM_GROUP` y `TELEGRAM_THREAD`.
</details>

<details>
<summary>🖥️ Tengo varios hosts, ¿cómo sé en qué máquina está cada contenedor?</summary>

El bot te lo dice él, y solo cuando hace falta.

Con **un solo host** el nivel de host no aparece en ningún sitio: `/list` y todos los menús se ven exactamente igual que antes de que existieran los hosts. No hay nada que aprender ni nada que cambie.

En cuanto tienes **dos o más**, aparece donde importa: `/list` agrupa los contenedores por máquina con una cabecera, cada mensaje de acción dice de qué host habla, y los menús de `/run`, `/stop`, `/logs`… te preguntan primero el host —salvo que solo uno tenga algo que ofrecer, en cuyo caso se salta la pregunta y va directo—.

Si dos máquinas tienen un contenedor con el mismo nombre y escribes `/logs nginx`, el bot no adivina: te pregunta en cuál de las dos.

</details>

<details>
<summary>🔌 Un host aparece en 🔴 rojo, ¿qué hago?</summary>

Entra en `/settings` → **🖥️ Hosts de Docker** y púlsalo: la pantalla del host te dice el motivo exacto del fallo, y tienes un **🔄 Probar de nuevo** para reintentar sin salir de ahí.

Lo importante es que **una máquina caída no rompe el resto**: `/list` sigue funcionando y muestra los hosts que sí responden, marcando el que no. Los comandos y las comprobaciones de actualización se saltan el que está fuera.

Para diagnosticarlo, la prueba de siempre desde la máquina del bot vale más que cualquier menú:

```bash
ssh usuario@maquina docker version
```

El mensaje que te dé ahí es el mismo que te está dando el bot, pero con todo el detalle. Las causas habituales están en la tabla de *La prueba que lo decide* (desplegable 🖥️ de arriba).

</details>

<details>
<summary>🔐 Quiero usar <code>tcp://</code> pero cifrado con TLS, ¿se puede?</summary>

Se puede, sí, y aquí tienes el procedimiento. Pero antes lo honesto: **es bastante avanzado y yo no lo considero necesario.**

TLS resuelve un problema concreto: cruzar una red en la que no confías. Si tus máquinas están en tu casa, `ssh://` te da exactamente la misma protección —cifrado y autenticación por clave— sin generar ni un certificado, y reutilizando el ssh que ya tienes montado. Y si necesitas llegar desde fuera, montar Tailscale o WireGuard es menos trabajo que un PKI y te sirve para todo lo demás de tu red, no solo para esto.

Dicho eso: si tu caso lo pide de verdad, o simplemente te apetece, funciona perfectamente. Ten en cuenta que **es el único camino que no se puede añadir desde el bot** y hay que escribir el host a mano en `settings.json`, como se ve en el paso 5.

Todo lo de abajo va en la **máquina remota**, salvo donde diga lo contrario.

**1. Genera la CA y los certificados.** Sustituye `maquina.local` por el nombre o IP con el que el bot va a llamarla:

```bash
openssl genrsa -aes256 -out ca-key.pem 4096
openssl req -new -x509 -days 3650 -key ca-key.pem -sha256 -out ca.pem

openssl genrsa -out server-key.pem 4096
openssl req -subj "/CN=maquina.local" -sha256 -new -key server-key.pem -out server.csr
echo "subjectAltName = DNS:maquina.local,IP:192.168.1.50" > extfile.cnf
echo "extendedKeyUsage = serverAuth" >> extfile.cnf
openssl x509 -req -days 3650 -sha256 -in server.csr -CA ca.pem -CAkey ca-key.pem   -CAcreateserial -out server-cert.pem -extfile extfile.cnf

openssl genrsa -out key.pem 4096
openssl req -subj "/CN=client" -new -key key.pem -out client.csr
echo "extendedKeyUsage = clientAuth" > extfile-client.cnf
openssl x509 -req -days 3650 -sha256 -in client.csr -CA ca.pem -CAkey ca-key.pem   -CAcreateserial -out cert.pem -extfile extfile-client.cnf
```

**2. Configura el daemon** en `/etc/docker/daemon.json`:

```json
{
  "tlsverify": true,
  "tlscacert": "/etc/docker/certs/ca.pem",
  "tlscert": "/etc/docker/certs/server-cert.pem",
  "tlskey": "/etc/docker/certs/server-key.pem",
  "hosts": ["unix:///var/run/docker.sock", "tcp://0.0.0.0:2376"]
}
```

En systemd hay que quitar el `-H` que trae la unidad, o `dockerd` se queja de que el host está definido dos veces:

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
printf '[Service]
ExecStart=
ExecStart=/usr/bin/dockerd
' |   sudo tee /etc/systemd/system/docker.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart docker
```

**3. Copia `ca.pem`, `cert.pem` y `key.pem`** a la máquina del bot y mapéalos:

```yaml
volumes:
    - /ruta/a/los/certs:/certs:ro
```

**4. Comprueba antes de configurar el bot:**

```bash
docker --tlsverify --tlscacert=ca.pem --tlscert=cert.pem --tlskey=key.pem   -H tcp://maquina.local:2376 version
```

**5. Declara el host a mano.** Esta es la única vía que **no** se puede añadir desde `/settings`: la pantalla de añadir host solo pide una URL, y un host con TLS necesita además las rutas de los tres certificados. Se escribe en `settings.json`, dentro del volumen que mapeaste, en la lista `hosts`:

```json
{
    "id": "h_tls1",
    "alias": "maquina",
    "url": "tcp://maquina.local:2376",
    "local": false,
    "tls": {
        "ca": "/certs/ca.pem",
        "cert": "/certs/cert.pem",
        "key": "/certs/key.pem",
        "verify": true
    }
}
```

Las rutas son **las de dentro del contenedor**, o sea la parte derecha del volumen del paso 3. El `id` lo eliges tú y da igual cuál sea, siempre que no se repita: solo tiene que ser estable, porque es lo que atan las programaciones y la caché de actualizaciones.

> ❗ **Importante:** editar `settings.json` a mano **requiere reiniciar el contenedor**: el bot lo lee al arrancar y lo mantiene en memoria. Después del reinicio el host aparece en `/settings` → **🖥️ Hosts de Docker** como cualquier otro, y ya se puede probar, renombrar y quitar desde ahí.

</details>

<details>
<summary>💬 ¿Desde dónde puedo manejar el bot?</summary>

Desde el chat privado con el bot y desde el grupo (o el tema concreto del supergrupo) que hayas configurado en `TELEGRAM_GROUP` y `TELEGRAM_THREAD`.

El bot contesta siempre donde le has escrito: si lanzas `/list` en el privado, la lista aparece en el privado; si lo lanzas en el tema del grupo, aparece en ese tema. El canal de notificaciones solo recibe las alertas de cambio de estado.

Cualquier otro chat se ignora: si el bot está metido en otro grupo, ahí no responde ni ejecuta nada, aunque quien escriba sea administrador. Ser administrador no basta, el chat también tiene que ser el privado del propio administrador o `TELEGRAM_GROUP`.
</details>

<details>
<summary>🔄 ¿Cómo actualizo el propio bot?</summary>

Igual que cualquier otro contenedor: desde `/checkupdate docker-controller-bot` o desde `/updateall`.

Internamente el bot lanza un contenedor auxiliar (`UPDATER-Docker-Controler-Bot`) que se encarga de descargar la nueva imagen, sustituirla y volver a levantar el bot, evitando que el propio bot se quede sin proceso a mitad de la actualización.

Si le añades la label `DCB-Auto-Update` a su `docker-compose.yml`, se actualizará solo en cuanto detecte una nueva versión.
</details>

---

<details>
<summary>🧑‍💻 Solo para desarrolladores</summary>

### Ejecución con código local

Para su ejecución en local y probar nuevos cambios de código, se necesita renombrar el fichero `.env-example` a `.env` con los valores necesarios para su ejecución.
Es necesario establecer un `TELEGRAM_TOKEN` y un `TELEGRAM_ADMIN` correctos y diferentes al de la ejecución normal.

La estructura de carpetas debe quedar:

```
docker-controller-bot/
    ├── .env
    ├── .gitignore
    ├── LICENSE
    ├── requirements.txt
    ├── README.md
    ├── Dockerfile_local
    ├── docker-compose.debug.yaml
    ├── docker-controller-bot.py   # punto de entrada: importa y arranca
    ├── core.py                    # el núcleo: DockerManager, monitores, teclados, UI
    ├── commands.py                # un `cmd_*` por comando
    ├── callbacks.py               # un `cb_*` por botón
    ├── callback_registry.py       # el decorador `@callback` y el parseo
    ├── host_registry.py           # los hosts Docker y su caché de clientes
    ├── store.py                   # settings.json, estado y cachés
    ├── migration.py               # migraciones de arranque (4.x → 5.0)
    ├── config.py                  # variables de arranque y constantes
    ├── i18n.py                    # carga de idiomas y `get_text`
    ├── own_container.py           # se identifica a sí mismo sin que se lo digan
    ├── docker_update.py
    ├── docker_compose_manager.py
    ├── compose_generator.py
    ├── port_manager.py
    ├── schedule_manager.py
    ├── message_queue.py
    ├── logger.py
    ├── tests
    │   ├── run_all.py
    │   └── ...
    └── locale
        ├── en.json
        ├── es.json
        ├── de.json
        ├── ru.json
        ├── gl.json
        ├── nl.json
        ├── cat.json
        └── it.json
```

Para levantarlo habría que ejecutar en esa ruta: `docker compose -f docker-compose.debug.yaml up  -d --build --force-recreate`
Para detenerlo y eliminarlo: `docker compose down --rmi`

Para probar nuevos cambios bastaría con guardar. Los cambios se refrescan en caliente.

### Los tests

```bash
python3 tests/run_all.py
```

No necesitan Docker: el SDK va sustituido y los ajustes se escriben en un directorio temporal, así que se pueden lanzar en cualquier sitio. Pasan un rato comprobando cosas que solo se ven en tiempo de ejecución —nombres que un módulo no define, claves de idioma que nada renderiza, botones que llevarían un id sin su host, marcado que se ha ido de un idioma— así que si tocas código, lánzalos antes de proponer el cambio.

Se puede filtrar por módulo: `python3 tests/run_all.py test_hosts`.

### Depuración con VS Code

Abre la carpeta del repositorio en [Visual Studio Code](https://code.visualstudio.com/) necesitaras las siguientes extensiones instaladas en VS Code:

- [Docker](https://marketplace.visualstudio.com/items?itemName=ms-azuretools.vscode-docker)
- [Python](https://marketplace.visualstudio.com/items?itemName=ms-python.python)

#### Instalación de las extensiones

1. Abre VS Code.
2. Ve a la extensión de la barra lateral y busca "Docker" y "Python".
3. Instala ambas extensiones desde el Marketplace.

#### Establecer Puntos de Parada (Breakpoints)

1. Abre el archivo de código que deseas depurar.
2. Haz clic en el margen izquierdo junto a la línea de código donde quieras establecer un punto de parada. Aparecerá un punto rojo indicando el `breakpoint`.

#### Iniciar la Depuración

1. Ve al menú `Run` y selecciona `Start Debugging` o presiona `F5`.
2. VS Code arrancará el `docker-compose.debug.yaml` y comenzará la depuración.
3. La ventana de depuración se abrirá en la parte inferior, mostrando las variables, la pila de llamadas y la consola de depuración.

![Depuracion](assets/debug.gif)

#### Conclusión de la Depuración

- Para detener la sesión de depuración, ve a `Run > Stop Debugging` o presiona `Shift+F5`

</details>
