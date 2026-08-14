# Keep WebView JS interfaces (none exposed currently — placeholder if added later)
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}

# Suppress noisy notes
-dontwarn org.codehaus.mojo.animal_sniffer.*
