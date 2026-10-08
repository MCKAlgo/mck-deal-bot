@echo off
title DealBot 24/7 - Flipkart Amazon Meesho
cd /d "%~dp0"
echo ============================================
echo   DealBot - starting...
echo ============================================
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found! Install from https://python.org
    echo and tick "Add Python to PATH" during install.
    pause
    exit /b
)
pip install -q requests beautifulsoup4 lxml
python bot.py
pause
