package com.animedub.mobile

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper
import android.util.LruCache
import android.widget.ImageView
import java.util.concurrent.Executors

/** Простая загрузка кадров-обложек с компьютера с кэшем в памяти. */
object Images {
    private val cache = object : LruCache<String, Bitmap>(24 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap) = value.byteCount
    }
    private val pool = Executors.newFixedThreadPool(3)
    private val main = Handler(Looper.getMainLooper())
    private val failed = HashSet<String>()

    fun load(api: Api, id: String, into: ImageView) {
        val url = api.thumbUrl(id)
        into.tag = url
        cache.get(url)?.let {
            into.setImageBitmap(it)
            return
        }
        into.setImageDrawable(null)
        if (synchronized(failed) { url in failed }) return
        pool.execute {
            val bmp = try {
                val data = api.bytes(url)
                BitmapFactory.decodeByteArray(data, 0, data.size)
            } catch (e: Exception) {
                null
            }
            if (bmp == null) {
                synchronized(failed) { failed.add(url) }
                return@execute
            }
            cache.put(url, bmp)
            main.post { if (into.tag == url) into.setImageBitmap(bmp) }
        }
    }

    fun forget() {
        cache.evictAll()
        synchronized(failed) { failed.clear() }
    }
}
