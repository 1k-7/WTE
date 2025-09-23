#!/usr/bin/env bash
# exit on error
set -o errexit

# Install pip dependencies
pip install -r requirements.txt

# Explicitly run playwright's install command using the python from the venv
python -m playwright install --with-deps chromium
