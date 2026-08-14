package ai.kartochka.app

import android.net.Uri
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView

class KartochkaChromeClient(
    private val onChooseFile: (
        callback: ValueCallback<Array<Uri>>,
        params: FileChooserParams,
    ) -> Boolean,
    private val onPermission: (PermissionRequest) -> Unit,
) : WebChromeClient() {

    override fun onShowFileChooser(
        webView: WebView,
        filePathCallback: ValueCallback<Array<Uri>>,
        fileChooserParams: FileChooserParams,
    ): Boolean = onChooseFile(filePathCallback, fileChooserParams)

    override fun onPermissionRequest(request: PermissionRequest) = onPermission(request)
}
