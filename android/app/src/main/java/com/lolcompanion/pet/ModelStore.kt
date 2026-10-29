package com.lolcompanion.pet

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * 模型热换，按游戏档位分文件：
 *
 *   /Android/data/com.lolcompanion.pet/files/models/<profile>.tflite
 *   /Android/data/com.lolcompanion.pet/files/models/<profile>.labels.txt
 *   /Android/data/com.lolcompanion.pet/files/models/current.tflite     ← 通用兜底
 *
 * profile 与 PC 端 `pc_capture.py --profile <name>` 同名（lol / valorant / delta）。
 * 手机同一时刻只加载一个模型：GPU delegate 多实例会互相抢资源，切换时先 close 再建。
 */
class ModelStore(private val context: Context) {

    val dir: File
        get() {
            val base = context.getExternalFilesDir(null) ?: context.filesDir
            return File(base, "models").apply { mkdirs() }
        }

    val genericModel: File get() = File(dir, "current.tflite")
    val internalFallback: File get() = File(File(context.filesDir, "models"), "current.tflite")

    fun modelFile(profile: String): File =
        if (profile.isBlank() || profile == PROFILE_GENERIC) genericModel
        else File(dir, "$profile.tflite")

    fun labelsFile(profile: String): File =
        if (profile.isBlank() || profile == PROFILE_GENERIC) File(dir, "labels.txt")
        else File(dir, "$profile.labels.txt")

    /** 找这个档位能用的权重：手填路径 → 档位专属 → 通用 current.tflite → 内部存储兜底。 */
    fun resolve(profile: String, overridePath: String = ""): File? {
        val override = if (overridePath.isBlank()) null else File(overridePath)
        return listOfNotNull(override, modelFile(profile), genericModel, internalFallback)
            .firstOrNull { it.isFile && it.length() > 1024 }
    }

    fun readLabels(profile: String): List<String> {
        for (file in listOf(labelsFile(profile), File(dir, "labels.txt"))) {
            if (!file.isFile) continue
            val lines = file.readLines().map { it.trim() }.filter { it.isNotEmpty() }
            if (lines.isNotEmpty()) return lines
        }
        return try {
            context.assets.open("coco.names").bufferedReader().useLines { sequence ->
                sequence.map { it.trim() }.filter { it.isNotEmpty() }.toList()
            }
        } catch (error: IOException) {
            emptyList()
        }
    }

    /** 已经放进来的档位（用于设置页提示）。 */
    fun availableProfiles(): List<String> {
        val names = dir.listFiles { file -> file.isFile && file.name.endsWith(".tflite") }
            ?.map { it.name.removeSuffix(".tflite") }
            ?.filter { it != PROFILE_GENERIC }
            ?: emptyList()
        return names.sorted()
    }

    /**
     * 从系统文件选择器导入。
     *
     * 按扩展名分流：`.tflite` 写到 ``<profile>.tflite``，`.txt` / `.names` 写到
     * ``<profile>.labels.txt``。标签文件必须能单独导入——缺少它时 [readLabels] 会退回
     * `assets/coco.names`，4 类模型的 0-3 号会被叫成 person/bicycle/car/motorcycle。
     */
    @Throws(IOException::class)
    fun importFrom(uri: Uri, profile: String): File {
        val display = displayName(uri).orEmpty().lowercase()
        val isLabels = display.endsWith(".txt") || display.endsWith(".names")
        val target = if (isLabels) labelsFile(profile) else modelFile(profile)
        val minBytes = if (isLabels) 4L else 1024L

        val temp = File(dir, "${target.name}.tmp")
        context.contentResolver.openInputStream(uri).use { input ->
            requireNotNull(input) { "打不开这个文件" }
            temp.outputStream().use { output -> input.copyTo(output) }
        }
        if (temp.length() < minBytes) {
            temp.delete()
            throw IOException(if (isLabels) "标签文件太小，不像类别列表" else "文件太小，不像是 tflite 模型")
        }
        if (target.exists()) target.delete()
        if (!temp.renameTo(target)) throw IOException("写入 ${target.name} 失败")
        return target
    }

    private fun displayName(uri: Uri): String? = try {
        context.contentResolver
            .query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
            ?.use { cursor -> if (cursor.moveToFirst()) cursor.getString(0) else null }
    } catch (error: Throwable) {
        null
    }

    /** 从 URL 下载模型，先落临时文件再替换。 */
    @Throws(IOException::class)
    fun download(url: String, profile: String): File {
        require(url.startsWith("http://") || url.startsWith("https://")) { "需要 http(s) 地址" }
        val client = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build()
        val request = Request.Builder().url(url).get().build()
        val target = modelFile(profile)
        val temp = File(dir, "${target.name}.tmp")
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IOException("HTTP ${response.code}")
            val body = response.body ?: throw IOException("空响应")
            temp.outputStream().use { output -> body.byteStream().copyTo(output) }
        }
        if (temp.length() < 1024 * 100) {
            val size = temp.length()
            temp.delete()
            throw IOException("下载内容太小（$size 字节），可能不是模型")
        }
        if (target.exists()) target.delete()
        if (!temp.renameTo(target)) throw IOException("替换模型失败")
        return target
    }

    fun describe(profile: String): String {
        val model = resolve(profile) ?: return "档位 $profile：未放模型，仅对话"
        val megabytes = model.length() / 1024.0 / 1024.0
        return "档位 %s：%s %.1fMB · %d 类".format(
            profile.ifBlank { PROFILE_GENERIC }, model.name, megabytes, readLabels(profile).size
        )
    }

    companion object {
        /** 不属于任何游戏档位的通用权重位。 */
        const val PROFILE_GENERIC = "generic"
    }
}
