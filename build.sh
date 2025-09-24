#!/usr/bin/env bash
# exit on error
set -o errexit

echo "--- Starting Render Native Chrome Installation ---"

# The directory for Render's persistent cache
STORAGE_DIR=/opt/render/project/.render

# Install Python dependencies first
echo "...Installing pip requirements"
pip install -r requirements.txt

# Check if Chrome is already installed in the cache
if [[ ! -d $STORAGE_DIR/chrome ]]; then
  echo "...Downloading and installing Google Chrome"
  # Create the directory
  mkdir -p $STORAGE_DIR/chrome
  cd $STORAGE_DIR/chrome

  # Download the official .deb package
  wget -P ./ https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb

  # Unpack the .deb file using ar and tar, which is more reliable in this environment
  ar x google-chrome-stable_current_amd64.deb
  tar -xf data.tar.xz

  # Clean up the downloaded and temporary files
  rm google-chrome-stable_current_amd64.deb data.tar.xz control.tar.xz debian-binary

  # Ensure the main chrome executable has the right permissions
  chmod +x ./opt/google/chrome/google-chrome

  echo "...Chrome installed successfully into persistent storage."
  cd $HOME/project/src # IMPORTANT: Return to the source directory
else
  echo "...Using Google Chrome from cache"
fi

# (FOR DEBUGGING) Print the Chrome version to the deploy logs
echo "Verifying Chrome installation..."
/opt/render/project/.render/chrome/opt/google/chrome/google-chrome --version

echo "--- Build script finished ---"
