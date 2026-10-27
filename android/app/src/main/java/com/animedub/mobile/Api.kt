package com.animedub.mobile

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

const val DEFAULT_PORT = 8765

data class Episode(
    val id: String,
    val title: String,
    val lang: String,
    val folder: String,
    val size: Long,
)

/** Аниме = папка с сериями на компьютере. */
data class Anime(val name: String, val episodes: List<Episode>) {
    val cover: Episode get() = episodes.first()
}

/** Исходная серия на компьютере (ещё не озвученная или уже с дубляжом). */
data class Source(
    val id: String,
    val title: String,
    val folder: String,
    val size: Long,
    val dubbed: List<String>,
)

/** Задание озвучки на компьютере. */
data class Job(
    val id: String,
    val source: String,
    val title: String,
    val folder: String,
    val lang: String,
    val state: String, // queued, running, done, failed, cancelled
    val stage: String,
    val percent: Int,
    val message: String,
) {
    val active: Boolean get() = state == "queued" || state == "running"
}

class ApiException(message: String) : IOException(message)

/** Связь с AnimeDub на компьютере (server.py). */
class Api(host: String, val key: String) {
    val base: String = normalize(host)

    fun videoUrl(id: String) = "$base/video/$id"

    fun thumbUrl(id: String) = "$base/thumb/$id"

    fun check() {
        String(get("/api/info"), Charsets.UTF_8)
    }

    fun list(): List<Episode> {
        val arr = JSONArray(String(get("/api/list"), Charsets.UTF_8))
        return (0 until arr.length()).map {
            val o = arr.getJSONObject(it)
            Episode(o.getString("id"), o.getString("title"), o.getString("lang"),
                o.getString("folder"), o.getLong("size"))
        }
    }

    fun bytes(url: String): ByteArray = fetch(URL(url))

    fun sources(): List<Source> {
        val arr = JSONArray(String(get("/api/sources"), Charsets.UTF_8))
        return (0 until arr.length()).map {
            val o = arr.getJSONObject(it)
            val d = o.getJSONArray("dubbed")
            Source(o.getString("id"), o.getString("title"), o.getString("folder"),
                o.getLong("size"), (0 until d.length()).map { i -> d.getString(i) })
        }
    }

    fun jobs(): List<Job> {
        val arr = JSONArray(String(get("/api/jobs"), Charsets.UTF_8))
        return (0 until arr.length()).map { job(arr.getJSONObject(it)) }
    }

    fun startDub(sourceId: String, lang: String, context: String): Job {
        val body = JSONObject().put("id", sourceId).put("lang", lang).put("context", context)
        return job(JSONObject(String(send("POST", "/api/dub", body.toString().toByteArray()),
            Charsets.UTF_8)))
    }

    fun cancel(jobId: String) {
        send("POST", "/api/jobs/$jobId/cancel", "{}".toByteArray())
    }

    /** Загрузка видео с телефона на компьютер; onProgress — доля от 0 до 1. */
    fun upload(
        folder: String, name: String, size: Long, input: InputStream,
        dub: Boolean, lang: String, context: String, onProgress: (Float) -> Unit,
    ) {
        fun enc(v: String) = URLEncoder.encode(v, "UTF-8")
        val url = URL("$base/api/upload?folder=${enc(folder)}&name=${enc(name)}" +
            "&dub=${if (dub) 1 else 0}&lang=${enc(lang)}&context=${enc(context.take(1500))}")
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = "PUT"
        conn.doOutput = true
        conn.connectTimeout = 8000
        conn.readTimeout = 120000
        conn.setFixedLengthStreamingMode(size)
        conn.setRequestProperty("X-Key", key)
        conn.setRequestProperty("Content-Type", "application/octet-stream")
        try {
            conn.outputStream.use { out ->
                val buf = ByteArray(256 * 1024)
                var sent = 0L
                var last = 0L
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    sent += n
                    if (sent - last > 2 * 1024 * 1024 || sent == size) {
                        last = sent
                        onProgress(if (size > 0) sent.toFloat() / size else 0f)
                    }
                }
            }
            check(conn)
        } catch (e: ApiException) {
            throw e
        } catch (e: IOException) {
            throw ApiException("Загрузка оборвалась: ${e.message}")
        } finally {
            conn.disconnect()
        }
    }

    private fun job(o: JSONObject) = Job(o.getString("id"), o.optString("source"),
        o.getString("title"), o.optString("folder"), o.optString("lang"), o.getString("state"),
        o.optString("stage"), o.optInt("percent"), o.optString("message"))

    private fun send(method: String, path: String, body: ByteArray): ByteArray {
        val conn = URL(base + path).openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.doOutput = true
        conn.connectTimeout = 8000
        conn.readTimeout = 30000
        conn.setRequestProperty("X-Key", key)
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        try {
            try {
                conn.outputStream.use { it.write(body) }
            } catch (e: IOException) {
                throw ApiException("Компьютер не отвечает.")
            }
            return check(conn)
        } finally {
            conn.disconnect()
        }
    }

    /** Код ответа -> данные или понятная ошибка (текст ошибки берём из ответа сервера). */
    private fun check(conn: HttpURLConnection): ByteArray {
        val code = try {
            conn.responseCode
        } catch (e: IOException) {
            throw ApiException("Компьютер не отвечает.")
        }
        if (code == 200) return conn.inputStream.use { it.readBytes() }
        val msg = try {
            JSONObject(String(conn.errorStream.use { it.readBytes() }, Charsets.UTF_8))
                .optString("error")
        } catch (e: Exception) {
            ""
        }
        throw ApiException(when {
            code == 401 -> "Неверный ключ"
            code == 503 -> "Обнови AnimeDub на компьютере — эта версия не умеет озвучивать с телефона."
            msg.isNotEmpty() -> msg
            else -> "Ошибка сервера: $code"
        })
    }

    private fun get(path: String): ByteArray = fetch(URL(base + path))

    private fun fetch(url: URL): ByteArray {
        val conn = url.openConnection() as HttpURLConnection
        conn.connectTimeout = 8000
        conn.readTimeout = 60000
        conn.setRequestProperty("X-Key", key)
        try {
            val code = try {
                conn.responseCode
            } catch (e: IOException) {
                throw ApiException("Компьютер не отвечает. Проверь, что AnimeDub запущен " +
                    "и галочка «Смотреть серии в приложении на Android» включена.")
            }
            if (code == 401) throw ApiException("Неверный ключ")
            if (code != 200) throw ApiException("Ошибка сервера: $code")
            return conn.inputStream.use { it.readBytes() }
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        fun normalize(host: String): String {
            var h = host.trim().removeSuffix("/")
            if (!h.startsWith("http://") && !h.startsWith("https://")) h = "http://$h"
            val afterScheme = h.substringAfter("://")
            if (!afterScheme.contains(":")) h = "$h:$DEFAULT_PORT"
            return h
        }

        fun prefs(ctx: Context) = ctx.getSharedPreferences("animedub", Context.MODE_PRIVATE)

        fun saved(ctx: Context): Api? {
            val p = prefs(ctx)
            val host = p.getString("host", null) ?: return null
            return Api(host, p.getString("key", "") ?: "")
        }

        fun group(episodes: List<Episode>): List<Anime> =
            episodes.groupBy { it.folder }.map { (name, eps) -> Anime(name, eps) }
    }
}
