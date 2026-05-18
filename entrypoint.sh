#!/bin/bash
set -e

echo "=== Autoresponder Agent Entrypoint ==="

# Decode cookies if COOKIES_BASE64 is provided in environment variables
if [ -n "$COOKIES_BASE64" ]; then
    echo "Decoding COOKIES_BASE64 env var to cookies.json..."
    python -c '
import os, base64, json
val = os.environ.get("COOKIES_BASE64", "")
try:
    decoded = base64.b64decode(val.strip()).decode("utf-8")
    # Verify valid JSON
    json.loads(decoded)
    with open("/app/cookies.json", "w", encoding="utf-8") as f:
        f.write(decoded)
    print("  Successfully decoded and verified cookies.json!")
except Exception as e:
    print(f"  Warning: failed to decode cookies: {e}")
'
fi

# Clean up stale locks or display processes
rm -f /tmp/.X99-lock

# Start Xvfb (Virtual Framebuffer) in background
echo "Starting virtual frame buffer Xvfb on display :99..."
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Wait a moment for Xvfb to boot up
sleep 3

# Check if Xvfb is running
if ps -p $XVFB_PID > /dev/null; then
    echo "Xvfb is running successfully with PID $XVFB_PID"
else
    echo "Warning: Xvfb failed to start. Browser automation may fail if non-headless."
fi

echo "=== Starting Agentic AI Framework Server ==="
exec python app/agent/agent.py
