#!/usr/bin/env bash
# exit on error
set -o errexit

# Install required system packages for the script and for the browser to run
echo "Updating apt and installing wget and unzip..."
apt-get update
apt-get install -y wget unzip

# Install pip dependencies
echo "Installing pip requirements..."
pip install -r requirements.txt

# --- Definitive Browser Installation ---
# Create a predictable, local directory for the browser
mkdir -p /opt/render/project/src/bin

# Download a specific, known-good version of Chromium for Linux x64
echo "Downloading a specific Chromium build..."
wget -O /tmp/chrome.zip https://storage.googleapis.com/chrome-for-testing-public/118.0.5993.70/linux64/chrome-linux64.zip

# Unzip to our local bin directory
echo "Unpacking Chromium..."
unzip /tmp/chrome.zip -d /opt/render/project/src/bin/

# Rename for a clean, predictable path
mv /opt/render/project/src/bin/chrome-linux64 /opt/render/project/src/bin/chrome-linux

# Grant execute permissions to the browser executable. THIS IS CRITICAL.
echo "Setting permissions for browser executable..."
chmod -R 755 /opt/render/project/src/bin/chrome-linux

# Clean up the downloaded zip file
rm /tmp/chrome.zip

# Verify the final executable exists and has the correct permissions
echo "Verifying installation..."
ls -la /opt/render/project/src/bin/chrome-linux/chrome

echo "Build script finished successfully."
