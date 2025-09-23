#!/usr/bin/env bash
# exit on error
set -o errexit

# Update the package manager and install a system-wide version of Chromium.
# This is the definitive fix for the browser executable issue.
echo "Updating apt and installing system-wide Chromium..."
apt-get update
apt-get install -y chromium-browser

# Install pip dependencies
echo "Installing pip requirements..."
pip install -r requirements.txt

echo "Build script finished successfully."
