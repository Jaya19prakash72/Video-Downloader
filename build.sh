#!/usr/bin/env bash
set -euo pipefail
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# yt-dlp currently uses a JavaScript runtime for full YouTube challenge solving.
# Install Deno into the Render build environment and expose it at runtime.
if ! command -v deno >/dev/null 2>&1; then
  curl -fsSL https://deno.land/install.sh | sh -s v2.5.6
fi
export PATH="$HOME/.deno/bin:$PATH"
deno --version
