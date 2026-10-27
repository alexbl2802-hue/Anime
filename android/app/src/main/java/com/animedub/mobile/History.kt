package com.animedub.mobile

import android.content.Context
import org.json.JSONObject

/** Запись истории: где остановился в серии. */
data class Watch(
    val id: String,
    val title: String,
    val anime: String,
    val position: Long,
    val duration: Long,
    val time: Long,
) {
    val progress: Float get() = if (duration > 0) position.toFloat() / duration else 0f
    val finished: Boolean get() = duration > 0 && position >= duration - 30_000
}

/** История просмотра в SharedPreferences (JSON на каждую серию). */
object History {
    private fun prefs(ctx: Context) = ctx.getSharedPreferences("history", Context.MODE_PRIVATE)

    fun all(ctx: Context): List<Watch> = prefs(ctx).all.values.mapNotNull { v ->
        try {
            val o = JSONObject(v as String)
            Watch(o.getString("id"), o.getString("title"), o.getString("anime"),
                o.getLong("pos"), o.getLong("dur"), o.getLong("time"))
        } catch (e: Exception) {
            null
        }
    }.sortedByDescending { it.time }

    fun get(ctx: Context, id: String): Watch? = all(ctx).firstOrNull { it.id == id }

    fun save(ctx: Context, id: String, title: String, anime: String, pos: Long, dur: Long) {
        val o = JSONObject().put("id", id).put("title", title).put("anime", anime)
            .put("pos", pos).put("dur", dur).put("time", System.currentTimeMillis())
        prefs(ctx).edit().putString(id, o.toString()).apply()
    }

    fun clear(ctx: Context) = prefs(ctx).edit().clear().apply()

    /** С какого места начинать: досмотренную серию — сначала. */
    fun resumeAt(ctx: Context, id: String): Long {
        val w = get(ctx, id) ?: return 0
        return if (w.finished || w.position < 5000) 0 else w.position
    }
}
