#!/bin/sh
# Startskript des Containers.
#
# Docker legt Bind-Mount-Verzeichnisse (Unraid: /mnt/user/appdata/...) als
# root an. Startet der Container als root (Default), übereignen wir das
# Daten-Verzeichnis einmalig dem App-Benutzer und geben die Rechte dann ab.
# Mit --user gestartete Container überspringen das und laufen direkt los.
set -e

if [ "$(id -u)" = "0" ]; then
    if [ "$(stat -c %u /app/data)" != "1000" ]; then
        chown -R ubuch:ubuch /app/data
    fi
    exec su -s /bin/sh ubuch -c 'exec /usr/local/bin/python /app/main.py'
fi

exec python main.py
