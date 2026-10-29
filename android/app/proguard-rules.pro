# TFLite 与 OkHttp 的反射调用保持原样即可，当前 release 未开启混淆。
-keep class org.tensorflow.lite.** { *; }
-keep class okhttp3.** { *; }
-dontwarn org.tensorflow.lite.**
