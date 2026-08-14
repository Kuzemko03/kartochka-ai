@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem  run_pc.bat — открывает kartochka.ai в безрамочном Chromium-окне
rem  с мобильным User-Agent. Это аналог "F5" из Годота.
rem
rem  Прод по умолчанию. Локальный сервер: run_pc_local.bat
rem ============================================================

set "URL=%~1"
if "%URL%"=="" set "URL=https://kartochka.ai/"

set "UA=Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 KartochkaApp/1.0-PC"
set "PROFILE=%LOCALAPPDATA%\KartochkaPCTest"

set "BROWSER="
for %%P in (
  "C:\Program Files\Google\Chrome\Application\chrome.exe"
  "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
  "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
  "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
  "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
) do (
  if exist %%P if "!BROWSER!"=="" set "BROWSER=%%~P"
)

if "%BROWSER%"=="" (
    echo [!] Не нашёл ни Chrome, ни Edge.
    echo     Поставь Google Chrome: https://www.google.com/chrome/
    exit /b 1
)

echo Запускаю %URL% через "%BROWSER%"
start "" "%BROWSER%" ^
    --app="%URL%" ^
    --user-agent="%UA%" ^
    --window-size=412,892 ^
    --window-position=200,80 ^
    --user-data-dir="%PROFILE%" ^
    --no-first-run ^
    --no-default-browser-check ^
    --disable-features=Translate
