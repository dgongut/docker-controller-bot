#!/usr/bin/env python3
"""
Migration script: Convert schedule.txt to schedules.json
Run this once to migrate existing schedules to the new JSON format
"""

import os
import json
from datetime import datetime
from schedule_manager import ScheduleManager

def migrate_schedules():
    """Migrate schedules from TXT to JSON format"""

    schedule_path = "/app/schedule"
    old_file = os.path.join(schedule_path, "schedule.txt")

    # Check if old file exists
    if not os.path.exists(old_file):
        print("✓ No schedule.txt found. Starting with empty schedules.json")
        return

    manager = ScheduleManager(schedule_path)

    try:
        with open(old_file, 'r') as f:
            lines = f.readlines()

        migrated_count = 0
        skipped_count = 0
        actual_line_num = 0  # Track actual line number for better error reporting

        for actual_line_num, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue

            # Parse the line: CRON ACTION [PARAMS...]
            parts = line.split()
            if len(parts) < 6:  # At least: cron(5) + action
                print(f"⚠ Skipping line {actual_line_num}: {line}")
                skipped_count += 1
                continue

            try:
                # Extract cron (5 parts)
                cron = " ".join(parts[:5])
                action = parts[5].lower()
                params = parts[6:]

                # Validate cron expression (basic check)
                cron_parts = cron.split()
                if len(cron_parts) != 5:
                    print(f"⚠ Skipping line {actual_line_num}: Invalid cron format: {cron}")
                    skipped_count += 1
                    continue

                # Generate a name from action and container/minutes
                if action == "mute" and params:
                    name = f"mute_{params[0]}_min"
                elif params:
                    name = f"{action}_{params[0]}"
                else:
                    name = f"{action}_{actual_line_num}"

                # Ensure unique name
                counter = 1
                original_name = name
                while manager.get_schedule(name):
                    name = f"{original_name}_{counter}"
                    counter += 1

                # Add schedule based on action
                if action == "run" and params:
                    manager.add_schedule(name, cron, action, container=params[0])
                    migrated_count += 1
                elif action == "stop" and params:
                    manager.add_schedule(name, cron, action, container=params[0])
                    migrated_count += 1
                elif action == "restart" and params:
                    manager.add_schedule(name, cron, action, container=params[0])
                    migrated_count += 1
                elif action == "mute" and len(params) >= 1:
                    try:
                        minutes = int(params[0])
                        manager.add_schedule(name, cron, action, minutes=minutes)
                        migrated_count += 1
                    except ValueError:
                        print(f"⚠ Skipping line {actual_line_num}: Invalid minutes value '{params[0]}': {line}")
                        skipped_count += 1
                elif action == "exec" and len(params) >= 3:
                    container = params[0]
                    show_output = params[1] == "1"
                    command = " ".join(params[2:])
                    manager.add_schedule(name, cron, action, container=container,
                                       show_output=show_output, command=command)
                    migrated_count += 1
                else:
                    print(f"⚠ Skipping line {actual_line_num}: Unknown format: {line}")
                    skipped_count += 1

            except Exception as e:
                print(f"⚠ Error processing line {actual_line_num}: {line}")
                print(f"  Error: {e}")
                skipped_count += 1

        print(f"\n✓ Migration completed!")
        print(f"  Migrated: {migrated_count} schedules")
        print(f"  Skipped: {skipped_count} lines")

        # Backup old file and remove original
        backup_file = old_file + ".backup"
        try:
            os.rename(old_file, backup_file)
            print(f"  Old file backed up to: {backup_file}")
        except Exception as backup_error:
            print(f"⚠ Warning: Could not backup file: {backup_error}")
            # Try to remove the original file anyway
            try:
                os.remove(old_file)
                print(f"  Original file removed (backup failed)")
            except Exception as remove_error:
                print(f"✗ Error: Could not remove original file: {remove_error}")
                raise

    except Exception as e:
        print(f"✗ Migration failed: {e}")

if __name__ == "__main__":
    migrate_schedules()

