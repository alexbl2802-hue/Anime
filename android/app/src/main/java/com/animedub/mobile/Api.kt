package com.animedub.mobile

import android.content.Context
import org.json.JSONArray
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

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
