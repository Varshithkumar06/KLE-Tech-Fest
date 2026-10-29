#!/usr/bin/env bash
# Package Estimo for Azure App Service Deployment (Linux / Cloud Shell)
set -e

DEST="$HOME/app.zip"
rm -f "$DEST"

echo "Creating deployment package at $DEST ..."
zip -r "$DEST" app.py wsgi.py run.py requirements.txt estimo/ ml/ pipeline_app/ \
    -x "*__pycache__*" "*.pyc" "*.log" "*.pid" "*.ipynb" "ml/datasets/*"

echo "Package ready: $DEST"
