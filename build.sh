#!/usr/bin/env bash
# exit on error
set -o errexit

# --- 1. Install All System Dependencies for Headless Chromium ---
echo "Updating apt and installing a comprehensive list of browser dependencies..."
apt-get update
apt-get install -y \
    gconf-service \
    libasound2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libgcc1 \
    libgconf-2-4 \
    libgdk-pixbuf2.0-0 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libstdc++6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    ca-certificates \
    fonts-liberation \
    libappindicator1 \
    libnss3 \
    lsb-release \
    xdg-utils \
    wget \
    unzip

# --- 2. Install Python Dependencies ---
echo "Installing pip requirements..."
pip install -r requirements.txt

# --- 3. Download and Prepare Self-Contained Browser ---
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

# Grant execute permissions to the entire browser directory. THIS IS CRITICAL.
echo "Setting permissions for browser executable..."
chmod -R 755 /opt/render/project/src/bin/chrome-linux

# Clean up the downloaded zip file
rm /tmp/chrome.zip

# Verify the final executable exists and has the correct permissions
echo "Verifying installation..."
ls -la /opt/render/project/src/bin/chrome-linux/chrome

echo "Build script finished successfully."
