import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_ADMIN = os.environ.get("TELEGRAM_ADMIN")
TELEGRAM_GROUP = os.environ.get("TELEGRAM_GROUP")
TELEGRAM_THREAD = os.environ.get("TELEGRAM_THREAD")
CHECK_UPDATES_RAW = os.environ.get("CHECK_UPDATES")
CHECK_UPDATE_EVERY_HOURS = os.environ.get("CHECK_UPDATE_EVERY_HOURS")
CONTAINER_NAME = os.environ.get("CONTAINER_NAME")
UPDATER_IMAGE = "dgongut/docker-container-updater:latest"
UPDATER_CONTAINER_NAME = "UPDATER-Docker-Controler-Bot"
BUTTON_COLUMNS = int(os.environ.get("BUTTON_COLUMNS"))
CONTAINER_ID_LENGTH = 5
UPDATED_CONTAINER_TEXT = "Contenedor actualizado ✅"
NEED_UPDATE_CONTAINER_TEXT = "Actualización disponible ⬆️"