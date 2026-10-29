package com.lolcompanion.pet

/**
 * 前台服务与 Activity 之间的最小通道。同进程、单消费者，不需要广播或 AIDL。
 */
object PetBus {

    /** 气泡内容 + 每页停留毫秒。 */
    var onSay: ((List<String>, Long) -> Unit)? = null

    /** 顶部状态文字，例如「已连接 5 FPS」「连接断开」。 */
    var onStatus: ((String) -> Unit)? = null

    /** HUD 数字与检测摘要，用于页面右上角的小字。 */
    var onStats: ((String) -> Unit)? = null

    fun say(lines: List<String>, holdMs: Long = 5000L) {
        if (lines.isEmpty()) return
        onSay?.invoke(lines, holdMs)
    }

    fun status(text: String) {
        onStatus?.invoke(text)
    }

    fun stats(text: String) {
        onStats?.invoke(text)
    }

    fun clear() {
        onSay = null
        onStatus = null
        onStats = null
    }
}
