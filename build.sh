#!/usr/bin/env bash
# exit on error
set -o errexit

# Install pip dependencies
pip install -r requirements.txt

# Set a persistent location inside the source directory for Playwright's browser cache
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright

# Install the browser and its dependencies
echo "Installing Playwright browsers to $PLAYWRIGHT_BROWSERS_PATH..."
python -m playwright install --with-deps chromium

# Grant execute permissions to the browser directory. THIS IS A CRITICAL STEP.
echo "Setting permissions for browser executables..."
chmod -R 755 $PLAYWRIGHT_BROWSERS_PATH

# (Optional) List the contents to verify in the logs
ls -laR $PLAYWRIGHT_BROWSERS_PATH

echo "Build script finished."
