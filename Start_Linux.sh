#!/usr/bin/env bash
set -e
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
if ! command -v python3 >/dev/null; then echo 'Требуется Python 3'; exit 1; fi
if [ "$1" = "--cli" ] || ! python3 -c 'import tkinter' 2>/dev/null; then
  [ "$1" = "--cli" ] || echo 'Нет tkinter (Arch: sudo pacman -S tk, Ubuntu: sudo apt install python3-tk) — запускаю текстовое меню.'
  exec python3 pzcloud.py
fi
exec python3 pzcloud_gui.py
