#!/usr/bin/env bash
# exit on error
set -o errexit

# Install pip dependencies
pip install -r requirements.txt

# Set a persistent location for Playwright's browser cache
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/.playwright

# Install the browser and its dependencies
echo "Installing Playwright browsers..."
python -m playwright install --with-deps chromium

# Grant execute permissions to the browser directory. THIS IS THE CRITICAL FIX.
echo "Setting permissions for browser executables..."
chmod -R 755 $PLAYWRIGHT_BROWSERS_PATH
echo "Build script finished."
