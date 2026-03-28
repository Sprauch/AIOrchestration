#!/usr/bin/env bash
# Set up the agents virtual environment.
# Run from the repo root: bash agents/setup_venv.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Find Python >= 3.11
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(sys.version_info[:2] >= (3, 11))')
        if [ "$version" = "True" ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python >= 3.11 is required. Found: $(python3 --version 2>&1)"
    exit 1
fi

echo "=== Setting up agents virtual environment ==="
echo "Using: $PYTHON ($($PYTHON --version))"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating venv at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Upgrade pip
pip install --upgrade pip setuptools -q

# Install the package in editable mode with dev dependencies
pip install -e "$REPO_ROOT[dev]" -q

# Add repo root to Python path as fallback
SITE_PACKAGES="$(python -c 'import site; print(site.getsitepackages()[0])')"
echo "$REPO_ROOT" > "$SITE_PACKAGES/agent-orchestrator.pth"

echo ""
echo "=== Done ==="
echo "Activate with:  source agents/.venv/bin/activate"
echo "Run agents:     agent-orchestrator"
echo "Run tests:      pytest tests/ -v"
