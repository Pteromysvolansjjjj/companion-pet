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

    /**
     * 陪玩人设。
     *
     * 之前这版开头是「只根据给你的 hud 数字说话」——那句话等于把模型钉成复读机：
     * 只准引用数字，它当然只会念数字。现在改成：数值可以提，但**别重复**，
     * 并且给了四个口语示例（few-shot 对口语风格的拉拽比任何形容词都管用）。
     */
    private const val SYSTEM_PROMPT =
        "你是在玩家旁边一起打游戏的陪玩，像一个熟人坐在旁边讲话。\n" +
            "说话方式：\n" +
            "- 口语、短句，像朋友聊天，别像播报员或者客服。\n" +
            "- 提醒玩家数值是可以的，比如血量不高、子弹快没了、刚拿下一个。\n" +
            "- 但别把同一个数值反复念。玩家早就知道自己的血量是多少，你再报一遍就没意思了。\n" +
            "- 你刚说过的话不要重复。同样的意思换个说法，或者干脆闭嘴。\n" +
            "- 最多 2 句，总共不超过 40 个汉字。不要 Markdown，不要表情符号。\n" +
            "- 不要编造没给的信息。没素材的时候可以只说一句闲话。\n" +
            "玩家被打死的时候：先接住情绪——安慰一句或者给个具体建议，\n" +
            "别只说「你死了」。\n" +
            "下面是好的说法：\n" +
            "「对面那个狙有点东西，别急着拉枪线」\n" +
            "「你血量不多了，先退一下」\n" +
            "「这波我帮你盯着，你放心打」\n" +
            "「刚才那波是他先开的枪，下次等技能转好再 peek」\n" +
            "下面是坏的说法（别这样）：\n" +
            "「当前生命值 23，护甲 0，弹药 3」\n" +
            "「根据检测结果，画面中存在 2 个敌方目标」"

    /**
     * @param playerText 玩家刚说的话；非空时当作对话来答，而不是自动播报。
     * @param history 多轮对话历史（前面的 user/assistant 消息），让陪玩能接上话。
     * @return first = 文本, second = "deepseek" 或 "fallback"。
     */
    fun ask(
        payload: JSONObject,
        apiKey: String,
        model: String,
        playerText: String? = null,
        history: List<JSONObject> = emptyList()
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
                val messages = JSONArray()
                messages.put(JSONObject().put("role", "system").put("content", SYSTEM_PROMPT))
                // 历史在前，当前这轮在后——陪玩才能接上话
                for (item in history) {
                    messages.put(item)
                }
                messages.put(JSONObject().put("role", "user").put("content", content))
                val body = JSONObject()
                    .put("model", currentModel)
                    .put("stream", false)
                    .put("temperature", 1.0)
                    .put("max_tokens", 80)
                    // 压一下复读：同一场对话里反复说同一句会很劝退
                    .put("presence_penalty", 0.6)
                    .put("frequency_penalty", 0.4)
                    .put("messages", messages)
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

    /**
     * 没 key / 没网 / 模型挂了，也要说一句真话。
     *
     * 兼容两种 payload：自动播报用的是「情境句」版（notes），
     * 玩家插话用的是带 hud 数值的那版。
     */
    fun fallback(payload: JSONObject): String {
        val notes = payload.optJSONArray("notes")
        val events = payload.optJSONArray("events") ?: JSONArray()

        // 玩家开口问了，先应一声，别拿数据糊弄
        if (payload.optBoolean("fromPlayer", false)) {
            val values = payload.optJSONObject("hud")?.optJSONObject("values") ?: JSONObject()
            val timer = if (values.has("timer")) values.optInt("timer", -1) else -1
            val clock = if (timer >= 0) "%d:%02d".format(timer / 60, timer % 60) else null
            return if (clock != null) "我在，$clock 这会儿你问我了，说吧。" else "我在，你说。"
        }

        for (index in 0 until events.length()) {
            val event = events.optJSONObject(index) ?: continue
            when (event.optString("kind")) {
                "death" -> return "这波没了，别急，想好再出去。"
                "hurt" -> return "你挨了一下，先拉开距离。"
                "allyKillsChange" -> return "拿下一个，节奏来了。"
                "enemyKillsChange" -> return "对面又拿一个，稳一点。"
            }
        }

        if (notes != null && notes.length() > 0) {
            val first = notes.optString(0)
            return when {
                first.contains("打死") -> "这波没了，别急，想好再出去。"
                first.contains("挨了") -> "你挨了一下，先拉开距离。"
                first.contains("血") -> "你血量不多了，先退一下。"
                first.contains("子弹") || first.contains("备弹") -> "子弹不多了，看看怎么补。"
                first.contains("经济") -> "经济不多了，这波别乱买。"
                first.contains("护甲") -> "护甲没了，小心点。"
                first.contains("对面") -> "那边有人，注意点。"
                else -> "我盯着呢，你打你的。"
            }
        }

        val values = payload.optJSONObject("hud")?.optJSONObject("values") ?: JSONObject()
        val timer = if (values.has("timer")) values.optInt("timer", -1) else -1
        if (timer >= 0) return "%d:%02d，稳住别急。".format(timer / 60, timer % 60)
        return "画面有点安静，稳住补刀。"
    }
}
