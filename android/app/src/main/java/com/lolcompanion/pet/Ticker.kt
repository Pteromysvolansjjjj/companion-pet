package com.lolcompanion.pet

import android.util.Base64
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

data class Tick(
    val seq: Int,
    val unchanged: Boolean,
    val image: ByteArray?,
    val values: JSONObject,
    val unstable: List<String>,
    val calibrated: Boolean,
    val ocrMs: Double,
    /** PC 端 --profile 的值，手机据此决定加载哪个模型。 */
    val profile: String
)

/**
 * 拉 PC 的 /tick。带 since，画面没变时 PC 只回 20 字节，省流量也省手机电。
 */
class Ticker(private val baseUrl: String) {

    private val client = OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private var since: Int? = null

    fun reset() {
        since = null
    }

    @Throws(IOException::class)
    fun pull(wantImage: Boolean): Tick {
        val url = StringBuilder(baseUrl).append("/tick")
        since?.let { url.append("?since=").append(it) }
        if (!wantImage) {
            url.append(if (since != null) "&" else "?").append("image=0")
        }

        val request = Request.Builder().url(url.toString()).get().build()
        client.newCall(request).execute().use { response ->
            val body = response.body?.string() ?: throw IOException("空响应")
            if (!response.isSuccessful) {
                throw IOException("HTTP ${response.code}: ${body.take(120)}")
            }
            val json = JSONObject(body)
            val seq = json.optInt("seq", -1)
            val profile = json.optString("profile", "")
            if (json.optBoolean("unchanged", false)) {
                return Tick(seq, true, null, JSONObject(), emptyList(), false, 0.0, profile)
            }
            if (seq >= 0) since = seq
            val ocr = json.optJSONObject("ocr") ?: JSONObject()
            val values = ocr.optJSONObject("values") ?: JSONObject()
            val unstable = mutableListOf<String>()
            ocr.optJSONArray("unstable")?.let { array: JSONArray ->
                for (index in 0 until array.length()) {
                    unstable.add(array.optString(index))
                }
            }
            val image = json.optString("image", "").takeIf { it.isNotEmpty() }?.let {
                try {
                    Base64.decode(it, Base64.DEFAULT)
                } catch (error: IllegalArgumentException) {
                    null
                }
            }
            return Tick(
                seq = seq,
                unchanged = false,
                image = image,
                values = values,
                unstable = unstable,
                calibrated = ocr.optBoolean("calibrated", false),
                ocrMs = ocr.optDouble("ms", 0.0),
                profile = profile
            )
        }
    }
}
