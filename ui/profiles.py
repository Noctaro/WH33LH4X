"""Profiles: named sets of tuning values, applied by writing them into user-tune.json."""

import json
import os
import re

import paths
from live_tune import DEFAULTS, USER_TUNE

PROFILE_DIR = os.path.join(paths.APP, "profiles")   # the built-in ones
USER_DIR = os.path.join(paths.DATA, "profiles")     # where saves go; the same in a checkout
TUNE = USER_TUNE

# Button assignments belong to the wheel, not to a profile, so switching leaves them alone.
KEYS = [key for key in DEFAULTS if not key.startswith("btn_")]


class Profile(object):
    def __init__(self, name, path, values, notes=""):
        self.name = name
        self.path = path
        self.values = values
        self.notes = notes

    def complete(self):
        """Every key, defaults filled in, so a switch leaves nothing behind."""
        return {key: self.values.get(key, DEFAULTS[key]) for key in KEYS}

    def matches(self, values):
        return all(_same(values.get(key, DEFAULTS[key]), value)
                   for key, value in self.complete().items())

    def save(self, values):
        self.values = {key: values.get(key, DEFAULTS[key]) for key in KEYS}
        if not writable(os.path.dirname(self.path)):
            # A built-in profile in a read-only install is saved as a copy that shadows it.
            self.path = os.path.join(USER_DIR, os.path.basename(self.path))
        _write_json(self.path, {"name": self.name, "notes": self.notes, "tune": self.values})


def _same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(a - b) < 1e-6
    return a == b


def writable(folder):
    return os.access(folder, os.W_OK)


def _write_json(path, data):
    """Write through a temporary file, so a reader never sees half a file."""
    temp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(temp, path)


def load_all(*directories):
    """Every readable profile, Default first, then by name; a later folder's file wins."""
    found = {}
    for directory in directories or (PROFILE_DIR, USER_DIR):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for filename in names:
            if not filename.endswith(".json"):
                continue
            path = os.path.join(directory, filename)
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                values = {key: value for key, value in data.get("tune", {}).items()
                          if key in KEYS}
                found[filename] = Profile(str(data["name"]), path, values,
                                          data.get("notes", ""))
            except (OSError, ValueError, KeyError, AttributeError):
                continue
    return sorted(found.values(), key=lambda p: (p.name != "Default", p.name.lower()))


def create(name, values, directory=USER_DIR):
    """A new profile file for name. Raises ValueError if the name is empty or taken."""
    name = name.strip()
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not slug:
        raise ValueError("a profile needs a name")
    if any(p.name.lower() == name.lower() for p in load_all(PROFILE_DIR, directory)):
        raise ValueError("a profile called %s already exists" % name)
    path = os.path.join(directory, slug + ".json")
    if os.path.exists(path):
        raise ValueError("%s already exists" % os.path.basename(path))
    profile = Profile(name, path, {})
    profile.save(values)
    return profile


def read_tune(path=TUNE):
    """The tuning file as a dict, or {} when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def current_values(path=TUNE):
    """The values the bridge is using: the tuning file over the defaults."""
    values = dict(DEFAULTS)
    values.update({key: value for key, value in read_tune(path).items() if key in DEFAULTS})
    return values


def write_tune(values, path=TUNE):
    """Merge values into the tuning file, keeping keys this window does not manage."""
    data = read_tune(path)
    data.update(values)
    _write_json(path, data)
