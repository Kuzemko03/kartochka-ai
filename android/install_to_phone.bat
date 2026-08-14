@echo off
setlocal
cd /d "%~dp0"

set APK=app\build\outputs\apk\debug\app-debug.apk
if not exist "%APK%" (
    echo [!] APK не собран. Сначала: build_apk.bat
    exit /b 1
)

where adb >nul 2>nul
if errorlevel 1 (
    echo [!] adb не найден на PATH.
    echo     Это идёт с Android SDK Platform-Tools.
    echo     Скачай: https://developer.android.com/tools/releases/platform-tools
    exit /b 1
)

adb devices
adb install -r "%APK%"
if errorlevel 1 (
    echo [x] Установка не удалась. Проверь, что телефон подключён и USB-debug включён.
    exit /b 1
)

echo.
echo [OK] Установлено. Запусти "карточка Ai" из лаунчера.
