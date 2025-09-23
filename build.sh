#!/usr/bin/env bash
# exit on error
set -o errexit

echo "--- Installing Python dependencies only ---"
pip install -r requirements.txt
echo "--- Build complete ---"
