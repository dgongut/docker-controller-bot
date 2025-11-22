"""
Schedule Manager Module
Handles reading/writing schedules in JSON format with improved structure
Optimized for performance with caching and efficient lookups
"""

import json
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any

class ScheduleManager:
    """Manages schedules stored in JSON format with caching and efficient lookups"""

    def __init__(self, schedule_path: str = "/app/schedule", schedule_file: str = "schedules.json"):
        self.schedule_path = schedule_path
        self.schedule_file = schedule_file
        self.full_path = os.path.join(schedule_path, schedule_file)
        self._file_lock = threading.Lock()
        self._cache = None  # Cache for schedules
        self._cache_dirty = False  # Flag to track if cache needs refresh
        self._next_id = 1  # Track next available ID
        self._ensure_file_exists()
        self._load_cache()

    def _ensure_file_exists(self):
        """Create schedule file if it doesn't exist"""
        os.makedirs(self.schedule_path, exist_ok=True)
        if not os.path.exists(self.full_path):
            with self._file_lock:
                with open(self.full_path, 'w') as f:
                    json.dump({"schedules": []}, f, indent=2)

    def _load_cache(self):
        """Load schedules into cache and calculate next ID"""
        try:
            with self._file_lock:
                with open(self.full_path, 'r') as f:
                    data = json.load(f)
            self._cache = data.get("schedules", [])
            # Calculate next available ID (max existing ID + 1)
            self._next_id = max([s.get("id", 0) for s in self._cache], default=0) + 1
        except Exception as e:
            print(f"Error loading cache: {e}")
            self._cache = []
            self._next_id = 1

    def _read_schedules(self) -> List[Dict[str, Any]]:
        """Get schedules from cache (optimized)"""
        if self._cache_dirty:
            self._load_cache()
            self._cache_dirty = False
        return self._cache if self._cache is not None else []

    def _write_schedules(self):
        """Write cached schedules to JSON file"""
        try:
            with self._file_lock:
                with open(self.full_path, 'w') as f:
                    json.dump({"schedules": self._cache}, f, indent=2)
            self._cache_dirty = False
        except Exception as e:
            print(f"Error writing schedules: {e}")
    
    def add_schedule(self, name: str, cron: str, action: str, container: str = None,
                     minutes: int = None, show_output: bool = False, command: str = None) -> bool:
        """Add a new schedule. Returns True if successful, False if name already exists"""
        schedules = self._read_schedules()

        # Check if name already exists (optimized with early return)
        if any(s["name"] == name for s in schedules):
            return False

        schedule = {
            "id": self._next_id,  # Use tracked ID instead of calculating
            "name": name,
            "cron": cron,
            "action": action,
            "container": container,
            "minutes": minutes,
            "show_output": show_output,
            "command": command,
            "created_at": datetime.now().isoformat(),
            "enabled": True
        }

        self._cache.append(schedule)
        self._next_id += 1
        self._write_schedules()
        return True
    
    def delete_schedule(self, name: str) -> bool:
        """Delete a schedule by name. Returns True if deleted, False if not found"""
        schedules = self._read_schedules()
        original_count = len(schedules)
        self._cache = [s for s in schedules if s["name"] != name]

        if len(self._cache) < original_count:
            self._write_schedules()
            return True
        return False

    def get_all_schedules(self) -> List[Dict[str, Any]]:
        """Get all schedules (from cache)"""
        return self._read_schedules()

    def get_schedule(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a specific schedule by name (optimized with early return)"""
        for schedule in self._read_schedules():
            if schedule["name"] == name:
                return schedule
        return None

    def get_schedule_by_id(self, schedule_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific schedule by ID (optimized with early return)"""
        for schedule in self._read_schedules():
            if schedule.get("id") == schedule_id:
                return schedule
        return None

    def update_schedule(self, schedule_name: str, **kwargs) -> bool:
        """Update a schedule. Returns True if updated, False if not found"""
        schedules = self._read_schedules()
        for schedule in schedules:
            if schedule["name"] == schedule_name:
                schedule.update(kwargs)
                self._write_schedules()
                return True
        return False

    def toggle_schedule(self, name: str) -> Optional[bool]:
        """Toggle schedule enabled/disabled status. Returns new status or None if not found"""
        schedules = self._read_schedules()
        for schedule in schedules:
            if schedule["name"] == name:
                schedule["enabled"] = not schedule.get("enabled", True)
                self._write_schedules()
                return schedule["enabled"]
        return None

    def get_enabled_schedules(self) -> List[Dict[str, Any]]:
        """Get only enabled schedules (optimized with list comprehension)"""
        return [s for s in self._read_schedules() if s.get("enabled", True)]

