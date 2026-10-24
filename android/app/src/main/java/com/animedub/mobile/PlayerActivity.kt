package com.animedub.mobile

import android.app.Activity
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView

/**
 * Плеер серии. Дорожки «Дубляж» / «Оригинал» и субтитры переключаются
 * через кнопку настроек в плеере. Место остановки запоминается.
 */
@OptIn(UnstableApi::class)
class PlayerActivity : Activity() {
    private var player: ExoPlayer? = null
    private lateinit var view: PlayerView
    private val id by lazy { intent.getStringExtra("id") ?: "" }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        view = PlayerView(this).apply {
            keepScreenOn = true
            setShowSubtitleButton(true)
            setShowNextButton(false)
            setShowPreviousButton(false)
        }
        setContentView(view)
    }

    override fun onStart() {
        super.onStart()
        hideSystemBars()
        val http = DefaultHttpDataSource.Factory()
            .setDefaultRequestProperties(mapOf("X-Key" to (intent.getStringExtra("key") ?: "")))
            .setConnectTimeoutMs(15000)
            .setReadTimeoutMs(30000)
        val p = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(http))
            .build()
        p.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                Toast.makeText(this@PlayerActivity,
                    "Не удалось воспроизвести: ${error.errorCodeName}. " +
                        "Проверь связь с компьютером; видео в 10-битном H.264 телефоны часто не тянут.",
                    Toast.LENGTH_LONG).show()
            }

            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED) {
                    Api.prefs(this@PlayerActivity).edit().remove("pos_$id").apply()
                    finish()
                }
            }
        })
        p.setMediaItem(MediaItem.fromUri(intent.getStringExtra("url") ?: ""),
            Api.prefs(this).getLong("pos_$id", 0))
        p.prepare()
        p.playWhenReady = true
        view.player = p
        player = p
    }

    override fun onStop() {
        player?.let { p ->
            val pos = p.currentPosition
            val dur = p.duration
            val e = Api.prefs(this).edit()
            if (p.playbackState != Player.STATE_ENDED && pos > 5000 &&
                (dur <= 0 || pos < dur - 30000)) {
                e.putLong("pos_$id", pos)
            } else if (dur > 0 && pos >= dur - 30000) {
                e.remove("pos_$id")
            }
            e.apply()
            p.release()
        }
        player = null
        view.player = null
        super.onStop()
    }

    @Suppress("DEPRECATION")
    private fun hideSystemBars() {
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            or View.SYSTEM_UI_FLAG_FULLSCREEN
            or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION)
    }
}
