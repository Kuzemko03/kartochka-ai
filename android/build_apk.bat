@echo off
setlocal
cd /d "%~dp0"

if exist gradlew.bat (
    call gradlew.bat assembleDebug
) else (
    where gradle >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [!] Gradle не найден на PATH и gradlew.bat ещё не сгенерирован.
        echo     Открой проект один раз в Android Studio - он сам создаст gradlew,
        echo     либо установи Gradle 8.7+ ^(https://gradle.org/install/^) и перезапусти.
        echo.
        exit /b 1
    )
    call gradle assembleDebug
)

if errorlevel 1 (
    echo.
    echo [x] Сборка упала. Лог выше.
    exit /b 1
)

echo.
echo [OK] Готово: app\build\outputs\apk\debug\app-debug.apk
echo      Установка на подключённый телефон:
echo      adb install -r app\build\outputs\apk\debug\app-debug.apk
