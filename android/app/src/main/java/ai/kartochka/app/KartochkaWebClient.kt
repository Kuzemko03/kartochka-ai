package ai.kartochka.app

import android.graphics.Bitmap
import android.net.Uri
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

class KartochkaWebClient(
    private val onExternalUrl: (String) -> Unit,
    private val onContentVisible: () -> Unit,
    private val onMainFrameError: (failingUrl: String, errorCode: Int) -> Unit,
    private val onPageStarted: () -> Unit,
) : WebViewClient() {

    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        return handleUrl(request.url ?: return false)
    }

    @Deprecated("Pre-N fallback", level = DeprecationLevel.HIDDEN)
    override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
        return handleUrl(Uri.parse(url))
    }

    private fun handleUrl(uri: Uri): Boolean {
        val scheme = uri.scheme?.lowercase() ?: return false

        if (scheme !in setOf("http", "https")) {
            onExternalUrl(uri.toString())
            return true
        }

        val host = uri.host?.lowercase() ?: return false
        // Продакшн-домен example.com — принимаем и punycode, и кириллицу
        // (на случай внутренних ссылок в любом из видов). Оба = «свой» домен.
        if (host == "xn--your-domain.example" || host.endsWith(".xn--your-domain.example")) return false
        if (host == "example.com" || host.endsWith(".example.com")) return false
        if (host == "10.0.2.2" || host == "127.0.0.1" || host == "localhost") return false

        onExternalUrl(uri.toString())
        return true
    }

    override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
        super.onPageStarted(view, url, favicon)
        onPageStarted()
    }

    override fun onPageCommitVisible(view: WebView, url: String) {
        super.onPageCommitVisible(view, url)
        onContentVisible()
    }

    override fun onReceivedError(
        view: WebView,
        request: WebResourceRequest,
        error: WebResourceError,
    ) {
        super.onReceivedError(view, request, error)
        if (!request.isForMainFrame) return
        onMainFrameError(request.url.toString(), error.errorCode)
    }
}
