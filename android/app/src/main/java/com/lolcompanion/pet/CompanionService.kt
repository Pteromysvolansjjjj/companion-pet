package com.lolcompanion.pet

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.BitmapFactory
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 前台服务：拉 /tick → 解码 → TFLite → 聚合 → DeepSeek → 气泡。
 *
 * 手机端不做 OCR，也不碰原始帧以外的东西；送给模型的只有数字和「类别 + 三分区」。
 */
class CompanionService : Service() {

    private lateinit var prefs: Prefs
    private val running = AtomicBoolean(false)
    private var worker: Thread? = null
    private var ticker: Ticker? = null
    private var detector: Detector? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val previous = HashMap<String, Any>()

    /** 最近一帧的上下文，玩家插话时即使这一帧没变也能拿来做答。 */
    private var lastValues = JSONObject()
    private var lastUnstable = emptyList<String>()
    private var lastObjects = emptyList<Triple<String, Int, String>>()
    private var lastEvents = emptyList<JSONObject>()
    private var lastCalibrated = false

    /** 最近一次看到敌人的时刻与数量：玩家随后死亡时用来判断是不是被打了。 */
    private var lastEnemyAt = 0L
    private var lastEnemyCount = 0

    /** 最近说过的话，防复读。 */
    private val recentSpoken = mutableListOf<String>()

    /** 多轮对话历史：陪玩要能接上话，不能每句都从零开始。 */
    private val chatHistory = mutableListOf<JSONObject>()

    /** 这一局发生过什么（人类可读），每条一句，按时间顺序。 */
    private val matchMemory = mutableListOf<String>()

    /** 血量 HUD 从「有」变「没有」的时刻，用来判死亡。 */
    private var hpMissingSince = 0L

    /** 上一次跑 YOLO 的时刻；配合 DETECT_INTERVAL_MS 降频。 */
    private var lastDetectAt = 0L

    /** 降频期间复用的检测结果。 */
    private var cachedObjects = emptyList<Triple<String, Int, String>>()

    /** PC 最近一次下发的画面宽度，用来挑对应分辨率的模型。 */
    private var lastSentWidth = 0

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            isRunning = false
            stopSelf()
            return START_NOT_STICKY
        }
        ServiceCompat.startForeground(
            this, NOTIFICATION_ID, buildNotification(getString(R.string.waiting)),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            } else {
                0
            }
        )
        if (running.compareAndSet(false, true)) {
            isRunning = true
            acquireWakeLock()
            worker = Thread({ loop() }, "companion-loop").apply {
                priority = Thread.NORM_PRIORITY
                start()
            }
        }
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        isRunning = false
        running.set(false)
        worker?.interrupt()
        worker = null
        detector?.close()
        detector = null
        releaseWakeLock()
        PetBus.status("已停止")
        super.onDestroy()
    }

    // ------------------------------------------------------------------ loop
    private fun loop() {
        val baseUrl = prefs.baseUrl()
        if (baseUrl.isBlank()) {
            PetBus.status("还没填电脑地址，点右上角设置")
            stopSelf()
            return
        }
        ticker = Ticker(baseUrl)

        val store = ModelStore(this)
        // 先空手套一次 /tick（不带图）拿到 PC 端的 profile，再决定加载哪个模型。
        var profile = prefs.profileMode
        if (profile == Prefs.PROFILE_AUTO) {
            profile = try {
                val probe = ticker?.pull(false)
                ticker?.reset()
                probe?.profile?.ifBlank { DEFAULT_PROFILE } ?: DEFAULT_PROFILE
            } catch (error: Throwable) {
                DEFAULT_PROFILE
            }
        }
        prefs.lastProfile = profile
        loadDetector(store, profile)

        var lastSpokeAt = 0L
        var failures = 0

        while (running.get()) {
            val startedAt = System.currentTimeMillis()
            try {
                val tick = ticker?.pull(detector != null) ?: break
                failures = 0

                if (tick.sentWidth > 0) lastSentWidth = tick.sentWidth

                // 玩家说话优先回：即使这一帧画面没变也要应答
                var answeredPlayer = false
                if (tick.say.isNotEmpty()) {
                    handlePlayerTalk(tick.say, profile)
                    answeredPlayer = true
                }

                if (tick.unchanged) {
                    sleep(POLL_IDLE_MS)
                    continue
                }

                // PC 端换了 --profile：同一个 APK 自动换模型，不用手点
                if (prefs.profileMode == Prefs.PROFILE_AUTO &&
                    tick.profile.isNotBlank() && tick.profile != profile
                ) {
                    profile = tick.profile
                    prefs.lastProfile = profile
                    loadDetector(store, profile)
                }

                val values = tick.values
                val unstable = tick.unstable

                val objects = mutableListOf<Triple<String, Int, String>>()
                val activeDetector = detector
                // 检测不必每帧都做：YOLO 一次几十到几百毫秒，5 FPS 全跑会把主循环
                // 拖到 1.5 FPS，语音消息和气泡跟着一起卡。按时间间隔降频，
                // 两帧之间复用上一次结果。
                val nowMs = System.currentTimeMillis()
                if (activeDetector != null && tick.image != null &&
                    nowMs - lastDetectAt >= DETECT_INTERVAL_MS
                ) {
                    try {
                        val bitmap = BitmapFactory.decodeByteArray(tick.image, 0, tick.image.size)
                        if (bitmap != null) {
                            cachedObjects = Detector.summarise(activeDetector.detect(bitmap))
                            bitmap.recycle()
                        }
                    } catch (error: Throwable) {
                        // 单帧推理失败不影响主循环
                    }
                    lastDetectAt = nowMs
                }
                objects += cachedObjects

                // 事件要在检测之后才算：死亡时要结合「刚刚看到几个敌人」
                val events = trackEvents(values, unstable, objects)

                lastValues = values
                lastUnstable = unstable
                lastObjects = objects
                lastEvents = events
                lastCalibrated = tick.calibrated

                val emptyHud = values.length() == 0
                // clock 事件只当上下文，不触发播报——每 2.4 秒来一次的话必然复读。
                // 只有「死了 / 挨打 / 拿了人头」这种才值得立刻说一句。
                val notable = events.any {
                    val kind = it.optString("kind")
                    kind == "death" || kind == "hurt" ||
                        kind == "allyKillsChange" || kind == "enemyKillsChange"
                }
                val due = notable ||
                    System.currentTimeMillis() - lastSpokeAt >= prefs.intervalSec * 1000L
                if (emptyHud && objects.isEmpty()) {
                    PetBus.status("已连接，画面里还没有可读数字")
                    sleep(POLL_IDLE_MS)
                    continue
                }

                val shownDetector = detector
                PetBus.status("已连接 · ${tick.ocrMs.toInt()}ms" +
                    detectSuffix(objects, shownDetector != null,
                        shownDetector?.lastDetectMs ?: 0.0,
                        shownDetector?.usingGpu == true))
                PetBus.stats(buildStats(profile, values, objects))

                if (due && !answeredPlayer) {
                    val payload = broadcastPayload(profile, values, objects, events)
                    val (text, source) = DeepSeek.ask(
                        payload, prefs.apiKey, prefs.deepseekModel, null, chatHistory
                    )
                    val lines = DeepSeek.splitLines(text)
                    // 复读保护：跟最近几句太像就不发，宁可不说也别像机器人
                    if (lines.isNotEmpty() && !isRepeat(text)) {
                        PetBus.say(lines, holdMillisFor(text))
                        rememberSpoken(text)
                        rememberChat(briefAsk(payload), text)
                        lastSpokeAt = System.currentTimeMillis()
                        updateNotification(buildNotification(lines.first(), source))
                    }
                }
            } catch (error: Throwable) {
                failures += 1
                PetBus.status("连接断开，重试中…（${failures}）")
                ticker?.reset()
                sleep(if (failures > 3) 3000 else 1200)
            }
            val spent = System.currentTimeMillis() - startedAt
            if (spent < POLL_MIN_MS) sleep(POLL_MIN_MS - spent)
        }
    }

    // ------------------------------------------------------------ 档位与模型
    /**
     * 同一时刻只保留一个 Interpreter：TFLite 的 GPU delegate 多实例会互相抢资源，
     * 切换游戏时先 close 旧的再建新的，代价约 200–400ms。
     */
    private fun loadDetector(store: ModelStore, profile: String) {
        detector?.close()
        detector = null
        if (profile == Prefs.PROFILE_NONE) {
            PetBus.status("档位 none：只对话、不检测")
            return
        }
        // 按 PC 下发的宽度自动挑模型：1920 宽的图配 1280 模型，放大也是白搭；
        // 反过来 640 的图配 1280 模型只是插值，慢 4 倍还没有新信息。
        val suffix = when {
            lastSentWidth >= 1200 -> "1280"
            lastSentWidth >= 900 -> "960"
            else -> ""
        }
        val sizedProfile = if (suffix.isEmpty()) profile else "$profile$suffix"
        val file = store.resolve(sizedProfile, prefs.modelPath)
            ?: store.resolve(profile, prefs.modelPath)
        if (file == null) {
            PetBus.status("档位 $profile 没有模型，仅对话")
            return
        }
        try {
            val loaded = Detector(file, store.readLabels(profile))
            detector = loaded
            PetBus.status(
                "模型 ${file.name} 就绪${if (loaded.usingGpu) " · GPU" else " · CPU"}"
            )
        } catch (error: Throwable) {
            PetBus.status("模型 $profile 加载失败：${error.message ?: error.javaClass.simpleName}")
        }
    }

    // ------------------------------------------------------------ 记忆
    /** 往对局记忆里加一条。 */
    private fun remember(text: String) {
        if (text.isBlank()) return
        matchMemory.add(text)
        while (matchMemory.size > 24) matchMemory.removeAt(0)
    }

    /** 把这一轮记进对话历史，下次能接上话。 */
    private fun rememberChat(userContent: String, assistant: String) {
        if (userContent.isBlank() || assistant.isBlank()) return
        chatHistory.add(JSONObject().put("role", "user").put("content", userContent))
        chatHistory.add(JSONObject().put("role", "assistant").put("content", assistant))
        while (chatHistory.size > 16) chatHistory.removeAt(0)
    }

    // ------------------------------------------------------------ 复读保护
    /**
     * 跟最近几句太像就算复读。
     *
     * 之前「每 2.4 秒一个 clock 事件就播报一次」是复读的主因，
     * 现在 clock 不再触发播报，这里再加一道兜底：宁可不说，也别像机器人。
     */
    private fun isRepeat(text: String): Boolean {
        val current = bigrams(text)
        if (current.isEmpty()) return false
        return recentSpoken.any { previous ->
            val other = bigrams(previous)
            other.isNotEmpty() &&
                current.count { it in other }.toDouble() /
                maxOf(current.size, other.size) > 0.55
        }
    }

    private fun rememberSpoken(text: String) {
        recentSpoken.add(text)
        while (recentSpoken.size > 4) recentSpoken.removeAt(0)
    }

    private fun bigrams(text: String): Set<String> {
        val clean = text.filter { !it.isWhitespace() }
        if (clean.isEmpty()) return emptySet()
        if (clean.length < 2) return setOf(clean)
        return (0 until clean.length - 1).map { clean.substring(it, it + 2) }.toSet()
    }

    // ------------------------------------------------------------ 玩家插话
    /**
     * 玩家说了一句话（PC 麦克风识别后送来的），立刻回一句。
     * 不受「点评间隔」限制——玩家主动问了就不该等。
     */
    private fun handlePlayerTalk(messages: List<Say>, profile: String) {
        for (message in messages) {
            val payload = buildPayload(
                profile, lastValues, lastUnstable, lastCalibrated, lastObjects, lastEvents
            ).put("ask", message.text).put("fromPlayer", true)
            val (text, source) = DeepSeek.ask(
                payload, prefs.apiKey, prefs.deepseekModel, message.text, chatHistory
            )
            val lines = DeepSeek.splitLines(text)
            val pages = mutableListOf<String>()
            pages.add("你：${message.text}")
            pages.addAll(lines)
            PetBus.say(pages, holdMillisFor(text))
            rememberChat(message.text, text)
            updateNotification(buildNotification(lines.firstOrNull() ?: "…", source))
        }
    }

    /** 自动播报时记进对话历史的一句「用户消息」，保持简短，别把整份 payload 存进去。 */
    private fun briefAsk(payload: JSONObject): String {
        val notes = payload.optJSONArray("notes")
        val first = if (notes != null && notes.length() > 0) notes.optString(0) else ""
        return if (first.isNotBlank()) "现在的局面：$first" else "现在这局还在继续"
    }

    // ---------------------------------------------------------------- events
    /**
     * 把这一帧的变化转成事件。
     *
     * 血量格式有两种：LoL 是数组 `hp: [当前, 上限]`，瓦罗兰特是纯数字 `hp: 100`。
     * 之前只认数组，所以**瓦罗兰特死了永远不会触发 death**——这是真 bug。
     */
    private fun trackEvents(
        values: JSONObject,
        unstable: List<String>,
        objects: List<Triple<String, Int, String>>
    ): List<JSONObject> {
        val events = mutableListOf<JSONObject>()

        // 看到敌人就记一笔：等会儿玩家死了，才知道说得出「刚才那波是被打了」
        val enemies = objects.filter { it.first.equals("enemy", ignoreCase = true) }
        if (enemies.isNotEmpty()) {
            lastEnemyAt = System.currentTimeMillis()
            lastEnemyCount = enemies.sumOf { it.second }
        }

        if (values.has("timer")) {
            val timer = values.optInt("timer", -1)
            val old = previous["timer"] as? Int
            if (old != null && timer > old && timer - old <= 6) {
                events.add(JSONObject().put("kind", "clock").put("value", timer))
            }
        }
        for (name in listOf("allyKills", "enemyKills", "level", "gold")) {
            if (!values.has(name)) continue
            val value = values.optInt(name, -1)
            val old = previous[name] as? Int
            if (old != null && !unstable.contains(name) && value != old) {
                events.add(
                    JSONObject().put("kind", "${name}Change")
                        .put("from", old).put("to", value)
                )
            }
        }

        if (values.has("hp")) {
            hpMissingSince = 0L
            val arrayNow = values.optJSONArray("hp")
            val arrayBefore = previous["hp"] as? JSONArray
            val now: Int
            val before: Int
            if (arrayNow != null && arrayNow.length() >= 1) {
                now = arrayNow.optInt(0)
                before = arrayBefore?.optInt(0) ?: -1
            } else {
                now = values.optInt("hp", -1)
                before = previous["hp"] as? Int ?: -1
            }
            if (now == 0 && before > 0) {
                val gap = if (lastEnemyAt > 0) {
                    Math.round((System.currentTimeMillis() - lastEnemyAt) / 1000.0)
                } else {
                    -1
                }
                events.add(
                    JSONObject().put("kind", "death")
                        .put("hpBefore", before)
                        .put("enemyCount", lastEnemyCount)
                        .put("secondsSinceEnemy", gap)
                )
            } else if (now > 0 && before > 0 && before - now >= 15) {
                // 掉了不少血但没死：值得提醒一句「你被打了」
                events.add(
                    JSONObject().put("kind", "hurt")
                        .put("from", before).put("to", now)
                )
            }
        } else if (previous.containsKey("hp")) {
            // 血量 HUD 整块不见了：瓦罗兰特死亡后切到观战视角，自己的血条会直接消失，
            // 读到的不是 0 而是「读不到」。持续 1.2 秒以上才判死，避免 OCR 偶发漏读。
            val now = System.currentTimeMillis()
            if (hpMissingSince == 0L) hpMissingSince = now
            if (now - hpMissingSince > 1200) {
                val before = (previous["hp"] as? Int) ?: (previous["hp"] as? JSONArray)?.optInt(0) ?: -1
                val gap = if (lastEnemyAt > 0) {
                    Math.round((now - lastEnemyAt) / 1000.0)
                } else {
                    -1
                }
                events.add(
                    JSONObject().put("kind", "death")
                        .put("hpBefore", before)
                        .put("enemyCount", lastEnemyCount)
                        .put("secondsSinceEnemy", gap)
                        .put("spectating", true)
                )
                hpMissingSince = 0L
                // 清掉，避免下一帧又判一次死
                previous.remove("hp")
            }
        }

        // 关键事件写进对局记忆，陪玩后面能引用「你刚才死过一次」这种事
        for (event in events) {
            when (event.optString("kind")) {
                "death" -> remember("你被杀了（当时血量 ${event.optInt("hpBefore")}）")
                "hurt" -> remember("你挨了一下，${event.optInt("from")} 掉到 ${event.optInt("to")}")
                "allyKillsChange" -> remember("你们拿下第 ${event.optInt("to")} 个人头")
                "enemyKillsChange" -> remember("对面拿下第 ${event.optInt("to")} 个人头")
            }
        }

        for (name in listOf("timer", "allyKills", "enemyKills", "level", "gold", "hp", "mana")) {
            if (!values.has(name)) continue
            val value = values.opt(name)
            when (value) {
                is Int -> previous[name] = value
                is Double -> previous[name] = value
                is JSONArray -> previous[name] = value
            }
        }
        return events
    }

    private fun buildPayload(
        profile: String,
        values: JSONObject,
        unstable: List<String>,
        calibrated: Boolean,
        objects: List<Triple<String, Int, String>>,
        events: List<JSONObject>
    ): JSONObject {
        val scene = JSONArray()
        for ((label, count, region) in objects) {
            scene.put(JSONObject().put("cls", label).put("count", count).put("region", region))
        }
        val unstableArray = JSONArray()
        unstable.forEach { unstableArray.put(it) }
        val eventArray = JSONArray()
        events.forEach { eventArray.put(it) }
        return JSONObject()
            .put("game", profile)
            .put("scene", JSONObject().put("objects", scene))
            .put(
                "hud", JSONObject()
                    .put("values", values)
                    .put("unstable", unstableArray)
                    .put("calibrated", calibrated)
            )
            .put("events", eventArray)
            .put("ask", "此刻你想对他说什么？")
    }

    /**
     * 自动播报用的精简 payload。
     *
     * 关键改动：**不把 hud 数值表发过去**，只发「情境句」。
     * 模型看到 `{"hp": 23, "ammo": 3}` 就会去复述；看到「你只剩 23 血，快没了」
     * 才会去说人话。这一条比任何提示词都管用，顺带还省 token。
     */
    private fun broadcastPayload(
        profile: String,
        values: JSONObject,
        objects: List<Triple<String, Int, String>>,
        events: List<JSONObject>
    ): JSONObject {
        val notes = JSONArray()
        notableNotes(values).forEach { notes.put(it) }
        for ((label, count, region) in objects) {
            notes.put(sceneSentence(label, count, region))
        }
        val eventArray = JSONArray()
        events.forEach { eventArray.put(it) }

        // 死亡/挨打翻译成人话，顺便把「刚刚看到几个敌人」带上去，
        // 模型才能说出「刚才那波是被他抓的」而不是干巴巴一句「你死了」。
        var died = false
        for (event in events) {
            when (event.optString("kind")) {
                "death" -> {
                    died = true
                    val hpBefore = event.optInt("hpBefore", 0)
                    val gap = event.optInt("secondsSinceEnemy", -1)
                    val enemies = event.optInt("enemyCount", 0)
                    val text = when {
                        enemies > 0 && gap in 0..8 ->
                            "你刚被对面的人打死了（${gap}秒前画面里还有 $enemies 个）"
                        enemies > 0 -> "你被对面的人打死了"
                        else -> "你死了"
                    }
                    notes.put("$text，刚才血量还有 $hpBefore")
                }
                "hurt" -> notes.put(
                    "你挨了一下，血量从 ${event.optInt("from")} 掉到 ${event.optInt("to")}"
                )
            }
        }

        // 把最近说过的话也带上，模型自己就知道别再重复
        val recent = JSONArray()
        recentSpoken.forEach { recent.put(it) }

        // 这一局发生过什么
        val memory = JSONArray()
        matchMemory.takeLast(8).forEach { memory.put(it) }

        return JSONObject()
            .put("game", profile)
            .put("notes", notes)
            .put("events", eventArray)
            .put("recent", recent)
            .put("memory", memory)
            .put(
                "ask", if (died) "他刚被打死了，说句话安慰一下或者给个具体建议"
                else "此刻你想对他说什么？"
            )
    }

    /** 把数值翻译成「值得说的事」。 */
    private fun notableNotes(values: JSONObject): List<String> {
        val notes = mutableListOf<String>()
        val hp = values.optInt("hp", -1)
        if (hp in 1..35) notes.add("你只剩 $hp 血，快没了")
        else if (hp in 36..70) notes.add("血量不太健康")
        val shield = values.optInt("shield", -1)
        if (shield == 0 && hp > 0) notes.add("护甲已经掉光了")
        val ammo = values.optInt("ammo", -1)
        if (ammo in 0..3) notes.add("子弹只剩 $ammo 发")
        val reserve = values.optInt("ammoReserve", -1)
        if (reserve == 0 && ammo in 0..12) notes.add("备弹也空了")
        val credits = values.optInt("credits", -1)
        if (credits in 0..800) notes.add("经济只剩 $credits，可能买不起东西")
        return notes
    }

    /** 检测结果说成人话。 */
    private fun sceneSentence(label: String, count: Int, region: String): String {
        val who = when (label.trim().lowercase()) {
            "enemy" -> "对面"
            "teammate" -> "队友"
            "dropped spike", "planted spike" -> "包"
            else -> label
        }
        val where = when (region) {
            "left" -> "左边"
            "right" -> "右边"
            else -> "中路"
        }
        // 注意：Kotlin 把中文也当标识符字符，`"$where有"` 会被解析成变量 `where有`，
        // 所以变量后面紧跟中文时必须写 ${where}。
        return "${where}有 $count 个$who"
    }

    private fun buildStats(
        profile: String,
        values: JSONObject,
        objects: List<Triple<String, Int, String>>
    ): String {
        val bits = mutableListOf<String>()
        if (profile.isNotBlank() && profile != Prefs.PROFILE_NONE) bits.add(profile)
        if (values.has("timer")) {
            val timer = values.optInt("timer", 0)
            bits.add("%d:%02d".format(timer / 60, timer % 60))
        }
        if (values.has("allyKills") && values.has("enemyKills")) {
            bits.add("${values.optInt("allyKills")}:${values.optInt("enemyKills")}")
        }
        if (values.has("level")) bits.add("${values.optInt("level")} 级")
        val detected = detectSuffix(
            objects, true,
            detector?.lastDetectMs ?: 0.0, detector?.usingGpu == true
        ).removePrefix(" · ")
        if (detected.isNotEmpty() && detected != "未检出目标") bits.add(detected)
        return bits.joinToString(" · ")
    }

    /**
     * 检测摘要，挂在状态行后面：「敌×2 友×1」。
     *
     * 没有模型时明确写「仅对话」，否则「没检出敌人」和「根本没在检测」分不清——
     * 这两种情况在手机上看起来一模一样。
     */
    private fun detectSuffix(
        objects: List<Triple<String, Int, String>>,
        hasDetector: Boolean,
        costMs: Double = 0.0,
        onGpu: Boolean = false
    ): String {
        if (!hasDetector) return " · 仅对话（无模型）"
        val cost = if (costMs > 0) " ${costMs.toInt()}ms${if (onGpu) " GPU" else " CPU"}" else ""
        if (objects.isEmpty()) return " · 未检出$cost"
        val text = objects.joinToString(" ") { (label, count, _) ->
            val short = when (label.trim().lowercase()) {
                "enemy" -> "敌"
                "teammate" -> "友"
                "dropped spike", "planted spike" -> "包"
                else -> label
            }
            "$short×$count"
        }
        return " · $text$cost"
    }

    private fun holdMillisFor(text: String): Long =
        (2200L + text.length * 110L).coerceIn(3000L, 12000L)

    // ---------------------------------------------------------- notification
    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.channel_name),
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = getString(R.string.channel_desc)
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String, source: String = ""): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, CompanionService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_pet)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(if (source == "fallback") "$text（本地兜底）" else text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(0, getString(R.string.notification_stop), stop)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun updateNotification(notification: Notification) {
        try {
            ServiceCompat.startForeground(
                this, NOTIFICATION_ID, notification,
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                } else {
                    0
                }
            )
        } catch (error: Throwable) {
            // 通知权限被拒时不影响服务本身
        }
    }

    // ------------------------------------------------------------ utilities
    private fun acquireWakeLock() {
        try {
            val manager = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = manager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "pet:loop").apply {
                setReferenceCounted(false)
                acquire(4 * 60 * 60 * 1000L)
            }
        } catch (error: Throwable) {
        }
    }

    private fun releaseWakeLock() {
        try {
            wakeLock?.takeIf { it.isHeld }?.release()
        } catch (error: Throwable) {
        }
        wakeLock = null
    }

    private fun sleep(millis: Long) {
        try {
            Thread.sleep(millis)
        } catch (error: InterruptedException) {
            Thread.currentThread().interrupt()
        }
    }

    companion object {
        const val ACTION_START = "com.lolcompanion.pet.START"
        const val ACTION_STOP = "com.lolcompanion.pet.STOP"

        /** Activity 用它决定按钮显示「开始」还是「停止」。 */
        @Volatile
        var isRunning: Boolean = false

        private const val CHANNEL_ID = "companion"
        private const val NOTIFICATION_ID = 1001
        private const val POLL_MIN_MS = 320L
        private const val POLL_IDLE_MS = 220L
        /**
         * 多久跑一次 YOLO。
         *
         * 5 FPS 全跑的话，一帧推理几十到几百毫秒，主循环会被拖到 1.5 FPS，
         * 语音消息和气泡一起卡。陪玩是按秒说话的，1.4 次/秒足够。
         */
        private const val DETECT_INTERVAL_MS = 700L
        private const val DEFAULT_PROFILE = "lol"
    }
}
