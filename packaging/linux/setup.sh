#!/bin/sh
# One-time Linux host setup for WH33LH4X: access to the wheel and to /dev/uinput with no root.
#
#     sudo sh setup.sh                install
#     sudo sh setup.sh --remove       undo
#     sh setup.sh --print-rule        print the udev rule it installs
set -eu

GROUP=wh33lh4x
RULE=70-wh33lh4x.rules
RULES_DIR=/etc/udev/rules.d
MODULES=/etc/modules-load.d/wh33lh4x.conf
SYSFS=/sys/bus/usb/devices
TARGET_USER=${SUDO_USER:-}

# One file, so a release can ship it next to the Flatpak as a single download.
rule() {
    cat <<'EOF'
# WH33LH4X: access to the HORI wheel in Xbox mode and to /dev/uinput, with no root.
# uaccess covers the desktop session; the group covers SSH and headless sessions.
# Sorts before 73-seat-late.rules, which is what applies uaccess.

# With no driver bound Linux suspends the wheel after 2 s, and every resume runs a calibration
# sweep that ignores force, so it is kept awake.
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0f0d", ATTR{idProduct}=="015c", \
  TAG+="uaccess", GROUP="wh33lh4x", MODE="0660", ATTR{power/control}="on"

KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", GROUP="wh33lh4x", MODE="0660", \
  OPTIONS+="static_node=uinput"

# The virtual wheel's event node is root:input with no desktop login, measured 2026-09-30.
KERNEL=="event*", SUBSYSTEM=="input", ATTRS{name}=="WH33LH4X virtual wheel", \
  GROUP="wh33lh4x", MODE="0660"
EOF
}

if [ "${1:-}" = "--print-rule" ]; then
    rule
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo sh $0 $*" >&2
    exit 1
fi

reload_rules() {
    udevadm control --reload
    udevadm trigger --subsystem-match=usb --attr-match=idVendor=0f0d || true
    udevadm trigger --subsystem-match=misc --sysname-match=uinput || true
    udevadm settle || true
}

wheel_dir() {
    for dir in "$SYSFS"/*; do
        if [ ! -r "$dir/idVendor" ] || [ ! -r "$dir/idProduct" ]; then
            continue
        fi
        if [ "$(cat "$dir/idVendor")" = 0f0d ] && [ "$(cat "$dir/idProduct")" = 015c ]; then
            echo "$dir"
            return
        fi
    done
}

if [ "${1:-}" = "--remove" ]; then
    rm -f "$RULES_DIR/$RULE" "$MODULES"
    reload_rules
    if getent group "$GROUP" > /dev/null; then
        groupdel "$GROUP"
    fi
    echo "Removed the udev rule, the uinput autoload and the $GROUP group."
    exit 0
fi

if [ -n "${1:-}" ]; then
    echo "Unknown option: $1" >&2
    exit 1
fi

if ! getent group "$GROUP" > /dev/null; then
    groupadd --system "$GROUP"
fi
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ]; then
    usermod -a -G "$GROUP" "$TARGET_USER"
fi

rule > "$RULES_DIR/$RULE"
chmod 0644 "$RULES_DIR/$RULE"
echo uinput > "$MODULES"
modprobe uinput || true
reload_rules

echo "Installed $RULES_DIR/$RULE and the $GROUP group."
echo
echo "Still to do:"
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ]; then
    echo "  - Log out and back in, so $TARGET_USER's $GROUP membership applies."
else
    echo "  - Add your user to the group: sudo usermod -a -G $GROUP <user>, then log in again."
fi
wheel=$(wheel_dir)
if [ -z "$wheel" ]; then
    echo "  - Plug the wheel in, in Xbox mode (long-press PROFILE)."
elif [ "$(cat "$wheel/power/control")" != on ]; then
    echo "  - Replug the wheel: it is still allowed to suspend."
else
    echo "  - Let the wheel finish its calibration sweep if it started one."
fi
if [ ! -e /dev/uinput ]; then
    echo "  - /dev/uinput is missing: this kernel has no uinput module."
fi
# A later rule that sets the group or mode of the same node wins over this one.
for other in "$RULES_DIR"/*.rules; do
    if [ ! -e "$other" ] || [ "$other" = "$RULES_DIR/$RULE" ]; then
        continue
    fi
    if grep -q -e uinput -e 015c "$other"; then
        echo "  - $other also covers uinput or the wheel; remove it if access is still missing."
    fi
done
