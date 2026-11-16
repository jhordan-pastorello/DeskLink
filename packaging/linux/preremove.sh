#!/bin/sh
# Antes de remover (nao em upgrade): desabilita e para o servico.
set +e
case "$1" in
    remove|0|purge)
        systemctl --global disable desklink.service >/dev/null 2>&1
        for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
            systemctl --user -M "${uid}@" stop desklink.service >/dev/null 2>&1
        done
        ;;
esac
exit 0
