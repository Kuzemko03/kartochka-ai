package ai.kartochka.app

import android.Manifest
import android.app.Activity
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Color
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContracts
import androidx.browser.customtabs.CustomTabsIntent
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewFeature
import java.io.File

class MainActivity : ComponentActivity() {

    private lateinit var webView: WebView
    private var pendingFileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingCameraUri: Uri? = null
    private var pendingChooserParams: WebChromeClient.FileChooserParams? = null
    private var pendingPermissionRequest: PermissionRequest? = null
    private var pendingCameraDirect = false
    private var contentReady = false
    private var loadingOverlay: View? = null

    private var loadRetryCount = 0
    private var lastTargetUrl: String = BuildConfig.SITE_URL
    private var showingOfflinePage = false

    private val pickerLauncher: ActivityResultLauncher<Intent> =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val callback = pendingFileCallback ?: return@registerForActivityResult
            pendingFileCallback = null
            val cameraUri = pendingCameraUri
            pendingCameraUri = null
            pendingChooserParams = null

            val uris: Array<Uri>? = if (result.resultCode != Activity.RESULT_OK) {
                null
            } else {
                // Ручное извлечение URI — НЕ через FileChooserParams.parseResult:
                // та глючит при EXTRA_ALLOW_MULTIPLE на Android 13+ photopicker
                // (URI лежат в clipData, parseResult их не достаёт).
                val raw = result.data
                val rawUris = buildList<Uri> {
                    raw?.data?.let { add(it) }
                    raw?.clipData?.let { cd ->
                        for (i in 0 until cd.itemCount) {
                            cd.getItemAt(i).uri?.let { add(it) }
                        }
                    }
                }
                val chosen: List<Uri> = when {
                    rawUris.isNotEmpty() -> rawUris
                    cameraUri != null && fileExistsAndNonEmpty(cameraUri) -> listOf(cameraUri)
                    else -> emptyList()
                }
                // photopicker URI content://media/picker/... не читается WebView
                // renderer'ом из-за scoped-storage. Копируем в свой cacheDir и
                // отдаём WebView FileProvider-URI — он гарантированно открывается.
                val copied = chosen.mapNotNull { src -> copyToCacheUri(src) }
                if (copied.isEmpty()) null else copied.toTypedArray()
            }
            callback.onReceiveValue(uris)
        }

    private fun copyToCacheUri(src: Uri): Uri? {
        return try {
            val cr = contentResolver
            val ext = when {
                cr.getType(src)?.contains("png") == true -> "png"
                cr.getType(src)?.contains("webp") == true -> "webp"
                cr.getType(src)?.contains("heic") == true -> "heic"
                cr.getType(src)?.contains("heif") == true -> "heif"
                cr.getType(src)?.contains("gif") == true -> "gif"
                else -> "jpg"
            }
            val dir = File(cacheDir, "uploads").apply { mkdirs() }
            val dst = File(dir, "pick_${System.currentTimeMillis()}_${(0..9999).random()}.$ext")
            cr.openInputStream(src)?.use { input ->
                dst.outputStream().use { output -> input.copyTo(output) }
            }
            if (dst.length() <= 0L) return null
            FileProvider.getUriForFile(this, "$packageName.fileprovider", dst)
        } catch (_: Throwable) {
            null
        }
    }

    private val cameraPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        val webPerm = pendingPermissionRequest
        pendingPermissionRequest = null
        if (webPerm != null) {
            if (granted) webPerm.grant(webPerm.resources) else webPerm.deny()
            return@registerForActivityResult
        }
        if (pendingCameraDirect) {
            pendingCameraDirect = false
            // Кнопка «Сделать фото»: дали камеру → открываем её сразу; отказали →
            // мягко падаем на галерейный чузер (юзер хотя бы выберет фото).
            if (granted) launchCameraDirect() else launchFileChooserInternal(includeCamera = false)
            return@registerForActivityResult
        }
        if (pendingFileCallback != null) {
            launchFileChooserInternal(includeCamera = granted)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        val splash = installSplashScreen()
        splash.setKeepOnScreenCondition { !contentReady }
        super.onCreate(savedInstanceState)

        // Hard cap: never block UI on splash > 2.5s, even if network stalls.
        window.decorView.postDelayed({ contentReady = true }, 2500)

        WindowCompat.setDecorFitsSystemWindows(window, true)
        window.statusBarColor = Color.parseColor("#4C29D3")
        window.navigationBarColor = Color.parseColor("#F4F2FA")
        WindowInsetsControllerCompat(window, window.decorView).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = true
        }

        webView = buildWebView()
        // Брендовый лоадер ПОВЕРХ WebView: страница тяжёлая (~670 КБ) и грузится
        // по сети не мгновенно, а splash снимается уже через 2.5с — без оверлея
        // юзер видел бы пустой светлый WebView и думал «приложение зависло».
        // Оверлей держится, пока контент реально не отрисуется (onPageCommitVisible),
        // плюс есть жёсткий backstop, чтобы он никогда не «прилип» навсегда.
        val overlay = buildLoadingOverlay()
        loadingOverlay = overlay
        val root = FrameLayout(this).apply {
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            )
            setBackgroundColor(Color.parseColor("#F4F2FA"))
            addView(webView)
            addView(overlay)
        }
        setContentView(root)

        // Backstop: если onPageCommitVisible почему-то не придёт (битый ответ,
        // экзотический WebView) — не держим лоадер дольше 15с.
        window.decorView.postDelayed({ hideLoadingOverlay() }, 15_000)

        // В debug/тестовых сборках всегда берём СВЕЖИЙ веб-код: иначе WebView
        // держит закешированные index.html/account.js, и правки (напр. соц-кнопки)
        // «не появляются» в эмуляторе, хотя в браузере уже есть. В release кеш
        // остаётся (ETag-ревалидация → быстрый старт без перекачки ~670КБ).
        if (BuildConfig.DEBUG) {
            try { webView.clearCache(true) } catch (_: Throwable) {}
        }

        if (savedInstanceState != null) {
            webView.restoreState(savedInstanceState)
        } else if (!handleOAuthRedirect(intent)) {
            // Обычный старт. Если приложение подняли по deep-link соц-входа
            // (холодный старт), handleOAuthRedirect уже загрузил финальный URL.
            loadTargetUrl(BuildConfig.SITE_URL)
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    override fun onPause() { super.onPause(); webView.onPause() }
    override fun onResume() { super.onResume(); webView.onResume() }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        // Тёплый возврат из внешнего браузера после соц-входа (обычный случай:
        // приложение уже было запущено, singleTask → сюда).
        handleOAuthRedirect(intent)
    }

    override fun onDestroy() {
        (webView.parent as? ViewGroup)?.removeView(webView)
        webView.destroy()
        super.onDestroy()
    }

    private fun buildWebView(): WebView {
        val wv = WebView(this)
        wv.layoutParams = FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT,
        )
        wv.setBackgroundColor(Color.parseColor("#F4F2FA"))
        wv.overScrollMode = View.OVER_SCROLL_NEVER
        wv.isVerticalScrollBarEnabled = false
        wv.isHorizontalScrollBarEnabled = false
        wv.isVerticalFadingEdgeEnabled = false
        wv.isHorizontalFadingEdgeEnabled = false
        wv.setLayerType(View.LAYER_TYPE_HARDWARE, null)

        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            // Полагаемся на Cache-Control с сервера: HTML/JS отдаются с
            // no-cache (мгновенная revalidation через ETag — свежие правки
            // подхватываются), а /static/assets/* — immutable на 30 дней
            // (иконки/демо-фото читаются из локального кеша без сети,
            // скролл становится мгновенным).
            cacheMode = WebSettings.LOAD_DEFAULT
            loadsImagesAutomatically = true
            mediaPlaybackRequiresUserGesture = false
            useWideViewPort = true
            loadWithOverviewMode = true
            allowFileAccess = false
            allowContentAccess = true
            javaScriptCanOpenWindowsAutomatically = false
            setSupportMultipleWindows(false)
            builtInZoomControls = false
            displayZoomControls = false
            setSupportZoom(false)
            textZoom = 100
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            userAgentString = "$userAgentString KartochkaApp/1.0"
            @Suppress("DEPRECATION")
            setRenderPriority(WebSettings.RenderPriority.HIGH)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) safeBrowsingEnabled = true
        }

        if (WebViewFeature.isFeatureSupported(WebViewFeature.FORCE_DARK)) {
            @Suppress("DEPRECATION")
            WebSettingsCompat.setForceDark(wv.settings, WebSettingsCompat.FORCE_DARK_OFF)
        }
        if (WebViewFeature.isFeatureSupported(WebViewFeature.OFF_SCREEN_PRERASTER)) {
            WebSettingsCompat.setOffscreenPreRaster(wv.settings, true)
        }

        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(wv, true)
        }

        wv.webViewClient = KartochkaWebClient(
            onExternalUrl = ::openExternal,
            onContentVisible = { contentReady = true; hideLoadingOverlay() },
            onMainFrameError = ::handleMainFrameError,
            onPageStarted = ::handlePageStarted,
        )
        wv.webChromeClient = KartochkaChromeClient(
            onChooseFile = ::onWebFileChooser,
            onPermission = ::onWebPermissionRequest,
        )
        val jsBridge = KartochkaJsBridge()
        wv.addJavascriptInterface(jsBridge, "kartochka")

        // Подстраховка: если что-то в вебе всё же дёрнет нативное скачивание
        // (<a download> / Content-Disposition) — не теряем файл, а сохраняем
        // http(s)-картинку в галерею тем же путём, что и мост saveImages.
        wv.setDownloadListener { url, _, _, _, _ ->
            if (url.startsWith("http://") || url.startsWith("https://")) {
                try {
                    jsBridge.saveImages(org.json.JSONArray().put(url).toString(), null)
                } catch (_: Throwable) { /* ignore */ }
            }
        }
        return wv
    }

    private fun loadTargetUrl(url: String) {
        loadRetryCount = 0
        lastTargetUrl = url
        showingOfflinePage = false
        showLoadingOverlay()
        webView.loadUrl(url)
    }

    private fun handlePageStarted() {
        // Splash может полагаться на onPageCommitVisible, но если страница быстро
        // меняется (offline → live) — поднимем contentReady по началу нового пейджа.
    }

    // ── Брендовый лоадер поверх WebView (вместо пустого белого экрана) ─────────
    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()

    private fun buildLoadingOverlay(): View {
        val overlay = FrameLayout(this).apply {
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
            )
            setBackgroundColor(Color.parseColor("#F4F2FA"))
            // Перехватываем тапы, пока грузимся (под лоадером нажимать нечего).
            isClickable = true
        }
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER,
            )
        }
        val spinner = ProgressBar(this).apply {
            isIndeterminate = true
            indeterminateTintList = ColorStateList.valueOf(Color.parseColor("#6B4FE0"))
        }
        val label = TextView(this).apply {
            text = "Загружаем…"
            setTextColor(Color.parseColor("#7A6BB0"))
            textSize = 14f
            gravity = Gravity.CENTER
            setPadding(0, dp(18), 0, 0)
        }
        column.addView(spinner)
        column.addView(label)
        overlay.addView(column)
        return overlay
    }

    private fun showLoadingOverlay() {
        val ov = loadingOverlay ?: return
        ov.animate().cancel()
        ov.alpha = 1f
        ov.visibility = View.VISIBLE
    }

    private fun hideLoadingOverlay() {
        val ov = loadingOverlay ?: return
        if (ov.visibility != View.VISIBLE) return
        ov.animate()
            .alpha(0f)
            .setDuration(220)
            .withEndAction { ov.visibility = View.GONE }
            .start()
    }

    private fun handleMainFrameError(failingUrl: String, errorCode: Int) {
        // Все коды от Chromium отрицательные — таймауты/refused/dns/proxy.
        // Ретрай только для нашего домена; внешние ошибки не глушим.
        if (!isOwnHost(failingUrl)) return
        if (showingOfflinePage) return

        if (loadRetryCount < MAX_RETRIES) {
            loadRetryCount++
            val delay = when (loadRetryCount) {
                1 -> 600L
                2 -> 1500L
                else -> 2500L
            }
            webView.postDelayed({
                webView.loadUrl(failingUrl)
            }, delay)
        } else {
            showOfflinePage(failingUrl)
        }
    }

    private fun isOwnHost(url: String): Boolean {
        return try {
            val host = Uri.parse(url).host?.lowercase() ?: return false
            host == "kartochka.ai" || host.endsWith(".kartochka.ai") ||
                // боевой домен example.com в punycode (+ кириллическая форма)
                host == "xn--your-domain.example" || host.endsWith(".xn--your-domain.example") ||
                host == "example.com" || host.endsWith(".example.com") ||
                host == "10.0.2.2" || host == "127.0.0.1" || host == "localhost"
        } catch (_: Throwable) { false }
    }

    private fun showOfflinePage(failingUrl: String) {
        showingOfflinePage = true
        lastTargetUrl = failingUrl
        contentReady = true
        hideLoadingOverlay()

        val isLocalDev = failingUrl.contains("10.0.2.2") || failingUrl.contains("127.0.0.1") || failingUrl.contains("localhost")
        val hint = if (isLocalDev) {
            "Запусти 1_server.bat — потом нажми «Повторить»."
        } else {
            "Проверь интернет-соединение."
        }

        val html = """
            <!DOCTYPE html>
            <html lang="ru">
            <head>
              <meta charset="UTF-8">
              <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
              <title>Сервер недоступен</title>
              <style>
                * { box-sizing: border-box; margin: 0; padding: 0; }
                html, body { height: 100%; -webkit-font-smoothing: antialiased; }
                body {
                  display: flex; align-items: center; justify-content: center;
                  padding: 32px;
                  font-family: -apple-system, system-ui, 'Inter', sans-serif;
                  background: #F4F2FA;
                  color: #1F1A35;
                }
                .card { max-width: 360px; width: 100%; text-align: center; }
                .icon {
                  width: 72px; height: 72px; margin: 0 auto 22px;
                  border-radius: 22px;
                  background: rgba(76,41,211,0.08);
                  color: #4C29D3;
                  display: flex; align-items: center; justify-content: center;
                  font-size: 32px;
                }
                h1 { font-size: 22px; font-weight: 700; margin-bottom: 10px; }
                p { color: rgba(31,26,53,0.66); line-height: 1.5; margin-bottom: 22px; font-size: 15px; }
                button {
                  width: 100%;
                  padding: 16px 20px;
                  border-radius: 14px;
                  background: linear-gradient(135deg, #6B4FE0, #4C29D3);
                  color: #fff;
                  border: 0;
                  font-size: 16px;
                  font-weight: 700;
                  cursor: pointer;
                  min-height: 52px;
                  box-shadow: 0 10px 28px rgba(76,41,211,0.28);
                  font-family: inherit;
                }
                button:active { transform: scale(0.98); }
                .hint { margin-top: 14px; font-size: 12px; color: rgba(31,26,53,0.42); }
                .spinner {
                  display: inline-block; width: 14px; height: 14px;
                  border: 2px solid rgba(76,41,211,0.25); border-top-color: #4C29D3;
                  border-radius: 50%; animation: spin 0.8s linear infinite;
                  vertical-align: -3px; margin-right: 6px;
                }
                @keyframes spin { to { transform: rotate(360deg); } }
              </style>
            </head>
            <body>
              <div class="card">
                <div class="icon">📡</div>
                <h1>Сервер не отвечает</h1>
                <p>$hint</p>
                <button onclick="window.kartochka.retry()">Повторить</button>
                <div class="hint"><span class="spinner"></span>Проверяю каждые 5 секунд…</div>
              </div>
              <script>
                setInterval(function(){ try { window.kartochka.retry(); } catch(e){} }, 5000);
              </script>
            </body>
            </html>
        """.trimIndent()
        webView.loadDataWithBaseURL(failingUrl, html, "text/html", "utf-8", null)
    }

    inner class KartochkaJsBridge {
        @JavascriptInterface
        fun retry() {
            webView.post {
                loadTargetUrl(lastTargetUrl)
            }
        }

        /**
         * Старт соц-входа (Google/Яндекс) из веба: account.js зовёт
         * window.kartochka.startOAuth(provider, next, ref).
         *
         * Провайдеры блокируют OAuth внутри встроенного WebView
         * («disallowed_useragent»), поэтому открываем /start?client=app во ВНЕШНЕМ
         * браузере (Custom Tab). После колбэка сервер вернёт нас в приложение по
         * deep-link kartochka://oauth?code=..., где код обменивается на сессию
         * (см. handleOAuthRedirect → /oauth/app/finish).
         */
        @JavascriptInterface
        fun startOAuth(provider: String, next: String?, ref: String?) {
            val p = provider.lowercase()
            if (p != "google" && p != "yandex") return
            val base = BuildConfig.SITE_URL.trimEnd('/')
            val sb = StringBuilder("$base/api/account/oauth/$p/start?client=app")
            if (!next.isNullOrBlank()) sb.append("&next=").append(Uri.encode(next))
            if (!ref.isNullOrBlank()) sb.append("&ref=").append(Uri.encode(ref))
            val url = sb.toString()
            webView.post { openOAuthTab(url) }
        }

        /**
         * Шарит набор сгенерированных карточек через системный Android share-sheet.
         * Вызывается из JS: window.kartochka.shareImages(JSON.stringify(absUrls), title).
         *
         * Зачем нативный путь, а не Web Share API: navigator.share({files}) в Android
         * WebView работает нестабильно (зависит от версии System WebView), для
         * нескольких файлов часто возвращает NotAllowedError. Нативный ACTION_SEND_MULTIPLE
         * работает на всех версиях Android и даёт настоящий выбор приложения
         * (TG/WA/Gmail/...) с превью миниатюр.
         */
        @JavascriptInterface
        fun shareImages(urlsJson: String, title: String?) {
            val urls: List<String> = try {
                val arr = org.json.JSONArray(urlsJson)
                (0 until arr.length()).mapNotNull { i ->
                    arr.optString(i, null).takeIf { !it.isNullOrBlank() }
                }
            } catch (_: Throwable) {
                emptyList()
            }
            if (urls.isEmpty()) return

            // Скачиваем файлы в background-потоке (нельзя сеть на UI-thread).
            // Чистим старые share-файлы (>1h) чтобы кеш не разрастался.
            Thread {
                val dir = File(cacheDir, "share").apply { mkdirs() }
                val now = System.currentTimeMillis()
                dir.listFiles()?.forEach { f ->
                    if (now - f.lastModified() > 60L * 60L * 1000L) f.delete()
                }
                val files = mutableListOf<File>()
                for ((i, url) in urls.withIndex()) {
                    try {
                        val dst = File(dir, "card_${now}_$i.jpg")
                        val conn = java.net.URL(url).openConnection()
                        conn.connectTimeout = 15_000
                        conn.readTimeout = 30_000
                        attachWebViewCookies(conn, url)
                        conn.getInputStream().use { input ->
                            dst.outputStream().use { output -> input.copyTo(output) }
                        }
                        if (dst.length() > 0) files.add(dst)
                    } catch (_: Throwable) { /* skip single bad URL */ }
                }
                if (files.isEmpty()) return@Thread

                // startActivity и FileProvider — на UI-потоке через webView.post.
                webView.post {
                    try {
                        val uris = ArrayList<Uri>(files.size)
                        for (f in files) {
                            uris.add(
                                FileProvider.getUriForFile(
                                    this@MainActivity, "$packageName.fileprovider", f
                                )
                            )
                        }
                        val intent = if (uris.size == 1) {
                            Intent(Intent.ACTION_SEND).apply {
                                type = "image/jpeg"
                                putExtra(Intent.EXTRA_STREAM, uris[0])
                                if (!title.isNullOrBlank()) putExtra(Intent.EXTRA_TEXT, title)
                                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            }
                        } else {
                            Intent(Intent.ACTION_SEND_MULTIPLE).apply {
                                type = "image/jpeg"
                                putParcelableArrayListExtra(Intent.EXTRA_STREAM, uris)
                                if (!title.isNullOrBlank()) putExtra(Intent.EXTRA_TEXT, title)
                                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            }
                        }
                        val chooser = Intent.createChooser(intent, "Поделиться карточками")
                            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                        startActivity(chooser)
                    } catch (_: Throwable) {
                        // нет приложения для приёма share — тихо игнорируем
                    }
                }
            }.start()
        }

        /**
         * Сохраняет набор сгенерированных карточек в галерею устройства
         * (Pictures/Kartochka). Это и есть «Скачать» в Android-обёртке: <a download>
         * в WebView молча не работает без DownloadListener, поэтому фронт зовёт
         * этот мост (window.kartochka.saveImages).
         *
         * Android 10+ (API 29): MediaStore, без каких-либо разрешений.
         * Android 9- (API 24-28): public Pictures + media-scan, если есть
         * WRITE_EXTERNAL_STORAGE; иначе тихо отдаём share как запасной путь.
         */
        @JavascriptInterface
        fun saveImages(urlsJson: String, title: String?) {
            val urls: List<String> = try {
                val arr = org.json.JSONArray(urlsJson)
                (0 until arr.length()).mapNotNull { i ->
                    arr.optString(i, null).takeIf { !it.isNullOrBlank() }
                }
            } catch (_: Throwable) { emptyList() }
            if (urls.isEmpty()) return

            val legacy = Build.VERSION.SDK_INT < Build.VERSION_CODES.Q
            if (legacy && ContextCompat.checkSelfPermission(
                    this@MainActivity, Manifest.permission.WRITE_EXTERNAL_STORAGE
                ) != PackageManager.PERMISSION_GRANTED) {
                // На старом Android без разрешения на запись — не упираемся, даём
                // системный share-лист (юзер сохранит/отправит из него).
                shareImages(urlsJson, title)
                return
            }

            Thread {
                var saved = 0
                val stamp = System.currentTimeMillis()
                for ((i, url) in urls.withIndex()) {
                    try {
                        if (saveOneToGallery(url, "kartochka_${stamp}_${i + 1}.jpg")) saved++
                    } catch (_: Throwable) { /* skip single bad URL */ }
                }
                val msg = when {
                    saved == 0 -> "Не удалось сохранить — попробуйте «Поделиться»"
                    saved == 1 -> "Карточка сохранена в галерею"
                    else -> "$saved карточек сохранено в галерею"
                }
                webView.post {
                    Toast.makeText(this@MainActivity, msg, Toast.LENGTH_SHORT).show()
                }
            }.start()
        }

        private fun saveOneToGallery(url: String, fileName: String): Boolean {
            return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val resolver = contentResolver
                val values = ContentValues().apply {
                    put(MediaStore.Images.Media.DISPLAY_NAME, fileName)
                    put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                    put(MediaStore.Images.Media.RELATIVE_PATH,
                        Environment.DIRECTORY_PICTURES + "/Kartochka")
                    put(MediaStore.Images.Media.IS_PENDING, 1)
                }
                val uri = resolver.insert(
                    MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values
                ) ?: return false
                try {
                    resolver.openOutputStream(uri)?.use { os -> downloadUrlInto(url, os) }
                        ?: run { resolver.delete(uri, null, null); return false }
                } catch (t: Throwable) {
                    resolver.delete(uri, null, null)
                    return false
                }
                values.clear()
                values.put(MediaStore.Images.Media.IS_PENDING, 0)
                resolver.update(uri, values, null, null)
                true
            } else {
                @Suppress("DEPRECATION")
                val dir = File(
                    Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES),
                    "Kartochka"
                ).apply { mkdirs() }
                val out = File(dir, fileName)
                try {
                    out.outputStream().use { os -> downloadUrlInto(url, os) }
                    if (out.length() <= 0L) { out.delete(); return false }
                } catch (t: Throwable) {
                    // не оставляем битый/частичный файл в галерее
                    try { out.delete() } catch (_: Throwable) {}
                    return false
                }
                MediaScannerConnection.scanFile(
                    this@MainActivity, arrayOf(out.absolutePath), arrayOf("image/jpeg"), null
                )
                true
            }
        }

        private fun downloadUrlInto(url: String, os: java.io.OutputStream) {
            val conn = java.net.URL(url).openConnection() as java.net.HttpURLConnection
            conn.connectTimeout = 15_000
            conn.readTimeout = 30_000
            conn.instanceFollowRedirects = true
            attachWebViewCookies(conn, url)
            try {
                if (conn.responseCode != java.net.HttpURLConnection.HTTP_OK) {
                    throw java.io.IOException("HTTP ${conn.responseCode}")
                }
                conn.inputStream.use { input -> input.copyTo(os) }
            } finally {
                try { conn.disconnect() } catch (_: Throwable) {}
            }
        }
    }

    private fun openExternal(url: String) {
        try {
            val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            startActivity(intent)
        } catch (_: Throwable) { /* no handler — silently ignore */ }
    }

    /** Открыть соц-вход в Chrome Custom Tab. Google/Яндекс запрещают OAuth внутри
     *  встроенного WebView, поэтому ведём провайдера во ВНЕШНЕМ браузере. Фолбэк —
     *  обычный системный браузер, если Custom Tabs недоступны. */
    private fun openOAuthTab(url: String) {
        try {
            val tab = CustomTabsIntent.Builder().setShowTitle(true).build()
            tab.intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            tab.launchUrl(this, Uri.parse(url))
        } catch (_: Throwable) {
            openExternal(url)
        }
    }

    /** Возврат из внешнего браузера после соц-входа: kartochka://oauth?code=...
     *  Грузим финальный URL в WebView — сервер обменяет одноразовый код на сессию
     *  и поставит cookie ИМЕННО в WebView, затем редиректнет на нужную страницу.
     *  Возвращает true, если это был наш deep-link (и загрузка запущена). */
    private fun handleOAuthRedirect(intent: Intent?): Boolean {
        if (intent?.action != Intent.ACTION_VIEW) return false
        val data = intent.data ?: return false
        if (data.scheme?.lowercase() != "kartochka" || data.host?.lowercase() != "oauth") return false
        val code = data.getQueryParameter("code")
        if (code.isNullOrBlank()) return false
        val base = BuildConfig.SITE_URL.trimEnd('/')
        val url = "$base/api/account/oauth/app/finish?code=" + Uri.encode(code)
        webView.post {
            showLoadingOverlay()
            webView.loadUrl(url)
        }
        return true
    }

    /** Передаёт куки WebView (gate/сессия) в native-загрузку. Без них под
     *  site_gate (или для авторизованных /api/output) сервер ответит 401 → и
     *  «Скачать», и «Поделиться» молча не срабатывали (качали 401-страницу). */
    private fun attachWebViewCookies(conn: java.net.URLConnection, url: String) {
        try {
            val cookie = CookieManager.getInstance().getCookie(url)
            if (!cookie.isNullOrEmpty()) conn.setRequestProperty("Cookie", cookie)
        } catch (_: Throwable) { /* нет кук — продолжаем без них */ }
    }

    private fun onWebFileChooser(
        callback: ValueCallback<Array<Uri>>,
        params: WebChromeClient.FileChooserParams,
    ): Boolean {
        pendingFileCallback?.onReceiveValue(null)
        pendingFileCallback = callback
        pendingChooserParams = params

        // «Сделать фото»: веб-инпут с capture="environment" → СРАЗУ открываем
        // камеру, без галерейного чузера (так фоткать быстрее, ровно как просили).
        // Это срабатывает только в фолбэке (insecure http / старый WebView); на
        // https фронт показывает встроенную камеру через getUserMedia и сюда не идёт.
        if (params.isCaptureEnabled) {
            launchCameraDirect()
            return true
        }

        // Обычная «Загрузить фото товара»: камера — опциональна; НЕ блокируем
        // gallery picker permission-диалогом на первом тапе (рвёт UX и при отказе
        // callback зависает). Camera-intent добавляем только если разрешение уже есть.
        val hasCameraPerm = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA
        ) == PackageManager.PERMISSION_GRANTED
        launchFileChooserInternal(includeCamera = hasCameraPerm)
        return true
    }

    private fun launchCameraDirect() {
        if (ContextCompat.checkSelfPermission(
                this, Manifest.permission.CAMERA
            ) != PackageManager.PERMISSION_GRANTED) {
            pendingCameraDirect = true
            cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
            return
        }
        val cameraIntent = createCameraIntent()
        if (cameraIntent == null) {
            // нет камеры/не создался файл — откат на обычный чузер
            launchFileChooserInternal(includeCamera = false)
            return
        }
        try {
            pickerLauncher.launch(cameraIntent)
        } catch (_: Throwable) {
            pendingFileCallback?.onReceiveValue(null)
            pendingFileCallback = null
            pendingChooserParams = null
            pendingCameraUri = null
        }
    }

    private fun launchFileChooserInternal(includeCamera: Boolean) {
        val params = pendingChooserParams
        // Используем простой gallery-intent (НЕ params.createIntent — он может
        // возвращать кривой intent на старых WebView, который никто не обрабатывает).
        val pickIntent = Intent(Intent.ACTION_GET_CONTENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "image/*"
            if (params?.mode == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) {
                putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
            }
        }
        val chooser = Intent(Intent.ACTION_CHOOSER).apply {
            putExtra(Intent.EXTRA_INTENT, pickIntent)
            putExtra(Intent.EXTRA_TITLE, "Выберите фото товара")
        }
        if (includeCamera) {
            val cameraIntent = createCameraIntent()
            if (cameraIntent != null) {
                chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, arrayOf(cameraIntent))
            }
        }
        try {
            pickerLauncher.launch(chooser)
        } catch (_: Throwable) {
            pendingFileCallback?.onReceiveValue(null)
            pendingFileCallback = null
            pendingChooserParams = null
            pendingCameraUri = null
        }
    }

    private fun createCameraIntent(): Intent? {
        return try {
            val dir = File(cacheDir, "camera").apply { mkdirs() }
            val photoFile = File(dir, "shot_${System.currentTimeMillis()}.jpg").apply { createNewFile() }
            val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", photoFile)
            pendingCameraUri = uri
            Intent(MediaStore.ACTION_IMAGE_CAPTURE).apply {
                putExtra(MediaStore.EXTRA_OUTPUT, uri)
                addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
        } catch (_: Throwable) { null }
    }

    private fun onWebPermissionRequest(request: PermissionRequest) {
        val wantsCamera = request.resources.any { it == PermissionRequest.RESOURCE_VIDEO_CAPTURE }
        if (wantsCamera && ContextCompat.checkSelfPermission(
                this, Manifest.permission.CAMERA
            ) != PackageManager.PERMISSION_GRANTED) {
            pendingPermissionRequest = request
            cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
            return
        }
        request.grant(request.resources)
    }

    private fun fileExistsAndNonEmpty(uri: Uri): Boolean {
        return try {
            contentResolver.openFileDescriptor(uri, "r")?.use { it.statSize > 0 } ?: false
        } catch (_: Throwable) {
            uri.path?.let { File(it) }?.let { it.exists() && it.length() > 0 } ?: false
        }
    }

    companion object {
        private const val MAX_RETRIES = 3
    }
}
