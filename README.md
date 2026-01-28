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
- ✅ Obtener los logs tanto de manera directa como a través de fichero
- ✅ Extraer el docker-compose de tus contenedores
- ✅ Notificaciones cuando un contenedor se cae o se inicia
- ✅ Notificaciones cuando un contenedor tiene una actualización pendiente
- ✅ Actualizaciones de los contenedores
- ✅ Cambiar el tag (rollback o actualización)
- ✅ Limpia el sistema, eliminado contenedores, imagenes y otros objetos no utilizados.
- ✅ Ejecuta comandos dentro de contenedores
- ✅ Visualiza puertos usados por contenedores y genera puertos aleatorios disponibles
- ✅ Soporte de idiomas (Spanish, English, Dutch, German, Russian, Galician, Italian, Catalan)

¿Lo buscas en [![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/docker-controller-bot)?

**NUEVO** Canal de novedades en [![](https://badgen.net/badge/icon/telegram?icon=telegram&label)](https://t.me/dockercontrollerbotnews)

🖼️ Si deseas establecerle el icono al bot de telegram, te dejo [aquí](https://raw.githubusercontent.com/dgongut/pictures/main/Docker-Controller-Bot/Docker-Controller-Bot.png) el icono en alta resolución. Solo tienes que descargarlo y mandárselo al [BotFather](https://t.me/BotFather) en la opción de BotPic.

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
|GROUPED_UPDATES |❌| Si se desea que agrupe los mensajes de las actualizaciones en uno solo. 0 no - 1 sí. Por defecto 1 | 
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
            #- GROUPED_UPDATES=1
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
