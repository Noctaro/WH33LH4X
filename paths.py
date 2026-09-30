"""Where the app's files are and where it writes: one folder, unless WH33LH4X_DATA is set."""

import os

APP = os.path.dirname(os.path.abspath(__file__))
# The Flatpak's app folder is read-only; its launcher points this at the sandbox's data folder.
DATA = os.environ.get("WH33LH4X_DATA") or APP
LOGS = os.path.join(DATA, "logs")
