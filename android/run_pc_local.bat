@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem  run_pc_local.bat — то же самое, но грузит твой локальный
rem  FastAPI (server.py из WBnator_site). Если сервер не поднят —
rem  поднимает его.
rem ============================================================

set "SITE_DIR=C:\Users\Alex\Downloads\WBnator_site"

rem Найдём первый занятый порт в 8000..8019 — на нём слушает уже запущенный сервер.
set "PORT="
for /l %%P in (8000,1,8019) do (
    if not defined PORT (
        netstat -ano -p TCP | findstr /R /C:":%%P .*LISTENING" >nul 2>nul
        if not errorlevel 1 set "PORT=%%P"
    )
)

if not defined PORT (
    echo [i] FastAPI не запущен — поднимаю server.py...
    if not exist "%SITE_DIR%\server.py" (
        echo [!] %SITE_DIR%\server.py не найден.
        exit /b 1
    )
    pushd "%SITE_DIR%"
    start "WBnator FastAPI" cmd /k python server.py
    popd
    echo [i] Жду 4 секунды, пока сервер поднимется...
    timeout /t 4 /nobreak >nul
    rem Перепроверяем порт.
    for /l %%P in (8000,1,8019) do (
        if not defined PORT (
            netstat -ano -p TCP | findstr /R /C:":%%P .*LISTENING" >nul 2>nul
            if not errorlevel 1 set "PORT=%%P"
        )
    )
)

if not defined PORT (
    echo [!] Не нашёл слушающий порт FastAPI. Запусти server.py вручную.
    exit /b 1
)

echo [i] FastAPI на порту %PORT%
call "%~dp0run_pc.bat" "http://127.0.0.1:%PORT%/"
