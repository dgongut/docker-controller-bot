"""
Schedule Flow Module
Handles the interactive flow for adding/deleting schedules
Optimized: Uses JSON instead of pickle for better security and portability
"""

import json
import os
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

CACHE_DIR = "./cache/"
STATE_EXPIRY_HOURS = 24  # Auto-cleanup states older than 24 hours

def _get_state_path(user_id: int) -> str:
    """Get the file path for a user's state"""
    return os.path.join(CACHE_DIR, f"schedule_state_{user_id}.json")

def save_schedule_state(user_id: int, state: Dict[str, Any]):
    """Save schedule creation state for a user using JSON (secure, portable)"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _get_state_path(user_id)
    try:
        # Add timestamp for auto-cleanup
        state["_timestamp"] = datetime.now().isoformat()
        with open(path, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Error saving schedule state for user {user_id}: {e}")

def load_schedule_state(user_id: int) -> Optional[Dict[str, Any]]:
    """Load schedule creation state for a user. Returns None if expired or not found"""
    path = _get_state_path(user_id)
    try:
        if not os.path.exists(path):
            return None

        with open(path, 'r') as f:
            state = json.load(f)

        # Check if state has expired
        if "_timestamp" in state:
            timestamp = datetime.fromisoformat(state["_timestamp"])
            if datetime.now() - timestamp > timedelta(hours=STATE_EXPIRY_HOURS):
                clear_schedule_state(user_id)  # Auto-cleanup expired state
                return None

        return state
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading schedule state for user {user_id}: {e}")
        return None

def clear_schedule_state(user_id: int):
    """Clear schedule creation state for a user"""
    path = _get_state_path(user_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"Error clearing schedule state for user {user_id}: {e}")

def init_add_schedule_state() -> Dict[str, Any]:
    """Initialize state for adding a new schedule"""
    return {
        "step": "ask_name",
        "name": None,
        "cron": None,
        "action": None,
        "container": None,
        "minutes": None,
        "show_output": None,
        "command": None
    }

