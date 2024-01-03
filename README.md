# docker-controller-bot

Lleva el control de tus contenedores docker desde un único lugar.
 - ✅ Listar contenedores
 - ✅ Arrancar o parar contenedores
 - ✅ Obtener los logs tanto de manera directa como a través de fichero
 - ✅ Extraer el docker-compose de tus contenedores 

¿Lo buscas en [docker](https://hub.docker.com/r/dgongut/docker-controller-bot)?

## Configuración en config.py
                    
| CLAVE  | OBLIGATORIO | VALOR |
|:------------- |:---------------:| :-------------|
|TELEGRAM_TOKEN |✅| Token del bot |
|TELEGRAM_CHAT_ID |✅| ChatId del administrador (se puede obtener hablándole al bot Rose escribiendo /id) | 

### Anotaciones
La función de extracción de docker-compose se encuentra en una fase temprana de desarrollo y puede contener errores.