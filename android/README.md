# карточка Ai — Android-приложение

Нативная WebView-обёртка над `https://kartochka.ai/`. UI — твой сайт 1-в-1 (он же его и рисует), а нативный слой даёт:

- **0 лагов**: hardware-accelerated WebView, off-screen pre-raster, отключённый overscroll glow.
- **Без браузерных хвостов**: нет адресной строки, тулбара, бейджей «открыть в Chrome».
- **Брендовый статус-бар** `#4C29D3` (как `<meta theme-color>` сайта). При запуске splash на лавандовом фоне `#F4F2FA` с логотипом, без белой вспышки.
- **Фото-апдейт работает**: `<input type="file" accept="image/*">` на сайте триггерит нативный системный пикер + опцию «Камера». Снятый кадр пробрасывается в `/api/upload` как из браузера.
- **Back-кнопка** ведёт назад по WebView-истории; на корне закрывает приложение.
- **SSE для генерации** (`/api/generation/{gid}/stream`) работает нативно: WebView Chromium = тот же `EventSource`, что и в браузере.
- **Cookie-сессия** хранится постоянно (`CookieManager`) — пользователь не разлогинивается между запусками.
- **Внешние ссылки** (Telegram / VK / MAX из футера) открываются в системном браузере, а не подменяют контент.

## Быстрый старт

**Если ты тут впервые** → читай **[GUIDE.md](GUIDE.md)** (пошагово: установка Android Studio, создание эмулятора, первый запуск).

**Когда всё уже настроено** — одной кнопкой:

```bat
start.bat
```

Стартер сам найдёт SDK, поднимет эмулятор, соберёт APK, установит и запустит приложение.

Альтернатива — кнопкой **Run ▶** в Android Studio.

## Сборка APK из командной строки

Если стоит Gradle на PATH (`gradle -v` отрабатывает):

```bat
build_apk.bat
```

Готовый APK будет в `app\build\outputs\apk\debug\app-debug.apk`. Сбросить на телефон:

```bat
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

Если Gradle не установлен — открой проект в Android Studio один раз, оно сгенерирует `gradlew.bat`, после чего командная сборка пойдёт через него.

## Что куда крутить

- **Сменить URL сайта** (например, на staging или локальный сервер): [`MainActivity.kt:292`](app/src/main/java/ai/kartochka/app/MainActivity.kt#L292) → константа `SITE_URL`.
- **Доменный whitelist** (что считается «своим» и не уводит в браузер): [`KartochkaWebClient.kt`](app/src/main/java/ai/kartochka/app/KartochkaWebClient.kt) → метод `handleUrl`. Сейчас в-апп остаётся только `kartochka.ai` и поддомены.
- **Цвет статус-бара / фона**: [`res/values/colors.xml`](app/src/main/res/values/colors.xml).
- **Иконка лаунчера**: заменить `app/src/main/res/mipmap-xxxhdpi/ic_launcher_foreground.png` (сейчас стоит твой `logo.png`). Фон — градиент в [`drawable/ic_launcher_background.xml`](app/src/main/res/drawable/ic_launcher_background.xml).
- **Splash-логотип**: `app/src/main/res/drawable/splash_logo.png` (тоже `logo.png` сейчас).

## Релиз в Google Play

Когда дойдёшь до релиза — нужен release keystore и подпись:

```bat
keytool -genkey -v -keystore kartochka-release.keystore -alias kartochka -keyalg RSA -keysize 2048 -validity 10000
```

И в `app/build.gradle.kts` добавить:

```kotlin
android {
    signingConfigs {
        create("release") {
            storeFile = file("../kartochka-release.keystore")
            storePassword = "..."
            keyAlias = "kartochka"
            keyPassword = "..."
        }
    }
    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("release")
            ...
        }
    }
}
```

`*.keystore` уже в `.gitignore`.

## Проверка перформанса

- Включи режим **GPU Profiling** в Developer Options телефона — все полоски должны быть ниже зелёной линии. На Snapdragon 700+ серии должно быть стабильно 60 FPS даже на анимациях блобов.
- Если есть лаги на старых девайсах (Android 7-8 + Chromium WebView старой версии) — попроси юзера обновить **Android System WebView** через Play Store. Это решает 90% случаев.

## Что НЕ трогать без причины

- `KartochkaApp.kt` — задаёт `WebView.setDataDirectorySuffix` для безопасной мультипроцессности.
- `mixedContentMode = MIXED_CONTENT_NEVER_ALLOW` — сайт на HTTPS, любая HTTP-вставка = атака. Не ослаблять.
- `network_security_config.xml` — открыт cleartext только для loopback и LAN (для разработки). На прод-устройстве это не активируется.

## Что осталось «вне коробки» (если захочешь — добавим)

- Push-уведомления (Firebase): сейчас нет.
- Биометрия для разлока: не нужна, сессия по cookie.
- Share extension: чтобы из галереи «отправить → карточка Ai» — пока нет, добавляется ~50 строк.
- Авто-обновление через Play (in-app update) — стандартный паттерн, добавляется отдельной либой.

---

**Что НЕ менялось:**
- `C:\Users\Alex\Downloads\WBnator_site` — серверный код. Стоит как стоял.
- `C:\Users\Alex\WBnator_mobile` — старый Godot-проект. Не трогал.
