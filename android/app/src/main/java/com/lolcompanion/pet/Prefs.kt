package com.lolcompanion.pet

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit

/**
 * 所有用户设置都落在 SharedPreferences。API Key 只存这里，绝不进 APK。
 */
class Prefs(context: Context) {

    private val sp: SharedPreferences =
        context.getSharedPreferences("pet", Context.MODE_PRIVATE)

    var host: String
        get() = sp.getString(KEY_HOST, "") ?: ""
        set(value) = sp.edit { putString(KEY_HOST, normalizeHost(value)) }

    var apiKey: String
        get() = sp.getString(KEY_API_KEY, "") ?: ""
        set(value) = sp.edit { putString(KEY_API_KEY, value.trim()) }

    var modelPath: String
        get() = sp.getString(KEY_MODEL, "") ?: ""
        set(value) = sp.edit { putString(KEY_MODEL, value.trim()) }

    var deepseekModel: String
        get() = sp.getString(KEY_DS_MODEL, DEFAULT_DS_MODEL) ?: DEFAULT_DS_MODEL
        set(value) = sp.edit { putString(KEY_DS_MODEL, value.trim().ifBlank { DEFAULT_DS_MODEL }) }

    /** 没有事件时的常规点评间隔，秒。 */
    var intervalSec: Int
        get() = sp.getInt(KEY_INTERVAL, 30)
        set(value) = sp.edit { putInt(KEY_INTERVAL, value.coerceIn(5, 600)) }

    var autoStart: Boolean
        get() = sp.getBoolean(KEY_AUTO_START, false)
        set(value) = sp.edit { putBoolean(KEY_AUTO_START, value) }

    /** PC 端最近一次上报的档位，自动模式下决定模型导入到哪个文件。 */
    var lastProfile: String
        get() = sp.getString(KEY_LAST_PROFILE, "") ?: ""
        set(value) = sp.edit { putString(KEY_LAST_PROFILE, value.trim()) }

    /**
     * 用哪个游戏档位：`auto` 跟随 PC 端 `/tick` 里的 profile，
     * `none` 强制只对话不加载模型，其余值强制指定档位。
     */
    var profileMode: String
        get() = sp.getString(KEY_PROFILE, PROFILE_AUTO) ?: PROFILE_AUTO
        set(value) = sp.edit { putString(KEY_PROFILE, value.trim().ifBlank { PROFILE_AUTO }) }

    /** 形如 http://192.168.1.10:8770，末尾不带斜杠。 */
    fun baseUrl(): String {
        val raw = host
        if (raw.isBlank()) return ""
        val withScheme = if (raw.startsWith("http://") || raw.startsWith("https://")) {
            raw
        } else {
            "http://$raw"
        }
        return withScheme.trimEnd('/')
    }

    companion object {
        private const val KEY_HOST = "host"
        private const val KEY_API_KEY = "apiKey"
        private const val KEY_MODEL = "modelPath"
        private const val KEY_DS_MODEL = "deepseekModel"
        private const val KEY_INTERVAL = "intervalSec"
        private const val KEY_AUTO_START = "autoStart"
        private const val KEY_PROFILE = "profileMode"
        private const val KEY_LAST_PROFILE = "lastProfile"
        const val DEFAULT_DS_MODEL = "deepseek-flash"
        const val PROFILE_AUTO = "auto"
        const val PROFILE_NONE = "none"

        fun normalizeHost(value: String): String =
            value.trim().trimEnd('/').removePrefix("http://").removePrefix("https://")
    }
}
