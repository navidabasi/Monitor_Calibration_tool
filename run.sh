#!/bin/sh
# Start Monitor calibration on macOS or Linux. Works on a machine without Python.
#
#   curl -LsSf https://raw.githubusercontent.com/navidabasi/monitor-calibration/main/run.sh | sh
#
# If uv isn't installed, it installs it first (from astral.sh, into your home
# folder, no sudo needed). uv then downloads its own private copy of Python if
# needed and starts the app. Nothing else on the system is changed.
#
# MONCAL_SOURCE overrides where the app comes from (default: PyPI), for example
#   MONCAL_SOURCE=git+https://github.com/navidabasi/monitor-calibration sh run.sh

set -eu
SOURCE="${MONCAL_SOURCE:-monitor-calibration}"

find_uvx() {
    if command -v uvx >/dev/null 2>&1; then
        command -v uvx
        return 0
    fi
    for dir in "${XDG_BIN_HOME:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -n "$dir" ] && [ -x "$dir/uvx" ]; then
            echo "$dir/uvx"
            return 0
        fi
    done
    return 1
}

if ! UVX="$(find_uvx)"; then
    echo "Installing uv, a small tool that runs Python apps (from astral.sh)..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo "Please install curl or wget first, then run this again." >&2
        exit 1
    fi
    if ! UVX="$(find_uvx)"; then
        echo "uv was installed but uvx was not found. Open a new terminal and run: uvx monitor-calibration" >&2
        exit 1
    fi
fi

# Intel Macs: the RAW reader (rawpy) only has ready-made builds up to Python 3.13 there.
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "x86_64" ]; then
    exec "$UVX" --python 3.13 --from "$SOURCE" monitor-calibration "$@"
fi
exec "$UVX" --from "$SOURCE" monitor-calibration "$@"
