# docker-controller-bot

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
            #- TELEGRAM_GROUP=
        volumes:
            - /var/run/docker.sock:/var/run/docker.sock
        image: dgongut/docker-controller-bot:latest
        container_name: docker-controller-bot
        tty: true
```
