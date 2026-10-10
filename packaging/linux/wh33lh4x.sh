#!/bin/sh
# The Flatpak's command: the window, or any module with its flags, e.g.
#     flatpak run io.github.Noctaro.WH33LH4X bridge --run-seconds 10
set -eu

# /app is read-only; tuning, settings, saved profiles and logs go to the sandbox's data folder.
export WH33LH4X_DATA="$XDG_DATA_HOME"
cd /app/share/wh33lh4x
if [ $# -eq 0 ]; then
    set -- ui
fi
exec python3 -m "$@"
