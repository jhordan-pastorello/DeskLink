#!/bin/bash
# Configura /dev/uinput para quem NAO instalou pelo pacote .deb/.rpm.
# Uso: sudo ./tools/setup-uinput.sh
set -e
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${SUDO_USER:-$USER}"

cp "$here/../packaging/linux/60-desklink-uinput.rules" /etc/udev/rules.d/60-desklink-uinput.rules
cp "$here/../packaging/linux/desklink-modules.conf" /etc/modules-load.d/desklink-uinput.conf
modprobe uinput || true
udevadm control --reload-rules
udevadm trigger --subsystem-match=misc --action=add || true
sleep 1
getent group input >/dev/null && usermod -aG input "$target"
chgrp input /dev/uinput && chmod 0660 /dev/uinput
command -v setfacl >/dev/null && setfacl -m "u:$target:rw" /dev/uinput || true
ls -l /dev/uinput
