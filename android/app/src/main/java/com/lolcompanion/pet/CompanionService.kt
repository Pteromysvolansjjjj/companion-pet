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
        var loadedProfile = profile
        loadDetector(store, profile)

        var lastSpokeAt = 0L
        var failures = 0

        while (running.get()) {
            val startedAt = System.currentTimeMillis()
            try {
                val tick = ticker?.pull(detector != null) ?: break
                failures = 0
                if (tick.unchanged) {
                    sleep(POLL_IDLE_MS)
                    continue
                }

                // PC 端换了 --profile：同一个 APK 自动换模型，不用手点
                if (prefs.profileMode == Prefs.PROFILE_AUTO &&
                    tick.profile.isNotBlank() && tick.profile != loadedProfile
                ) {
                    loadedProfile = tick.profile
                    profile = tick.profile
                    loadDetector(store, profile)
                }

                val values = tick.values
                val unstable = tick.unstable
                val events = trackEvents(values, unstable)

                val objects = mutableListOf<Triple<String, Int, String>>()
                val activeDetector = detector
                if (activeDetector != null && tick.image != null) {
                    try {
                        val bitmap = BitmapFactory.decodeByteArray(tick.image, 0, tick.image.size)
                        if (bitmap != null) {
                            objects += Detector.summarise(activeDetector.detect(bitmap))
                            bitmap.recycle()
                        }
                    } catch (error: Throwable) {
                        // 单帧推理失败不影响主循环
                    }
                }

                val emptyHud = values.length() == 0
                val due = events.isNotEmpty() ||
                    System.currentTimeMillis() - lastSpokeAt >= prefs.intervalSec * 1000L
                if (emptyHud && objects.isEmpty()) {
                    PetBus.status("已连接，画面里还没有可读数字")
                    sleep(POLL_IDLE_MS)
                    continue
                }

                PetBus.status("已连接 · ${tick.ocrMs.toInt()}ms")
                PetBus.stats(buildStats(profile, values))

                if (due) {
                    val payload = buildPayload(profile, values, unstable, tick.calibrated, objects, events)
                    val (text, source) = DeepSeek.ask(payload, prefs.apiKey, prefs.deepseekModel)
                    val lines = DeepSeek.splitLines(text)
                    if (lines.isNotEmpty()) {
                        PetBus.say(lines, holdMillisFor(text))
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
        val file = store.resolve(profile, prefs.modelPath)
        if (file == null) {
            PetBus.status("档位 $profile 没有模型，仅对话")
            return
        }
        try {
            val loaded = Detector(file, store.readLabels(profile))
            detector = loaded
            PetBus.status("模型 $profile 就绪${if (loaded.usingGpu) " · GPU" else ""}")
        } catch (error: Throwable) {
            PetBus.status("模型 $profile 加载失败：${error.message ?: error.javaClass.simpleName}")
        }
    }

    // ---------------------------------------------------------------- events
    private fun trackEvents(values: JSONObject, unstable: List<String>): List<JSONObject> {
        val events = mutableListOf<JSONObject>()
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
            val health = values.optJSONArray("hp")
            val old = previous["hp"] as? JSONArray
            if (health != null && health.length() >= 1 && old != null && old.length() >= 1) {
                if (health.optInt(0) == 0 && old.optInt(0) > 0) {
                    events.add(JSONObject().put("kind", "death"))
                }
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
            .put("ask", "看一眼现在该干嘛，一句话。")
    }

    private fun buildStats(profile: String, values: JSONObject): String {
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
        return bits.joinToString(" · ")
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
        private const val DEFAULT_PROFILE = "lol"
    }
}
