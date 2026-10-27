#!/usr/bin/env bash
set -euo pipefail

ARDUCAM_INSTALLER_URL="https://github.com/ArduCAM/Arducam-Pivariety-V4L2-Driver/releases/download/install_script/install_pivariety_pkgs.sh"
ARDUCAM_INSTALLER_SHA256="19fa43e023ebd17c1b13c2925775eb6830395c249bec94fe441c709d7e655d07"
BOOT_CONFIG="/boot/firmware/config.txt"
CAMERA_PORT="${1:-}"
LOG_ROOT="/var/log/bapd8/rpi-b0267-camera-stack"

usage() {
    echo "Usage: sudo $0 [cam1|cam0]" >&2
    echo "Default: Pi 5 enables CAM0 and CAM1; Pi 4 enables CAM1." >&2
}

if [ "$#" -gt 1 ]; then
    usage
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root with sudo." >&2
    exit 1
fi

case "$CAMERA_PORT" in
    ""|cam0|cam1) ;;
    *)
        usage
        exit 1
        ;;
esac

for required_command in bash curl wget unzip dpkg-architecture apt-get sha256sum sed grep tee awk sudo; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        echo "Missing required command: $required_command" >&2
        exit 1
    fi
done

if [ ! -r /etc/os-release ]; then
    echo "Missing /etc/os-release." >&2
    exit 1
fi
. /etc/os-release
if [ "${VERSION_CODENAME:-}" != "bookworm" ]; then
    echo "This installer requires Raspberry Pi OS Bookworm." >&2
    exit 1
fi
if [ "$(dpkg --print-architecture)" != "arm64" ]; then
    echo "This installer requires the arm64 Raspberry Pi OS image." >&2
    exit 1
fi
if [ ! -r /proc/device-tree/model ]; then
    echo "Unable to read the Raspberry Pi model." >&2
    exit 1
fi

RPI_MODEL=$(tr -d '\000' </proc/device-tree/model)
case "$RPI_MODEL" in
    Raspberry\ Pi\ 5*)
        case "$CAMERA_PORT" in
            cam0) PIVARIETY_OVERLAYS="dtoverlay=arducam-pivariety,cam0" ;;
            cam1) PIVARIETY_OVERLAYS="dtoverlay=arducam-pivariety" ;;
            "") PIVARIETY_OVERLAYS=$'dtoverlay=arducam-pivariety\ndtoverlay=arducam-pivariety,cam0' ;;
        esac
        ;;
    Raspberry\ Pi\ 4*)
        if [ -n "$CAMERA_PORT" ] && [ "$CAMERA_PORT" != "cam1" ]; then
            echo "Raspberry Pi 4 does not support the cam0 selection used by this installer." >&2
            exit 1
        fi
        PIVARIETY_OVERLAYS="dtoverlay=arducam-pivariety"
        ;;
    *)
        echo "Unsupported Raspberry Pi model: $RPI_MODEL" >&2
        exit 1
        ;;
esac

if [ ! -f "$BOOT_CONFIG" ]; then
    echo "Missing boot configuration: $BOOT_CONFIG" >&2
    exit 1
fi
AUTO_DETECT_COUNT=$(grep -c '^camera_auto_detect=' "$BOOT_CONFIG" || true)
if [ "$AUTO_DETECT_COUNT" -ne 1 ]; then
    echo "Expected exactly one camera_auto_detect setting in $BOOT_CONFIG." >&2
    exit 1
fi
if ! grep -Fxq '[all]' "$BOOT_CONFIG"; then
    echo "Missing [all] section in $BOOT_CONFIG." >&2
    exit 1
fi
# Newline-separated patterns allow either managed overlay in Pi 5 dual-port mode.
if grep '^dtoverlay=arducam-pivariety' "$BOOT_CONFIG" | grep -Fvxq "$PIVARIETY_OVERLAYS"; then
    echo "A conflicting Pivariety overlay is already configured in $BOOT_CONFIG." >&2
    exit 1
fi

RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
LOG_DIR="$LOG_ROOT/$RUN_ID"
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
install -d -m 0755 "$LOG_DIR"

printf '%s\n' "$RPI_MODEL" >"$LOG_DIR/model.txt"
cp /etc/os-release "$LOG_DIR/os-release.txt"
uname -a >"$LOG_DIR/uname.txt"
cp "$BOOT_CONFIG" "$LOG_DIR/config.before.txt"
sha256sum "$BOOT_CONFIG" >"$LOG_DIR/config.before.sha256"
dpkg-query -W | awk '$1 ~ /^(libcamera|rpicam-apps|python3-libcamera|python3-picamera2|libpisp)/' >"$LOG_DIR/packages.before.txt"

INSTALLER_PATH="$WORK_DIR/install_pivariety_pkgs.sh"
curl --fail --location --silent --show-error --output "$INSTALLER_PATH" "$ARDUCAM_INSTALLER_URL"
printf '%s  %s\n' "$ARDUCAM_INSTALLER_SHA256" "$INSTALLER_PATH" | sha256sum --check -
install -m 0644 "$INSTALLER_PATH" "$LOG_DIR/install_pivariety_pkgs.sh"
printf '%s  %s\n' "$ARDUCAM_INSTALLER_SHA256" "install_pivariety_pkgs.sh" >"$LOG_DIR/install_pivariety_pkgs.sha256"
chmod 0755 "$INSTALLER_PATH"

cd "$WORK_DIR"
bash "$INSTALLER_PATH" -p libcamera_dev 2>&1 | tee "$LOG_DIR/libcamera-dev-install.log"
bash "$INSTALLER_PATH" -p libcamera_apps 2>&1 | tee "$LOG_DIR/rpicam-apps-install.log"

# Place both managed settings in an explicit unconditional section. Merely finding
# an earlier [all] does not make an append after a later model filter unconditional.
awk -v overlays="$PIVARIETY_OVERLAYS" '
    !/^camera_auto_detect=/ && !/^dtoverlay=arducam-pivariety/ { print }
    END { print ""; print "[all]"; print "camera_auto_detect=0"; print overlays }
' "$BOOT_CONFIG" >"$WORK_DIR/config.updated.txt"
cat "$WORK_DIR/config.updated.txt" >"$BOOT_CONFIG"

cp "$BOOT_CONFIG" "$LOG_DIR/config.after.txt"
sha256sum "$BOOT_CONFIG" >"$LOG_DIR/config.after.sha256"
dpkg-query -W | awk '$1 ~ /^(libcamera|rpicam-apps|python3-libcamera|python3-picamera2|libpisp)/' >"$LOG_DIR/packages.after.txt"
rpicam-still --version >"$LOG_DIR/rpicam-version.txt" 2>&1

sync
echo "Arducam B0267 camera stack installation completed."
echo "Installation record: $LOG_DIR"
echo "Rebooting now to activate the camera stack."
systemctl reboot
