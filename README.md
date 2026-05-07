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
![alt text](https://github.com/dgongut/pictures/blob/main/Docker-Controller-Bot/mockup.png)

<h3 align="center">
  ReadMe en Español
  <span> | </span>
  <a href="./README_EN.md">ReadMe in English</a>
  <span> | </span>
  <a href="https://t.me/dockercontrollerbotnews">Canal de Noticias en Telegram</a>
</h3>

Lleva el control de tus contenedores docker desde un único lugar.

- ✅ Listar contenedores
- ✅ Arrancar, parar y eliminar contenedores
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
- ✅ Silencia las notificaciones de forma temporal
- ✅ Imagen multiarquitectura (amd64, arm64, armv7…) compatible con Raspberry Pi, NAS y servidores estándar
- ✅ Soporte de idiomas (Spanish, English, Dutch, German, Russian, Galician, Italian, Catalan)

¿Lo buscas en [![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/docker-controller-bot)?

**NUEVO** Canal de novedades en [![](https://badgen.net/badge/icon/telegram?icon=telegram&label)](https://t.me/dockercontrollerbotnews)

## Crear tu bot de Telegram

Antes de levantar el contenedor necesitas un bot propio en Telegram y conocer tu identificador de usuario.

1. Abre [@BotFather](https://t.me/BotFather) en Telegram y envía `/newbot`. Sigue las instrucciones (un nombre y un username acabado en `bot`).
2. BotFather te devolverá el token del bot. Guárdalo: irá en la variable `TELEGRAM_TOKEN`.
3. Para conocer tu propio chat ID (lo necesitas para `TELEGRAM_ADMIN`), habla con [@MissRose_bot](https://t.me/MissRose_bot) y envíale `/id`. Te responderá con un número, ese es tu ID.
4. *(Opcional)* Si vas a usar el bot dentro de un grupo, añádelo, hazlo administrador y obtén el chat ID del grupo de la misma forma; ese valor irá en `TELEGRAM_GROUP`.
5. *(Opcional)* Si quieres ponerle el icono oficial al bot, descarga la imagen en alta resolución [aquí](https://raw.githubusercontent.com/dgongut/pictures/main/Docker-Controller-Bot/Docker-Controller-Bot.png) y envíasela a [@BotFather](https://t.me/BotFather) usando la opción `/setuserpic`.

## Comandos disponibles

Casi todos los comandos pueden ejecutarse en dos modos: escribiendo el comando solo (`/run`) para que el bot muestre un menú interactivo con botones, o pasando directamente el nombre del contenedor (`/run nginx`) para actuar sin menús.

| Comando | Descripción |
|---|---|
| `/start` | Menú principal con la lista de comandos |
| `/list` | Listado completo de contenedores |
| `/run` `/stop` `/restart` | Arranca / detiene / reinicia un contenedor o un proyecto Compose entero |
| `/delete` | Elimina un contenedor o un proyecto Compose entero |
| `/exec` | Ejecuta un comando dentro de un contenedor |
| `/logs` `/logfile` | Logs en mensaje o como fichero |
| `/checkupdate` | Comprueba si un contenedor tiene actualización |
| `/updateall` | Actualiza todos los contenedores |
| `/changetag` | Cambia el tag de la imagen (rollback o salto de versión) |
| `/compose` | Extrae el `docker-compose` de un contenedor o proyecto |
| `/info` | Muestra información detallada de un contenedor o de un proyecto |
| `/ports` | Lista puertos usados, comprueba uno concreto o genera uno libre |
| `/prune` | Limpia contenedores, imágenes, redes o volúmenes no usados |
| `/mute <minutos>` | Silencia las notificaciones durante X minutos |
| `/schedule` | Menú para crear, editar y borrar tareas programadas |
| `/version` `/donate` `/donors` | Versión actual / donar / lista de donantes |

## Soporte para Docker Compose

Si tus contenedores fueron creados con `docker compose`, el bot los reconoce automáticamente como un **proyecto** y los presenta agrupados.

En comandos como `/run`, `/stop`, `/restart`, `/delete`, `/info` o `/compose` verás primero la lista de proyectos y, al pulsar uno, sus contenedores. Las acciones de inicio, parada, reinicio y borrado se pueden aplicar al **proyecto entero** o a un contenedor individual.

## Programaciones (`/schedule`)

Desde `/schedule` puedes crear tareas que se ejecuten en cron.

- Acciones soportadas: `run`, `stop`, `restart`, `exec`, `prune` y `mute`.
- Acepta expresiones cron estándar (`0 */4 * * *`) y atajos: `@yearly`, `@monthly`, `@weekly`, `@daily`, `@hourly` y `@reboot`.
- Las programaciones se persisten en `/app/schedule` (recuerda mapear ese volumen).

## Configuración en las variables del Docker Compose

| CLAVE  | OBLIGATORIO | VALOR |
|:------------- |:---------------:| :-------------|
|TELEGRAM_TOKEN |✅| Token del bot |
|TELEGRAM_ADMIN |✅| ChatId del administrador (se puede obtener hablándole al bot [Rose](https://t.me/MissRose_bot) escribiendo /id). Admite múltiples administradores separados por comas. Por ejemplo 12345,54431,55944 |
|TELEGRAM_GROUP |❌| ChatId del grupo. Si este bot va a formar parte de un grupo, es necesario especificar el chatId de dicho grupo. Es necesario que el bot sea administrador del grupo |
|TELEGRAM_THREAD |❌| Thread del tema dentro de un supergrupo; valor numérico (2,3,4..). Por defecto 1. Se utiliza en conjunción con la variable TELEGRAM_GROUP |
|TELEGRAM_NOTIFICATION_CHANNEL |❌| Canal donde se publicarán exclusivamente los cambios de estado de los contenedores |
|CONTAINER_NAME |✅| Nombre del contenedor, lo que se le ponga en container_name en el docker-compose ha de ir aquí también |
|TZ |✅| Timezone (Por ejemplo Europe/Madrid) |
|CHECK_UPDATES |❌| Si se desea que compruebe actualizaciones. 0 no - 1 sí. Por defecto 1|
|CHECK_UPDATE_EVERY_HOURS |❌| Tiempo de espera en horas entre chequeo de actualizaciones. Por defecto 4 |
|CHECK_UPDATE_STOPPED_CONTAINERS |❌| Si se desea que compruebe las actualizaciones de los contenedores detenidos. 0 no - 1 sí. Por defecto 1 |
|BUTTON_COLUMNS |❌| Numero de columnas de botones en las listas de contenedores. Por defecto 2 |
|LANGUAGE |❌| Idioma, puede ser ES / EN / NL / DE / RU / GL / IT / CAT. Por defecto ES (Spanish) | 
|EXTENDED_MESSAGES |❌| Si se desea que muestre más mensajes de información. 0 no - 1 sí. Por defecto 0 | 

## Anotaciones
> [!WARNING]
> Será necesario mapear un volumen para almacenar lo que el bot escribe en /app/schedule

> [!NOTE]
> Si se requiere tener la sesión iniciada en algún registro como DockerHub, GitHub Registry o alguno privado (docker login) es posible trasladar ese login al contenedor mapeando el `~/.docker/config.json` a `/root/.docker/config.json`

## Ejemplo de Docker-Compose para su ejecución normal

```yaml
services:
    docker-controller-bot:
        environment:
            - TELEGRAM_TOKEN=
            - TELEGRAM_ADMIN=
            - CONTAINER_NAME=docker-controller-bot
            - TZ=Europe/Madrid
            #- TELEGRAM_GROUP=
            #- TELEGRAM_THREAD=1
            #- TELEGRAM_NOTIFICATION_CHANNEL=
            #- CHECK_UPDATES=1
            #- CHECK_UPDATE_EVERY_HOURS=4
            #- CHECK_UPDATE_STOPPED_CONTAINERS=1
            #- BUTTON_COLUMNS=2
            #- LANGUAGE=ES
            #- EXTENDED_MESSAGES=0
        volumes:
            - /var/run/docker.sock:/var/run/docker.sock # NO CAMBIAR
            - /ruta/para/guardar/las/programaciones:/app/schedule # CAMBIAR LA PARTE IZQUIERDA
            #- ~/.docker/config.json:/root/.docker/config.json # Solo si se requiere iniciar sesión en algún registro
        image: dgongut/docker-controller-bot:latest
        container_name: docker-controller-bot
        restart: always
        network_mode: host
        tty: true
```

## Funciones Extra mediante Labels/Etiquetas en otros contenedores

- Añadiendo la etiqueta `DCB-Ignore-Check-Updates` a un contenedor, no se comprobarán actualizaciones para él.
- Añadiendo la etiqueta `DCB-Auto-Update` a un contenedor, se actualizará automáticamente sin preguntar.

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
<summary>📢 Si configuro <code>TELEGRAM_NOTIFICATION_CHANNEL</code>, ¿se duplican las notificaciones?</summary>

No. Cuando se define ese canal, los avisos de cambio de estado de los contenedores (arranque, parada, caída, actualización disponible…) van **solo** a ese canal y dejan de aparecer en el chat principal.

El resto de mensajes (resultados de comandos, menús interactivos, etc.) siguen llegando al chat normal donde hablas con el bot.
</details>

<details>
<summary>🔄 ¿Cómo actualizo el propio bot?</summary>

Igual que cualquier otro contenedor: desde `/checkupdate docker-controller-bot` o desde `/updateall`.

Internamente el bot lanza un contenedor auxiliar (`UPDATER-Docker-Controler-Bot`) que se encarga de descargar la nueva imagen, sustituirla y volver a levantar el bot, evitando que el propio bot se quede sin proceso a mitad de la actualización.

Si le añades la label `DCB-Auto-Update` a su `docker-compose.yml`, se actualizará solo en cuanto detecte una nueva versión.
</details>

---
## Solo para desarrolladores

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
    ├── config.py
    ├── docker-controller-bot.py
    ├── Dockerfile_local
    ├── docker-compose.yaml
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
