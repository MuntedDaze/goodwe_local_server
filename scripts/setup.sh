#!/usr/bin/env bash
# Prepare a Linux development or Codex cloud environment. Run it from any
# directory; it can be repeated safely. The environment is created in .venv.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Home Assistant 2026.11 requires Python 3.14.2 or later.
supported='import sys; sys.exit(sys.version_info < (3, 14, 2))'

if ! .venv/bin/python -c "$supported" 2>/dev/null; then
    if python3.14 -c "$supported" 2>/dev/null; then
        python3.14 -m venv --clear .venv
    else
        # uv downloads a suitable interpreter when the image does not have one.
        if command -v uv >/dev/null; then
            uv=uv
        else
            python3 -m venv --clear .venv-uv
            .venv-uv/bin/python -m pip install uv
            uv=.venv-uv/bin/uv
        fi
        "$uv" venv --clear --seed --python ">=3.14.2,<3.15" .venv
    fi
fi

.venv/bin/python -m pip install -r requirements-dev.txt
echo "Ready. Run: .venv/bin/python -m pytest"
