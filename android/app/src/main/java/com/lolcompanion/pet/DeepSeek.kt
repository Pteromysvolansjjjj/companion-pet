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
            "玩家没问你就别讲道理，只点评眼前发生的事。" +
            "玩家开口问你时，先回答他，再补一句自己的判断。"

    /**
     * @param playerText 玩家刚说的话；非空时当作对话来答，而不是自动播报。
     * @return first = 文本, second = "deepseek" 或 "fallback"。
     */
    fun ask(
        payload: JSONObject,
        apiKey: String,
        model: String,
        playerText: String? = null
    ): Pair<String, String> {
        if (apiKey.isBlank()) return fallback(payload) to "fallback"
        var currentModel = model.ifBlank { Prefs.DEFAULT_DS_MODEL }
        val content = if (playerText.isNullOrBlank()) {
            payload.toString()
        } else {
            // 玩家提问：只发「一行简况 + 他说的话」。
            // 完整 payload 里有 HUD 全量、检测明细、事件列表，提问时根本用不上，
            // 发过去只是白烧 token。
            briefContext(payload) + "玩家说：" + playerText
        }
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
                            .put(JSONObject().put("role", "user").put("content", content))
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

    /**
     * 一行战况简况，用来给「玩家提问」这条路径当上下文。
     * 空的话返回空串——只发玩家说的话也够模型回答了。
     */
    private fun briefContext(payload: JSONObject): String {
        val values = payload.optJSONObject("hud")?.optJSONObject("values") ?: JSONObject()
        val bits = mutableListOf<String>()
        if (values.has("hp")) bits.add("血量${values.optInt("hp")}")
        if (values.has("shield")) bits.add("护甲${values.optInt("shield")}")
        if (values.has("ammo")) bits.add("弹药${values.optInt("ammo")}")
        if (values.has("ammoReserve")) bits.add("备弹${values.optInt("ammoReserve")}")
        if (values.has("timer")) {
            val timer = values.optInt("timer", 0)
            bits.add("计时%d:%02d".format(timer / 60, timer % 60))
        }
        if (values.has("allyKills") && values.has("enemyKills")) {
            bits.add("比分${values.optInt("allyKills")}:${values.optInt("enemyKills")}")
        }
        return if (bits.isEmpty()) "" else "当前：" + bits.joinToString(" ") + "\n"
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
        val objects = payload.optJSONObject("scene")?.optJSONArray("objects")
        val top = if (objects != null && objects.length() > 0) objects.optJSONObject(0) else null
        val timer = if (values.has("timer")) values.optInt("timer", -1) else -1
        val clock = if (timer >= 0) "%d:%02d".format(timer / 60, timer % 60) else null

        // 玩家开口问了，先应一声，别拿数据糊弄
        if (payload.optBoolean("fromPlayer", false)) {
            val hint = when {
                top != null && top.optInt("count") >= 3 -> "那边人不少"
                clock != null -> "$clock 这会儿"
                else -> "画面挺安静"
            }
            return "我在，$hint。"
        }

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
