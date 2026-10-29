package com.lolcompanion.pet

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.view.WindowManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject

/**
 * 一个 WebView 装 pet.html，两个按钮（开始 / 设置）。没有 Compose，没有导航库。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var prefs: Prefs
    private lateinit var webView: WebView
    private lateinit var startButton: Button

    /** 「换模型」导入时写到哪个档位；自动跟随模式下默认 lol。 */
    private var pendingProfile: String = "lol"

    private val pickModel = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri == null) return@registerForActivityResult
        toast("正在导入…")
        Thread {
            try {
                val profile = pendingProfile
                val file = ModelStore(this).importFrom(uri, profile)
                val size = if (file.name.endsWith(".labels.txt")) {
                    val count = file.readLines().count { it.isNotBlank() }
                    "$count 个类别"
                } else {
                    "%.1f MB".format(file.length() / 1024.0 / 1024.0)
                }
                runOnUiThread {
                    toast("已导入 $profile：${file.name}（$size），重启服务生效")
                }
            } catch (error: Throwable) {
                runOnUiThread { toast("导入失败：${error.message}") }
            }
        }.start()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(R.layout.activity_main)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        webView = findViewById(R.id.web)
        startButton = findViewById(R.id.start)
        configureWebView()

        PetBus.onSay = { lines, holdMs -> pushSay(lines, holdMs) }
        PetBus.onStatus = { text -> pushStatus(text) }
        PetBus.onStats = { text -> pushStats(text) }

        startButton.setOnClickListener { toggleCompanion() }
        findViewById<View>(R.id.gear).setOnClickListener { showSettings() }

        requestNotificationPermission()
        if (prefs.autoStart && CompanionService.isRunning.not()) startCompanion()
        updateStartLabel()
    }

    override fun onResume() {
        super.onResume()
        updateStartLabel()
    }

    override fun onDestroy() {
        PetBus.clear()
        webView.destroy()
        super.onDestroy()
    }

    // -------------------------------------------------------------- webview
    private fun configureWebView() {
        webView.setBackgroundColor(Color.parseColor("#080D18"))
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = true
            mediaPlaybackRequiresUserGesture = false
            useWideViewPort = true
            loadWithOverviewMode = true
            cacheMode = WebSettings.LOAD_NO_CACHE
        }
        webView.webViewClient = WebViewClient()
        webView.loadUrl("file:///android_asset/pet.html")
    }

    private fun pushSay(lines: List<String>, holdMs: Long) {
        runOnUiThread {
            val array = JSONArray()
            lines.forEach { array.put(it) }
            webView.evaluateJavascript("window.petSay(${array}, $holdMs)", null)
        }
    }

    private fun pushStatus(text: String) {
        runOnUiThread {
            webView.evaluateJavascript("window.petStatus(${JSONObject.quote(text)})", null)
        }
    }

    private fun pushStats(text: String) {
        runOnUiThread {
            webView.evaluateJavascript("window.petHud(${JSONObject.quote(text)})", null)
        }
    }

    // --------------------------------------------------------------- service
    private fun toggleCompanion() {
        if (CompanionService.isRunning) {
            stopService(Intent(this, CompanionService::class.java))
            toast("已停止")
            startButton.postDelayed({ updateStartLabel() }, 300)
            return
        }
        startCompanion()
    }

    private fun startCompanion() {
        if (prefs.baseUrl().isBlank()) {
            toast("先在设置里填电脑地址")
            showSettings()
            return
        }
        val intent = Intent(this, CompanionService::class.java)
            .setAction(CompanionService.ACTION_START)
        ContextCompat.startForegroundService(this, intent)
        startButton.postDelayed({ updateStartLabel() }, 300)
    }

    private fun updateStartLabel() {
        startButton.setText(if (CompanionService.isRunning) R.string.stop else R.string.start)
    }

    // -------------------------------------------------------------- settings
    private fun showSettings() {
        val density = resources.displayMetrics.density
        val padding = (16 * density).toInt()
        val store = ModelStore(this)

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, 0)
        }
        val hostInput = editText(prefs.host, getString(R.string.host_hint))
        val keyInput = editText(prefs.apiKey, "DeepSeek API Key（只存本机，不写进 APK）")
        val intervalInput = editText(prefs.intervalSec.toString(), "常规点评间隔（秒）").apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        val modelInput = editText(prefs.modelPath, "模型路径（一般留空，用「换模型」导入即可）")
        val profileRow = TextView(this).apply {
            text = "游戏档位：" + profileLabel(prefs.profileMode)
            setTextColor(Color.parseColor("#E9F0FF"))
            textSize = 14f
            setPadding(0, (12 * density).toInt(), 0, 0)
            setOnClickListener {
                showProfileDialog { mode ->
                    prefs.profileMode = mode
                    text = "游戏档位：" + profileLabel(mode)
                }
            }
        }
        val available = store.availableProfiles()
        val status = TextView(this).apply {
            val list = if (available.isEmpty()) "无" else available.joinToString(", ")
            text = "已放模型的档位：$list\n自动跟随时，文件名要跟电脑的 --profile 同名\n目录：${store.dir.absolutePath}"
            setTextColor(Color.parseColor("#8EA6FF"))
            textSize = 11f
            setPadding(0, (8 * density).toInt(), 0, 0)
        }
        listOf(hostInput, keyInput, intervalInput, modelInput, profileRow, status)
            .forEach { layout.addView(it) }

        AlertDialog.Builder(this)
            .setTitle("设置")
            .setView(layout)
            .setPositiveButton("保存") { _, _ ->
                prefs.host = hostInput.text.toString()
                prefs.apiKey = keyInput.text.toString()
                prefs.intervalSec = intervalInput.text.toString().toIntOrNull() ?: 30
                prefs.modelPath = modelInput.text.toString()
                toast("已保存，重启服务后生效")
            }
            .setNeutralButton("换模型") { _, _ -> showModelDialog(targetProfile()) }
            .setNegativeButton("取消", null)
            .show()
    }

    /**
     * 模型要写进哪个档位。
     *
     * 自动跟随时不能写死 lol——PC 报的是 valorant 就得写 valorant.tflite，
     * 否则服务按档位找不到模型，只会「仅对话」。
     */
    private fun targetProfile(): String {
        val mode = prefs.profileMode
        if (mode != Prefs.PROFILE_AUTO) return mode
        return prefs.lastProfile.ifBlank { "lol" }
    }

    private fun profileLabel(mode: String): String = when (mode) {
        Prefs.PROFILE_AUTO -> "自动跟随电脑"
        Prefs.PROFILE_NONE -> "仅对话（不检测）"
        "valorant" -> "瓦罗兰特"
        "delta" -> "三角洲行动"
        "lol" -> "英雄联盟"
        else -> mode
    }

    private fun showProfileDialog(onPick: (String) -> Unit) {
        val modes = arrayOf(Prefs.PROFILE_AUTO, "lol", "valorant", "delta", Prefs.PROFILE_NONE)
        val labels = modes.map { profileLabel(it) }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("游戏档位")
            .setItems(labels) { _, which -> onPick(modes[which]) }
            .show()
    }

    private fun showModelDialog(profile: String) {
        val name = if (profile == Prefs.PROFILE_NONE) ModelStore.PROFILE_GENERIC else profile
        val actions = arrayOf("从文件选择（.tflite 或 labels.txt）", "从 URL 下载", "清除 $name 的模型")
        AlertDialog.Builder(this)
            .setTitle("模型热换 → $name.tflite")
            .setItems(actions) { _, which ->
                when (which) {
                    0 -> {
                        pendingProfile = name
                        pickModel.launch("*/*")
                    }
                    1 -> promptModelUrl(name)
                    2 -> {
                        val store = ModelStore(this)
                        val removed = store.modelFile(name).delete()
                        toast(if (removed) "已清除 $name，该档位将变为仅对话" else "没有可清除的模型")
                    }
                }
            }
            .show()
    }

    private fun promptModelUrl(profile: String) {
        val input = editText("", "https://…/yolov8n.tflite")
        AlertDialog.Builder(this)
            .setTitle("模型地址 → $profile.tflite")
            .setView(input)
            .setPositiveButton("下载") { _, _ ->
                val url = input.text.toString().trim()
                if (url.isEmpty()) return@setPositiveButton
                toast("后台下载中…")
                Thread {
                    try {
                        val file = ModelStore(this).download(url, profile)
                        runOnUiThread { toast("已下载 ${file.length() / 1024 / 1024} MB，重启服务生效") }
                    } catch (error: Throwable) {
                        runOnUiThread { toast("下载失败：${error.message}") }
                    }
                }.start()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun editText(value: String, hint: String): EditText = EditText(this).apply {
        setText(value)
        this.hint = hint
        textSize = 13f
        inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        setSingleLine()
    }

    // ------------------------------------------------------------- plumbing
    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        ) return
        requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 42)
    }

    private fun toast(text: String) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show()
    }
}
