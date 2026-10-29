package com.lolcompanion.pet

import android.util.Base64
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** 玩家对陪玩说的一句话（来自 PC 麦克风或 PC 控制台）。 */
data class Say(val id: Int, val text: String, val source: String)

data class Tick(
    val seq: Int,
    val unchanged: Boolean,
    val image: ByteArray?,
    val values: JSONObject,
    val unstable: List<String>,
    val calibrated: Boolean,
    val ocrMs: Double,
    /** PC 端 --profile 的值，手机据此决定加载哪个模型。 */
    val profile: String,
    /** 玩家说的话，按 id 增量取，不会重复。 */
    val say: List<Say>,
    /** PC 下发的画面宽度。手机据此挑对应分辨率的模型。 */
    val sentWidth: Int = 0
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
    private var sinceSay = 0
    private var sayEpoch = 0

    fun reset() {
        since = null
    }

    @Throws(IOException::class)
    fun pull(wantImage: Boolean): Tick {
        val url = StringBuilder(baseUrl).append("/tick")
        since?.let { url.append("?since=").append(it) }
        url.append(if (since != null) "&" else "?").append("say=").append(sinceSay)
        if (!wantImage) {
            url.append("&image=0")
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
            // 下发宽度：手机据此自动挑 640/960/1280 的模型
            val sentWidth = json.optJSONArray("size")?.optInt(0, 0) ?: 0

            // PC 重启后说话队列会从头编号；世代变了就把游标归零，
            // 否则新进程里的消息会因为 id 变小而被一直跳过。
            val epoch = json.optInt("sayEpoch", 0)
            if (epoch != 0 && epoch != sayEpoch) {
                sayEpoch = epoch
                sinceSay = 0
            }
            val say = parseSay(json.optJSONArray("say"))
            if (say.isNotEmpty()) {
                sinceSay = say.maxOf { it.id }
            }

            if (json.optBoolean("unchanged", false)) {
                return Tick(seq, true, null, JSONObject(), emptyList(), false, 0.0,
                    profile, say, sentWidth)
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
                profile = profile,
                say = say,
                sentWidth = sentWidth
            )
        }
    }

    private fun parseSay(array: JSONArray?): List<Say> {
        if (array == null || array.length() == 0) return emptyList()
        val items = mutableListOf<Say>()
        for (index in 0 until array.length()) {
            val item = array.optJSONObject(index) ?: continue
            val id = item.optInt("id", 0)
            val text = item.optString("text", "").trim()
            if (id <= sinceSay || text.isEmpty()) continue
            items.add(Say(id, text, item.optString("source", "pc")))
        }
        return items
    }
}
