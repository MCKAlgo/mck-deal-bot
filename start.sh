#!/bin/sh
# DealBot 24/7 start command (used by PaaS hosts / manual runs)
cd "$(dirname "$0")"
exec python3 bot.py
