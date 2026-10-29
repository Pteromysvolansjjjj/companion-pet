package com.lolcompanion.pet

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import org.tensorflow.lite.DataType
import org.tensorflow.lite.Interpreter
import org.tensorflow.lite.gpu.CompatibilityList
import org.tensorflow.lite.gpu.GpuDelegate
import java.io.Closeable
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

data class Detection(
    val label: String,
    val score: Float,
    /** 中心点占画面宽高的比例，0..1。 */
    val cx: Float,
    val cy: Float
)

/**
 * YOLOv8 TFLite 推理 + 后处理。
 *
 * 输入约定 1x640x640x3 float32（0..1），输出 1x84x8400（或转置成 1x8400x84）。
 * 模型不存在时不要构造这个类，服务会走「只调 DeepSeek、无检测」的降级路径。
 */
class Detector(
    modelFile: File,
    private val labels: List<String>,
    // 实测瓦罗兰特模型的 enemy 类置信度只有 0.26-0.29（640 宽下发的真实对局帧），
    // 统一用 0.30 会把敌人整个滤掉，所以人形类单独放宽，见 thresholdFor。
    private val conf: Float = 0.30f,
    private val iou: Float = 0.45f,
    threads: Int = 4
) : Closeable {

    /**
     * 按类别给阈值。
     *
     * 陪玩只需要「画面里有没有人」，漏报的代价远大于误报，所以 enemy/teammate 放宽到
     * 0.15；spike 是小图标，低置信度容易把道具认成人形，保持严格。
     */
    private fun thresholdFor(label: String): Float = when (label.trim().lowercase()) {
        "enemy", "teammate" -> min(conf, 0.15f)
        else -> conf
    }

    private val interpreter: Interpreter
    private var gpuDelegate: GpuDelegate? = null
    private val inputSize: Int
    private val channelFirst: Boolean
    private val classCount: Int
    private val anchorCount: Int
    private val transposed: Boolean
    private val inputBuffer: ByteBuffer
    private val output: Array<Array<FloatArray>>

    var usingGpu: Boolean = false
        private set

    init {
        var delegate: GpuDelegate? = null
        val options = Interpreter.Options().setNumThreads(threads)
        try {
            if (CompatibilityList().isDelegateSupportedOnThisDevice) {
                delegate = GpuDelegate()
                options.addDelegate(delegate)
            }
        } catch (error: Throwable) {
            delegate?.close()
            delegate = null
        }
        gpuDelegate = delegate
        interpreter = try {
            Interpreter(modelFile, options)
        } catch (error: Throwable) {
            // GPU delegate 建起来了但模型不吃，退回 CPU 再试一次
            delegate?.close()
            gpuDelegate = null
            Interpreter(modelFile, Interpreter.Options().setNumThreads(threads))
        }
        usingGpu = gpuDelegate != null

        val inputShape = interpreter.getInputTensor(0).shape()
        require(inputShape.size == 4) { "输入维度异常: ${inputShape.joinToString()}" }
        channelFirst = inputShape[1] == 3
        inputSize = if (channelFirst) inputShape[2] else inputShape[1]
        val dtype = interpreter.getInputTensor(0).dataType()
        require(dtype == DataType.FLOAT32) {
            "模型输入是 $dtype，需要 float32（导出时不要 quantization）"
        }

        val outputShape = interpreter.getOutputTensor(0).shape()
        require(outputShape.size == 3) { "输出维度异常: ${outputShape.joinToString()}" }
        if (outputShape[1] < outputShape[2]) {
            transposed = false
            classCount = outputShape[1]
            anchorCount = outputShape[2]
        } else {
            transposed = true
            classCount = outputShape[2]
            anchorCount = outputShape[1]
        }
        output = Array(1) { Array(outputShape[1]) { FloatArray(outputShape[2]) } }

        val bytesPerChannel = 4
        inputBuffer = ByteBuffer
            .allocateDirect(inputSize * inputSize * 3 * bytesPerChannel)
            .order(ByteOrder.nativeOrder())
    }

    fun detect(bitmap: Bitmap): List<Detection> {
        val originalWidth = bitmap.width
        val originalHeight = bitmap.height
        if (originalWidth <= 0 || originalHeight <= 0) return emptyList()

        // 只取画面上部。底部那条是玩家自己的手和武器，模型会把它们认成人形——
        // 实测 250 帧实战画面里，检出的框 11/13 落在下半部，全是自己的武器；
        // 真正的敌人都在上半部，反而漏检。裁掉底部能同时减少误报和干扰。
        val keepHeight = (originalHeight * KEEP_RATIO).toInt().coerceAtLeast(1)
        val cropped = keepHeight < originalHeight
        val source = if (cropped) {
            Bitmap.createBitmap(bitmap, 0, 0, originalWidth, keepHeight)
        } else {
            bitmap
        }
        val sourceWidth = source.width
        val sourceHeight = source.height

        val scale = min(inputSize.toFloat() / sourceWidth, inputSize.toFloat() / sourceHeight)
        val scaledWidth = (sourceWidth * scale).roundToInt().coerceAtLeast(1)
        val scaledHeight = (sourceHeight * scale).roundToInt().coerceAtLeast(1)
        val padX = (inputSize - scaledWidth) / 2
        val padY = (inputSize - scaledHeight) / 2

        val canvasBitmap = Bitmap.createBitmap(inputSize, inputSize, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(canvasBitmap)
        canvas.drawColor(Color.rgb(114, 114, 114))
        val paint = Paint(Paint.FILTER_BITMAP_FLAG)
        canvas.drawBitmap(
            Bitmap.createScaledBitmap(source, scaledWidth, scaledHeight, true),
            padX.toFloat(), padY.toFloat(), paint
        )

        val pixels = IntArray(inputSize * inputSize)
        canvasBitmap.getPixels(pixels, 0, inputSize, 0, 0, inputSize, inputSize)
        canvasBitmap.recycle()

        inputBuffer.rewind()
        if (channelFirst) {
            for (channel in 0 until 3) {
                val shift = when (channel) {
                    0 -> 16
                    1 -> 8
                    else -> 0
                }
                for (pixel in pixels) {
                    inputBuffer.putFloat(((pixel shr shift) and 0xFF) / 255f)
                }
            }
        } else {
            for (pixel in pixels) {
                inputBuffer.putFloat(((pixel shr 16) and 0xFF) / 255f)
                inputBuffer.putFloat(((pixel shr 8) and 0xFF) / 255f)
                inputBuffer.putFloat((pixel and 0xFF) / 255f)
            }
        }

        interpreter.run(inputBuffer, output)

        // 先收集过阈值的候选，再做贪心 NMS
        val candidates = ArrayList<Candidate>(64)
        for (anchor in 0 until anchorCount) {
            var bestScore = 0f
            var bestClass = -1
            for (classIndex in 4 until classCount) {
                val score = scoreAt(classIndex, anchor)
                if (score > bestScore) {
                    bestScore = score
                    bestClass = classIndex - 4
                }
            }
            if (bestClass < 0 || bestScore < conf) continue
            val label = if (bestClass < labels.size) labels[bestClass] else bestClass.toString()
            if (bestScore < thresholdFor(label)) continue
            val cx = coordAt(0, anchor)
            val cy = coordAt(1, anchor)
            val width = coordAt(2, anchor)
            val height = coordAt(3, anchor)
            candidates.add(
                Candidate(
                    label = label,
                    score = bestScore,
                    x = cx - width / 2f,
                    y = cy - height / 2f,
                    width = width,
                    height = height
                )
            )
        }
        if (candidates.isEmpty()) return emptyList()

        candidates.sortByDescending { it.score }
        val kept = ArrayList<Candidate>(candidates.size)
        for (candidate in candidates) {
            if (kept.size >= MAX_DETECTIONS) break
            var overlaps = false
            for (chosen in kept) {
                if (chosen.label == candidate.label && iouOf(chosen, candidate) > iou) {
                    overlaps = true
                    break
                }
            }
            if (!overlaps) kept.add(candidate)
        }

        val result = kept.map { candidate ->
            val centerX = candidate.x + candidate.width / 2f
            val centerY = candidate.y + candidate.height / 2f
            val cxValue = ((centerX - padX) / scale / sourceWidth).coerceIn(0f, 1f)
            val cyValue = ((centerY - padY) / scale / sourceHeight).coerceIn(0f, 1f)
            Detection(
                label = candidate.label,
                score = candidate.score,
                cx = cxValue,
                // 裁剪过的话，把比例换算回原始画面
                cy = if (cropped) cyValue * sourceHeight / originalHeight else cyValue
            )
        }
        if (cropped) source.recycle()
        return result
    }

    private fun scoreAt(classIndex: Int, anchor: Int): Float =
        if (transposed) output[0][anchor][classIndex] else output[0][classIndex][anchor]

    private fun coordAt(index: Int, anchor: Int): Float =
        if (transposed) output[0][anchor][index] else output[0][index][anchor]

    private fun iouOf(a: Candidate, b: Candidate): Float {
        val left = max(a.x, b.x)
        val top = max(a.y, b.y)
        val right = min(a.x + a.width, b.x + b.width)
        val bottom = min(a.y + a.height, b.y + b.height)
        val intersection = max(0f, right - left) * max(0f, bottom - top)
        if (intersection <= 0f) return 0f
        val union = a.width * a.height + b.width * b.height - intersection
        return if (union <= 0f) 0f else intersection / union
    }

    override fun close() {
        try {
            interpreter.close()
        } catch (_: Throwable) {
        }
        try {
            gpuDelegate?.close()
        } catch (_: Throwable) {
        }
        gpuDelegate = null
    }

    private data class Candidate(
        val label: String,
        val score: Float,
        val x: Float,
        val y: Float,
        val width: Float,
        val height: Float
    )

    companion object {
        private const val MAX_DETECTIONS = 24

        /**
         * 只保留画面上部这么高再送模型。
         *
         * 底部约 18% 是玩家自己的手和武器，模型会把它们高置信度认成 enemy，
         * 实测 250 帧实战画面里 13 个检出有 11 个落在下半部、全是误报。
         */
        private const val KEEP_RATIO = 0.82f

        /** 只把「类别 + 画面三分区」交给模型，坐标和数量都去过隐私。 */
        fun summarise(detections: List<Detection>): List<Triple<String, Int, String>> {
            if (detections.isEmpty()) return emptyList()
            val buckets = LinkedHashMap<Pair<String, String>, Int>()
            for (item in detections) {
                val region = when {
                    item.cx < 1f / 3f -> "left"
                    item.cx > 2f / 3f -> "right"
                    else -> "center"
                }
                val key = item.label to region
                buckets[key] = (buckets[key] ?: 0) + 1
            }
            return buckets.entries
                .sortedByDescending { it.value }
                .take(8)
                .map { Triple(it.key.first, it.value, it.key.second) }
        }
    }
}
