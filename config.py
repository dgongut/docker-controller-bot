import os

# DOCKER ENVIRONMENT VARIABLES
#
# Only what the bot needs before it can read its own settings file lives here:
# how to reach Telegram, who is allowed to talk to it, and which container it
# is. Everything else is a setting, managed from /settings. The dividing line
# is that a wrong value in any of these can lock you out of the bot, and you
# cannot fix from the chat what stops the chat from working.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_ADMIN = os.environ.get("TELEGRAM_ADMIN")
TELEGRAM_GROUP = os.environ.get("TELEGRAM_GROUP")
TELEGRAM_THREAD = os.environ.get("TELEGRAM_THREAD", "1")
CONTAINER_NAME = os.environ.get("CONTAINER_NAME")

def _as_bool(raw):
    """Reads the 0/1 flags the bot has always used, tolerating true/false too."""
    return str(raw).strip().lower() in ("1", "true", "yes", "on")

def _as_language(raw):
    """
    Normalises a language code to the casing used everywhere else.

    The variable was accepted in any case, and storing it verbatim would leave
    the settings file holding "es" for someone upgrading and "ES" for someone
    who picked it from the menu.
    """
    code = str(raw).strip().upper()
    return code if code in SUPPORTED_LANGUAGES else "ES"

SUPPORTED_LANGUAGES = ("ES", "EN", "NL", "DE", "RU", "GL", "IT", "CAT")

# Variables that became settings in 5.0.0, mapped to their key in the settings
# file. On the first run they seed it, so updating from 4.x keeps every value
# the user had. From then on the settings file is the only source and a
# variable still present in the compose only earns a deprecation warning:
# letting it win would mean a change made in /settings silently reverts on the
# next restart.
SETTINGS_FROM_ENV = {
    "LANGUAGE": ("bot.language", _as_language),
    "BUTTON_COLUMNS": ("bot.button_columns", int),
    "EXTENDED_MESSAGES": ("bot.extended_messages", _as_bool),
    "MULTI_SELECTION": ("bot.multi_selection", _as_bool),
    "TELEGRAM_NOTIFICATION_CHANNEL": ("bot.notification_channel", str),
    "CHECK_UPDATES": ("bot.check_updates", _as_bool),
    "CHECK_UPDATE_EVERY_HOURS": ("bot.check_update_every_hours", float),
    "CHECK_UPDATE_STOPPED_CONTAINERS": ("bot.check_update_stopped_containers", _as_bool),
}

# CONSTANTS
UPDATER_IMAGE = "dgongut/docker-container-updater:latest"
UPDATER_CONTAINER_NAME = "UPDATER-Docker-Controler-Bot"
CONTAINER_ID_LENGTH = 5
ANONYMOUS_USER_ID = "1087968824"
# Shown in the language picker. Each language is named in itself, which is what
# someone looking for their own language actually scans for.
LANGUAGE_NAMES = {
    "ES": "Español",
    "EN": "English",
    "NL": "Nederlands",
    "DE": "Deutsch",
    "RU": "Русский",
    "GL": "Galego",
    "IT": "Italiano",
    "CAT": "Català",
}
DONORS_URL = "https://donate.dgongut.com/donors.json"
ICON_CONTAINER_MARK_FOR_UPDATE = "➕"
ICON_CONTAINER_MARKED_FOR_UPDATE = "✅"
ICON_CONTAINER_ACTION_DONE = "✅"

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

# Scheduled actions that act on Docker, and so belong to one host. Muting is
# the bot's own notifications: it belongs to no machine, and showing it a host
# would be claiming something that means nothing.
# Everything a schedule can do, in the order the picker offers it. One list
# rather than a hardcoded row of buttons: the screen also prints what each one
# does, and the two drifting apart is how `prune` ended up with a button and no
# explanation.
# Techo del silencio, en minutos: treinta días. No es una preferencia sino un
# límite técnico — el temporizador que devuelve la voz al bot se programa con
# ese número, y uno absurdo lo mata con OverflowError dentro de su hilo, con lo
# que el bot se queda callado para siempre y sin nada que lo despierte.
MUTE_MAX_MINUTES = 60 * 24 * 30

SCHEDULE_ACTIONS = ("run", "stop", "restart", "mute", "exec", "prune")

HOST_SCOPED_SCHEDULE_ACTIONS = frozenset({"run", "stop", "restart", "exec", "prune"})

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
    "prune": {
        "params": ["prune_type", "show_output"],
        "validators": {
            "prune_type": lambda x: x in ("containers", "images", "networks", "volumes"),
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
