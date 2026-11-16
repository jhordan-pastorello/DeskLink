#!/bin/sh
# Executado como root apos instalar o pacote .deb/.rpm.
set +e

modprobe uinput 2>/dev/null
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules 2>/dev/null
    udevadm trigger --subsystem-match=misc --action=add 2>/dev/null
fi
# o udev pode demorar; garante o grupo ate la
[ -e /dev/uinput ] && chgrp input /dev/uinput 2>/dev/null && chmod 0660 /dev/uinput 2>/dev/null

# habilita o servico (systemd --user) para todos os usuarios
if command -v systemctl >/dev/null 2>&1; then
    systemctl --global enable desklink.service >/dev/null 2>&1
    # inicia/reinicia nas sessoes ja abertas
    for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
        systemctl --user -M "${uid}@" daemon-reload >/dev/null 2>&1
        systemctl --user -M "${uid}@" restart desklink.service >/dev/null 2>&1
    done
fi

cat <<'MSG'

DeskLink instalado.
  - O servico sobe sozinho no login. Se ainda nao subiu: systemctl --user enable --now desklink
  - Firewall: libere 47810/tcp e 47811/udp na sua rede local. Exemplos:
      firewalld: sudo firewall-cmd --permanent --add-port=47810/tcp --add-port=47811/udp && sudo firewall-cmd --reload
      ufw:       sudo ufw allow from 192.168.0.0/16 to any port 47810 proto tcp
                 sudo ufw allow from 192.168.0.0/16 to any port 47811 proto udp
  - Logs: journalctl --user -u desklink -f
MSG
exit 0
