package com.lolcompanion.pet

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * DeepSeek 对话。手机端只做这一步云端调用，视觉和 OCR 都不上云。
 *
 * 超时 8s，失败重试 1 次，再失败就推本地兜底文案——桌宠不会因为没网就哑掉。
 */
object DeepSeek {

    private const val URL = "https://api.deepseek.com/chat/completions"
    private const val FALLBACK_MODEL = "deepseek-chat"
    private val JSON = "application/json; charset=utf-8".toMediaType()

    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(8, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()

    private const val SYSTEM_PROMPT =
        "你是坐在玩家旁边看比赛的陪玩。语气像熟人，短句，口语，别端着。" +
            "只根据给你的 hud 数字和 scene 检测结果说话，不要编造没给你的信息。" +
            "每次回 1-2 句，总长不超过 45 个汉字，不要 Markdown，不要表情符号。" +
            "玩家没问你就别讲道理，只点评眼前发生的事。"

    /** @return first = 文本, second = "deepseek" 或 "fallback"。 */
    fun ask(payload: JSONObject, apiKey: String, model: String): Pair<String, String> {
        if (apiKey.isBlank()) return fallback(payload) to "fallback"
        var currentModel = model.ifBlank { Prefs.DEFAULT_DS_MODEL }
        repeat(2) { attempt ->
            try {
                val body = JSONObject()
                    .put("model", currentModel)
                    .put("stream", false)
                    .put("temperature", 0.9)
                    .put("max_tokens", 120)
                    .put(
                        "messages", JSONArray()
                            .put(JSONObject().put("role", "system").put("content", SYSTEM_PROMPT))
                            .put(JSONObject().put("role", "user").put("content", payload.toString()))
                    )
                val request = Request.Builder()
                    .url(URL)
                    .header("authorization", "Bearer $apiKey")
                    .post(body.toString().toRequestBody(JSON))
                    .build()
                client.newCall(request).execute().use { response ->
                    val text = response.body?.string().orEmpty()
                    if (!response.isSuccessful) {
                        if (response.code in 400..422 && text.contains("model", true) &&
                            currentModel != FALLBACK_MODEL && attempt == 0
                        ) {
                            currentModel = FALLBACK_MODEL
                            return@repeat
                        }
                        return fallback(payload) to "fallback"
                    }
                    val content = JSONObject(text)
                        .optJSONArray("choices")
                        ?.optJSONObject(0)
                        ?.optJSONObject("message")
                        ?.optString("content")
                        .orEmpty()
                    val cleaned = clean(content)
                    if (cleaned.isNotEmpty()) return cleaned to "deepseek"
                }
            } catch (error: Throwable) {
                Thread.sleep(300)
            }
        }
        return fallback(payload) to "fallback"
    }

    fun clean(text: String): String =
        text.replace(Regex("[`*#]"), "")
            .replace(Regex("\\s+"), " ")
            .trim()
            .take(120)

    fun splitLines(text: String, limit: Int = 3): List<String> =
        text.split(Regex("(?<=[。！？!?])"))
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .take(limit)

    /** 没 key / 没网 / 模型挂了，也要说一句真话。 */
    fun fallback(payload: JSONObject): String {
        val hud = payload.optJSONObject("hud")
        val values = hud?.optJSONObject("values") ?: JSONObject()
        val events = payload.optJSONArray("events") ?: JSONArray()
        for (index in 0 until events.length()) {
            val event = events.optJSONObject(index) ?: continue
            when (event.optString("kind")) {
                "death" -> return "这波没了，别急，等人齐再打。"
                "allyKills", "enemyKills" -> {
                    val up = event.optString("kind") == "allyKills" &&
                        event.optInt("to") > event.optInt("from")
                    return if (up) "拿下一个，节奏来了。" else "对面又拿一个，稳一点。"
                }
            }
        }
        val objects = payload.optJSONObject("scene")?.optJSONArray("objects")
        val top = if (objects != null && objects.length() > 0) objects.optJSONObject(0) else null
        val timer = if (values.has("timer")) values.optInt("timer", -1) else -1
        val clock = if (timer >= 0) "%d:%02d".format(timer / 60, timer % 60) else null
        if (top != null && top.optInt("count") >= 3) {
            val region = when (top.optString("region")) {
                "left" -> "左边"
                "right" -> "右边"
                else -> "中路"
            }
            return "$region 那边人不少，注意点。"
        }
        if (clock != null) return "$clock，先补刀，别越线。"
        return "画面有点安静，稳住补刀。"
    }
}
