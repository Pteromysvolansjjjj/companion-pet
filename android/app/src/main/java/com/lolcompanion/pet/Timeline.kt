package com.lolcompanion.pet

import org.json.JSONObject
import kotlin.math.abs

/**
 * 最近一段时间的状态变化序列。
 *
 * 只记变化点，不记快照。依据是实测：
 *   10 秒窗口 / 320ms 采样 = 31 帧
 *   全量快照  434 token
 *   只发变化   21 token
 * 差 20 倍，信息量却完全一样 —— 游戏里大部分时间画面是静止的（架枪、走位），
 * 31 帧里往往 28 帧数字一个都没变。
 *
 * 它的用途不是「报告状态」，而是让模型**自己从过程里推断发生了什么**：
 * 看到 h100 → h69 → h0 且 e1l 一直在，它能得出「被左边那个人打死的」；
 * 只给它一句 kind=death 它说不出来。
 *
 * 输出格式：
 *
 *     0.0 h100 s50 a30/60 e0 t1:12
 *     +1.3 h69 s19 a28/60 e1l
 *     +2.4 h0
 *
 * 字段：h=血量 s=护甲 a=弹药/备弹 e=可见敌人数（l左 c中 r右）t=计时
 * h? 表示血条读不到 —— 这正是死亡切观战视角的特征。
 * +a~b 表示这段时间里同一字段连续变化，只保留终值。
 */
class Timeline(
    private val windowMs: Long = 30_000L,
    private val maxPoints: Int = 12
) {
    private class Sample(
        val at: Long,
        val hp: Int?,
        val shield: Int?,
        val ammo: Int?,
        val reserve: Int?,
        val enemies: Int,
        val region: String,
        val timer: Int?
    )

    private class Row(
        val fields: List<String>,
        val text: String,
        var weight: Int,
        val from: Double,
        var to: Double
    )

    private val samples = ArrayList<Sample>(64)

    fun reset() = samples.clear()

    /** 每帧调一次。values 为空（还没读到 HUD）时不记，避免脏数据污染时间线。 */
    fun record(values: JSONObject, objects: List<Triple<String, Int, String>>) {
        if (values.length() == 0) return
        val now = System.currentTimeMillis()
        val enemies = objects.filter { it.first.equals("enemy", ignoreCase = true) }
        samples.add(
            Sample(
                at = now,
                hp = readHp(values),
                shield = if (values.has("shield")) values.optInt("shield", -1) else null,
                ammo = if (values.has("ammo")) values.optInt("ammo", -1) else null,
                reserve = if (values.has("ammoReserve")) values.optInt("ammoReserve", -1) else null,
                enemies = enemies.sumOf { it.second },
                region = enemies.firstOrNull()?.third ?: "c",
                timer = if (values.has("timer")) values.optInt("timer", -1) else null
            )
        )

        val cutoff = now - windowMs
        while (samples.size > 2 && samples.first().at < cutoff) {
            samples.removeAt(0)
        }
        // 兜底：窗口失效（比如一直读不到变化）时别让链表无限长
        if (samples.size > 400) samples.subList(0, 200).clear()
    }

    /**
     * 压成变化序列。
     *
     * 两步：先把同一时刻的多个字段变化合成一行（+1.3 h69 s19 a28/60 e1l），
     * 再把相邻的、只有一个字段且相同的变化合并成区间（弹药每打一枪扣一发，
     * 逐条记会吃掉全部行数，把血量变化挤出去）。
     */
    fun digest(): String {
        if (samples.size < 2) return ""
        val base = samples.first()

        // 第一步：逐帧比对，聚成行
        val rows = ArrayList<Row>(32)
        var reference = base
        for (index in 1 until samples.size) {
            val sample = samples[index]
            val changes = diffOf(reference, sample)
            if (changes.isEmpty()) continue
            reference = sample
            val fields = changes.map { it.first }
            var weight = index
            for (field in fields) weight += FIELD_WEIGHT[field] ?: 5
            weight += bonus(sample)
            val seconds = (sample.at - base.at) / 1000.0
            rows.add(
                Row(
                    fields = fields,
                    text = changes.joinToString(" ") { it.second },
                    weight = weight,
                    from = seconds,
                    to = seconds
                )
            )
        }

        // 第二步：相邻单字段同类变化合并成区间
        val merged = ArrayList<Row>(rows.size)
        for (row in rows) {
            val previous = merged.lastOrNull()
            if (previous != null &&
                previous.fields.size == 1 && row.fields.size == 1 &&
                previous.fields[0] == row.fields[0]
            ) {
                previous.to = row.to
                previous.text = row.text
                previous.weight = maxOf(previous.weight, row.weight)
                continue
            }
            merged.add(row)
        }

        val lines = ArrayList<String>(merged.size + 1)
        val weights = ArrayList<Int>(merged.size + 1)
        lines.add(formatBase(base))
        weights.add(1_000_000)
        for (row in merged) {
            val span = if (abs(row.to - row.from) < 0.05) "" else "~%.1f".format(row.to)
            lines.add("+%.1f%s %s".format(row.from, span, row.text))
            weights.add(row.weight)
        }
        if (lines.size <= maxPoints) return lines.joinToString("\n")

        // 太长时按权重抽样：血量/敌人/死亡天然权重高，弹药低
        val keep = ArrayList<Int>(maxPoints)
        keep.add(0)
        val rest = (1 until lines.size).sortedByDescending { weights[it] }.take(maxPoints - 1)
        keep.addAll(rest)
        keep.sort()
        return keep.joinToString("\n") { lines[it] }
    }

    // ---------------------------------------------------------------- 内部

    private fun readHp(values: JSONObject): Int? {
        if (!values.has("hp")) return null
        val array = values.optJSONArray("hp")
        if (array != null && array.length() >= 1) return array.optInt(0)
        val scalar = values.optInt("hp", -1)
        return if (scalar >= 0) scalar else null
    }

    private fun formatBase(sample: Sample): String {
        val parts = ArrayList<String>(6)
        parts.add(sample.hp?.let { "h$it" } ?: "h?")
        sample.shield?.let { parts.add("s$it") }
        val ammo = sample.ammo
        val reserve = sample.reserve
        if (ammo != null || reserve != null) {
            parts.add("a${ammo ?: "-"}/${reserve ?: "-"}")
        }
        parts.add("e${sample.enemies}")
        sample.timer?.let { parts.add("t%d:%02d".format(it / 60, it % 60)) }
        return "0.0 " + parts.joinToString(" ")
    }

    /** @return 字段名到「渲染文本」的映射，按顺序。 */
    private fun diffOf(before: Sample, after: Sample): List<Pair<String, String>> {
        val changes = ArrayList<Pair<String, String>>(4)
        if (after.hp != before.hp) {
            changes.add("h" to (after.hp?.let { "h$it" } ?: "h?"))
        }
        if (after.shield != before.shield) {
            changes.add("s" to (after.shield?.let { "s$it" } ?: "s?"))
        }
        if (after.ammo != before.ammo || after.reserve != before.reserve) {
            changes.add("a" to "a${after.ammo ?: "-"}/${after.reserve ?: "-"}")
        }
        if (after.enemies != before.enemies) {
            val tag = if (after.enemies > 0) after.region.take(1) else ""
            changes.add("e" to "e${after.enemies}$tag")
        }
        return changes
    }

    /** 关键节点的额外加分，保证抽样时不被弹药这类高频小变化挤掉。 */
    private fun bonus(sample: Sample): Int {
        var score = 0
        if (sample.hp != null && sample.hp == 0) score += 60
        if (sample.hp == null) score += 40
        if (sample.enemies > 0) score += 20
        if (sample.ammo != null && sample.ammo == 0) score += 15
        return score
    }

    private companion object {
        val FIELD_WEIGHT = mapOf("h" to 50, "e" to 40, "s" to 25, "a" to 10)
    }
}
