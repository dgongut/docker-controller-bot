#!/bin/bash
git add -A
git commit -m "feat: add hierarchical navigation to /restart command" \
           -m "- Level 1: Show projects and standalone containers" \
           -m "- Level 2: Show project containers with restart/back buttons" \
           -m "- Apply single-container rule (projects with 1 container shown as standalone)" \
           -m "- Add whale emoji to containers and package emoji to messages" \
           -m "- Add translations for 8 languages" \
           -m "- Remove multi-select feature (not needed with projects)" \
           -m "- Add placeholder for whole project restart (FASE 5)" \
           -m "- Version: 4.0.0-FASE4"
git log --oneline -3

