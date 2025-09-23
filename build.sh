#!/usr/bin/env bash
# exit on error
set -o errexit

# Install pip dependencies
pip install -r requirements.txt

# Set a persistent location for Playwright's browser cache within the project directory
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/.playwright

# Explicitly run playwright's install command using the python from the venv
python -m playwright install --with-deps chromium
