import os

# DOCKER ENVIRONMENT VARIABLES
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_ADMIN = os.environ.get("TELEGRAM_ADMIN")
TELEGRAM_GROUP = os.environ.get("TELEGRAM_GROUP")
TELEGRAM_NOTIFICATION_CHANNEL = os.environ.get("TELEGRAM_NOTIFICATION_CHANNEL")
TELEGRAM_THREAD = os.environ.get("TELEGRAM_THREAD", "1")
CHECK_UPDATES = bool(int(os.environ.get("CHECK_UPDATES", "1")))
CHECK_UPDATE_EVERY_HOURS = float(os.environ.get("CHECK_UPDATE_EVERY_HOURS", "4"))
CHECK_UPDATE_STOPPED_CONTAINERS = bool(int(os.environ.get("CHECK_UPDATE_STOPPED_CONTAINERS", "1")))
CONTAINER_NAME = os.environ.get("CONTAINER_NAME")
LANGUAGE = os.environ.get("LANGUAGE", "ES")
EXTENDED_MESSAGES = bool(int(os.environ.get("EXTENDED_MESSAGES", "0")))
BUTTON_COLUMNS = int(os.environ.get("BUTTON_COLUMNS", "2"))

# CONSTANTS
UPDATER_IMAGE = "dgongut/docker-container-updater:latest"
UPDATER_CONTAINER_NAME = "UPDATER-Docker-Controler-Bot"
CONTAINER_ID_LENGTH = 5
ANONYMOUS_USER_ID = "1087968824"
SCHEDULE_PATH = "/app/schedule"
SCHEDULE_JSON_FILE = "schedules.json"
MUTE_FILE = ".muted_until"
FULL_SCHEDULE_JSON_PATH = f'{SCHEDULE_PATH}/{SCHEDULE_JSON_FILE}'
FULL_MUTE_FILE_PATH = f'{SCHEDULE_PATH}/{MUTE_FILE}'
DONORS_URL = "https://donate.dgongut.com/donors.json"
ICON_CONTAINER_MARK_FOR_UPDATE = "➕"
ICON_CONTAINER_MARKED_FOR_UPDATE = "✅"

# LABELS
LABEL_IGNORE_CHECK_UPDATES = "DCB-Ignore-Check-Updates"
LABEL_AUTO_UPDATE = "DCB-Auto-Update"

docker_architectures = {
    "x86_64": "amd64",
    "i386": "i386",
    "386": "386",
    "amd64": "amd64",
    "arm": "arm32v7",
    "arm64": "arm64",
    "aarch64": "arm64",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
    "unknown": "unknown",
}

CALL_PATTERNS = {
    "askCommand": ["containerId", "containerName"],
    "cancelAskCommand": [],
    "cancelExec": ["commandId"],
    "changeTag": ["containerId", "containerName", "tag"],
    "changeTagContainer": ["containerId", "containerName"],
    "cerrar": [],
    "checkUpdate": ["containerId", "containerName"],
    "compose": ["containerId", "containerName"],
    "confirmChangeTag": ["containerId", "containerName", "tag"],
    "confirmDelete": ["containerId", "containerName"],
    "confirmExec": ["containerId", "containerName", "commandId"],
    "confirmUpdate": ["containerId", "containerName"],
    "confirmUpdateAll": [],
    "confirmUpdateSelected": ["originalMessageId"],
    "delete": ["containerId", "containerName"],
    "deleteSchedule": ["scheduleHash"],
    "exec": ["containerId", "containerName", "commandId"],
    "info": ["containerId", "containerName"],
    "logfile": ["containerId", "containerName"],
    "logs": ["containerId", "containerName"],
    "toggleUpdate": ["containerName"],
    "toggleUpdateAll": [],
    "toggleRun": ["containerName"],
    "toggleRunAll": [],
    "toggleStop": ["containerName"],
    "toggleStopAll": [],
    "toggleRestart": ["containerName"],
    "toggleRestartAll": [],
    "prune": ["action"],
    "restart": ["containerId", "containerName"],
    "run": ["containerId", "containerName"],
    "stop": ["containerId", "containerName"],
    "update": ["containerId", "containerName"],
    "updateAll": [],
    "updateSelected": ["originalMessageId"],
    "confirmRunSelected": ["originalMessageId"],
    "runSelected": ["originalMessageId"],
    "confirmStopSelected": ["originalMessageId"],
    "stopSelected": ["originalMessageId"],
    "confirmRestartSelected": ["originalMessageId"],
    "restartSelected": ["originalMessageId"],
    "scheduleMenu": [],
    "scheduleAdd": [],
    "scheduleEdit": [],
    "scheduleSelectEdit": ["action"],
    "scheduleEditField": ["field", "scheduleId"],
    "scheduleEditValue": ["field", "scheduleId", "value"],
    "scheduleEditStatus": ["scheduleId"],
    "scheduleDelete": [],
    "scheduleSelectDelete": ["scheduleHash"],
    "scheduleSelectToggle": ["scheduleHash"],
    "scheduleSelectAction": ["action"],
    "scheduleSelectContainer": ["containerIdx"],
    "scheduleSelectShowOutput": ["action"],
    "scheduleConfirm": [],
}

# SCHEDULE COMMAND PATTERNS - Define required parameters for each schedule action
# Format: "action": {
#     "params": ["param1", "param2", ...],  # Required parameters
#     "validators": {"param_name": validation_function}  # Optional validators
# }
SCHEDULE_PATTERNS = {
    "run": {
        "params": ["container"],
    },
    "stop": {
        "params": ["container"],
    },
    "restart": {
        "params": ["container"],
    },
    "mute": {
        "params": ["minutes"],
        "validators": {
            "minutes": lambda x: x.isdigit() and int(x) > 0,
        },
    },
    "exec": {
        "params": ["container", "show_output", "command"],
        "validators": {
            "show_output": lambda x: x in ("0", "1"),
        },
    },
}

# SPECIAL CRON EXPRESSIONS - Supported by croniter and custom
# These are aliases that can be used instead of full cron expressions
SPECIAL_CRON_EXPRESSIONS = [
    "@yearly",      # Run once a year (0 0 1 1 *)
    "@annually",    # Same as @yearly
    "@monthly",     # Run once a month (0 0 1 * *)
    "@weekly",      # Run once a week (0 0 * * 0)
    "@daily",       # Run once a day (0 0 * * *)
    "@midnight",    # Same as @daily
    "@hourly",      # Run once an hour (0 * * * *)
    "@reboot",      # Run at system boot (custom handling)
]
