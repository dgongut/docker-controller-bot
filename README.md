# docker-controller-bot

![alt text](https://i.ibb.co/5jPfTQ1/Docker-Controller-Bot-shadow.png)

Lleva el control de tus contenedores docker desde un único lugar.
 - ✅ Listar contenedores
 - ✅ Arrancar, parar y eliminar contenedores
 - ✅ Obtener los logs tanto de manera directa como a través de fichero
 - ✅ Extraer el docker-compose de tus contenedores
 - ✅ Notificaciones cuando un contenedor se cae o se inicia
 - ✅ Notificaciones cuando un contenedor tiene una actualización pendiente
 - ✅ Actualizaciones de los contenedores

¿Lo buscas en [docker](https://hub.docker.com/r/dgongut/docker-controller-bot)?

## Configuración en config.py
                    
| CLAVE  | OBLIGATORIO | VALOR |
|:------------- |:---------------:| :-------------|
|TELEGRAM_TOKEN |✅| Token del bot |
|TELEGRAM_ADMIN |✅| ChatId del administrador (se puede obtener hablándole al bot Rose escribiendo /id) |
|TELEGRAM_GROUP |❌| ChatId del grupo. Si este bot va a formar parte de un grupo, es necesario especificar el chatId de dicho grupo |
|CONTAINER_NAME |✅| Nombre del contenedor, lo que se le ponga en container_name en el docker-compose ha de ir aquí también |
|CHECK_UPDATES |❌| Si se desea que compruebe actualizaciones. 0 no - 1 sí. Por defecto 1|
|CHECK_UPDATE_EVERY_HOURS |❌| Tiempo de espera en horas entre chequeo de actualizaciones (4 horas por defecto) | 

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
            #- CHECK_UPDATES=1
            #- CHECK_UPDATE_EVERY_HOURS=4
        volumes:
            - /var/run/docker.sock:/var/run/docker.sock
        image: dgongut/docker-controller-bot:latest
        container_name: docker-controller-bot
        restart: always
        network_mode: host
        tty: true
```
