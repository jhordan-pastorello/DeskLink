#!/bin/bash
# Compartilhamento SMB opcional para troca de arquivos com o Windows.
# Uso: sudo ./tools/setup-samba.sh   (a senha SMB e pedida no terminal)
set -e

SMB_USER="${SUDO_USER:-$USER}"
SHARE_DIR="${SHARE_DIR:-$(getent passwd "$SMB_USER" | cut -d: -f6)/Transferencia}"

if [ -z "${SMB_PASS:-}" ]; then
    read -r -s -p "Senha SMB para o usuario $SMB_USER: " SMB_PASS; echo
fi

mkdir -p "$SHARE_DIR"
chown "$SMB_USER:$SMB_USER" "$SHARE_DIR"

if ! command -v smbd >/dev/null 2>&1; then
    if command -v apt-get >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get install -y samba
    elif command -v dnf >/dev/null; then dnf install -y samba
    else echo "instale o samba manualmente"; exit 1; fi
fi

if ! grep -q "^\[Transferencia\]" /etc/samba/smb.conf; then
    cat >> /etc/samba/smb.conf <<EOC

[Transferencia]
   comment = Area de transferencia DeskLink
   path = $SHARE_DIR
   browseable = yes
   read only = no
   valid users = $SMB_USER
   create mask = 0664
   directory mask = 0775
   force user = $SMB_USER
EOC
fi

printf '%s\n%s\n' "$SMB_PASS" "$SMB_PASS" | smbpasswd -a -s "$SMB_USER"
unset SMB_PASS

SVC=smbd; systemctl list-unit-files smb.service >/dev/null 2>&1 && SVC=smb
systemctl enable --now "$SVC"
systemctl restart "$SVC"

echo "Compartilhamento pronto: \\\\<ip-do-servidor>\\Transferencia  ($SHARE_DIR)"
echo "No Windows: net use Z: \\\\<ip-do-servidor>\\Transferencia /user:$SMB_USER * /persistent:yes"
