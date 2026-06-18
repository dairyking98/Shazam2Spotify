#!/usr/bin/env bash
set -e

echo "=== Shazam2Spotify Installer ==="
echo

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.9+ and try again."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
REQUIRED="3.9"
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"; then
    echo "Python $PYTHON_VERSION found."
else
    echo "ERROR: Python $REQUIRED+ required (found $PYTHON_VERSION)."
    exit 1
fi

echo "Creating virtual environment..."
python3 -m venv .venv

echo "Installing dependencies..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

cat > run.sh << 'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
source .venv/bin/activate
python web_app.py "$@"
EOF
chmod +x run.sh

echo
echo "=== Done! ==="
echo
echo "Run the app with:  ./run.sh"
echo "Debug mode:        ./run.sh --debug"
echo
echo "Then open http://127.0.0.1:5000 in your browser."
