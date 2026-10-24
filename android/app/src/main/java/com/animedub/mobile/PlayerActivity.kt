package com.animedub.mobile

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.media.AudioManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.TextUtils
import android.view.GestureDetector
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.PlaybackParameters
import androidx.media3.common.Player
import androidx.media3.common.TrackSelectionOverride
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import kotlin.math.abs

/**
 * Плеер серии со своими элементами управления:
 * двойное касание слева/справа — перемотка на 10 с, свайп слева — яркость, справа — громкость,
 * пропуск опенинга, скорость, дорожка «Дубляж»/«Оригинал», субтитры, блокировка, следующая серия.
 */
@OptIn(UnstableApi::class)
class PlayerActivity : Activity() {
    private var player: ExoPlayer? = null
    private lateinit var view: PlayerView
    private lateinit var controls: FrameLayout
    private lateinit var lockOnly: ImageView
    private lateinit var playBtn: ImageView
    private lateinit var seek: SeekBar
    private lateinit var timeNow: TextView
    private lateinit var timeAll: TextView
    private lateinit var speedBtn: TextView
    private lateinit var nextBtn: View
    private lateinit var hint: TextView
    private lateinit var subtitle: TextView

    private val handler = Handler(Looper.getMainLooper())
    private val api by lazy { Api.saved(this) }
    private val anime by lazy { intent.getStringExtra("anime") ?: "" }
    private val ids by lazy { intent.getStringArrayExtra("ids") ?: emptyArray() }
    private val titles by lazy { intent.getStringArrayExtra("titles") ?: emptyArray() }
    private var index = 0
    private var locked = false
    private var dragging = false
    private var speed = 1f

    private val id get() = ids.getOrElse(index) { "" }
    private val epTitle get() = titles.getOrElse(index) { "" }

    private fun dp(v: Int) = Ui.dp(this, v)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        index = savedInstanceState?.getInt("index") ?: intent.getIntExtra("index", 0)
        val root = FrameLayout(this).apply { setBackgroundColor(0xFF000000.toInt()) }
        view = PlayerView(this).apply {
            useController = false
            keepScreenOn = true
            resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
        }
        root.addView(view, FrameLayout.LayoutParams(-1, -1))
        controls = buildControls()
        root.addView(controls, FrameLayout.LayoutParams(-1, -1))
        hint = Ui.text(this, "", 18f, Ui.TEXT, true).apply {
            background = Ui.rounded(this@PlayerActivity, 0xAA000000.toInt(), 14)
            setPadding(dp(18), dp(10), dp(18), dp(10))
            visibility = View.GONE
        }
        root.addView(hint, FrameLayout.LayoutParams(-2, -2, Gravity.CENTER))
        lockOnly = Ui.icon(this, R.drawable.ic_lock, 26, Ui.ACCENT).apply {
            visibility = View.GONE
            setOnClickListener { setLocked(false) }
        }
        root.addView(lockOnly, FrameLayout.LayoutParams(dp(48), dp(48), Gravity.START or Gravity.CENTER_VERTICAL)
            .apply { leftMargin = dp(24) })
        setupGestures(root)
        setContentView(root)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putInt("index", index)
    }

    // ---------- элементы управления ----------
    private fun buildControls(): FrameLayout {
        val c = FrameLayout(this).apply { setBackgroundColor(0x66000000) }

        val top = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), dp(10), dp(12), dp(10))
            background = Ui.gradient(0xCC000000.toInt(), 0x00000000)
        }
        top.addView(Ui.icon(this, R.drawable.ic_back).apply { setOnClickListener { finish() } })
        val names = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(8), 0, dp(8), 0)
        }
        names.addView(Ui.text(this, anime, 17f, Ui.TEXT, true).apply {
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
        })
        subtitle = Ui.text(this, "", 13f, Ui.SUB).apply { maxLines = 1 }
        names.addView(subtitle)
        top.addView(names, LinearLayout.LayoutParams(0, -2, 1f))
        top.addView(Ui.icon(this, R.drawable.ic_voice).apply { setOnClickListener { chooseAudio() } })
        top.addView(Ui.icon(this, R.drawable.ic_subtitles).apply { setOnClickListener { chooseSubs() } })
        top.addView(Ui.icon(this, R.drawable.ic_lock).apply { setOnClickListener { setLocked(true) } })
        c.addView(top, FrameLayout.LayoutParams(-1, -2, Gravity.TOP))

        val center = LinearLayout(this).apply { gravity = Gravity.CENTER }
        center.addView(Ui.icon(this, R.drawable.ic_rewind, 34).apply {
            setOnClickListener { seekBy(-10_000) }
        })
        playBtn = Ui.icon(this, R.drawable.ic_pause, 48).apply {
            background = Ui.rounded(this@PlayerActivity, Ui.ACCENT, 40)
            setOnClickListener { togglePlay() }
        }
        center.addView(playBtn, LinearLayout.LayoutParams(dp(76), dp(76)).apply {
            setMargins(dp(36), 0, dp(36), 0)
        })
        center.addView(Ui.icon(this, R.drawable.ic_forward, 34).apply {
            setOnClickListener { seekBy(10_000) }
        })
        c.addView(center, FrameLayout.LayoutParams(-2, -2, Gravity.CENTER))

        val bottom = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(24), dp(16), dp(12))
            background = Ui.gradient(0x00000000, 0xDD000000.toInt())
        }
        val buttons = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        buttons.addView(Ui.pill(this, "Пропустить опенинг", 0x99FF4F8B.toInt()).apply {
            setOnClickListener { seekBy(85_000) }
        })
        buttons.addView(View(this), LinearLayout.LayoutParams(0, 1, 1f))
        speedBtn = Ui.pill(this, "1.0×", 0x55FFFFFF).apply { setOnClickListener { chooseSpeed() } }
        buttons.addView(speedBtn)
        nextBtn = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            background = Ui.rounded(this@PlayerActivity, 0x55FFFFFF, 16)
            setPadding(dp(10), 0, dp(14), 0)
            addView(ImageView(this@PlayerActivity).apply {
                setImageResource(R.drawable.ic_next)
            }, LinearLayout.LayoutParams(dp(20), dp(20)))
            addView(Ui.text(this@PlayerActivity, "Следующая", 13f, Ui.TEXT, true).apply {
                setPadding(dp(4), dp(7), 0, dp(7))
            })
            setOnClickListener { playIndex(index + 1) }
        }
        buttons.addView(nextBtn, LinearLayout.LayoutParams(-2, -2).apply { leftMargin = dp(8) })
        bottom.addView(buttons)

        val line = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(6), 0, 0)
        }
        timeNow = Ui.text(this, "0:00", 13f)
        timeAll = Ui.text(this, "0:00", 13f)
        seek = SeekBar(this).apply {
            max = 1000
            progressTintList = android.content.res.ColorStateList.valueOf(Ui.ACCENT)
            thumbTintList = android.content.res.ColorStateList.valueOf(Ui.ACCENT)
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: SeekBar, p: Int, fromUser: Boolean) {
                    val dur = player?.duration ?: 0
                    if (fromUser && dur > 0) timeNow.text = Ui.fmt(dur * p / 1000)
                }

                override fun onStartTrackingTouch(sb: SeekBar) {
                    dragging = true
                    handler.removeCallbacks(autoHide)
                }

                override fun onStopTrackingTouch(sb: SeekBar) {
                    dragging = false
                    val p = player ?: return
                    if (p.duration > 0) p.seekTo(p.duration * sb.progress / 1000)
                    scheduleHide()
                }
            })
        }
        line.addView(timeNow)
        line.addView(seek, LinearLayout.LayoutParams(0, -2, 1f))
        line.addView(timeAll)
        bottom.addView(line)
        c.addView(bottom, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
        return c
    }

    // ---------- жесты ----------
    private fun setupGestures(root: View) {
        val audio = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        var mode = 0 // 0 — нет, 1 — яркость, 2 — громкость
        var startValue = 0f
        val detector = GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent): Boolean {
                mode = 0
                return true
            }

            override fun onSingleTapConfirmed(e: MotionEvent): Boolean {
                if (locked) flashLock() else showControls(controls.visibility != View.VISIBLE)
                return true
            }

            override fun onDoubleTap(e: MotionEvent): Boolean {
                if (locked) return true
                if (e.x < root.width / 2f) {
                    seekBy(-10_000)
                    showHint("« −10 с")
                } else {
                    seekBy(10_000)
                    showHint("+10 с »")
                }
                return true
            }

            override fun onScroll(e1: MotionEvent?, e2: MotionEvent, dx: Float, dy: Float): Boolean {
                if (locked || e1 == null) return false
                val totalY = e1.y - e2.y
                if (mode == 0) {
                    if (abs(totalY) < dp(16) || abs(e2.x - e1.x) > abs(totalY)) return false
                    if (e1.x < root.width / 2f) {
                        mode = 1
                        val b = window.attributes.screenBrightness
                        startValue = if (b < 0) 0.5f else b
                    } else {
                        mode = 2
                        startValue = audio.getStreamVolume(AudioManager.STREAM_MUSIC).toFloat() /
                            audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
                    }
                }
                val value = (startValue + totalY / (root.height * 0.8f)).coerceIn(0.01f, 1f)
                if (mode == 1) {
                    window.attributes = window.attributes.apply { screenBrightness = value }
                    showHint("☀ Яркость ${(value * 100).toInt()}%")
                } else {
                    val max = audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
                    audio.setStreamVolume(AudioManager.STREAM_MUSIC, (value * max).toInt(), 0)
                    showHint("🔊 Громкость ${(value * 100).toInt()}%")
                }
                return true
            }
        })
        root.setOnTouchListener { _, ev -> detector.onTouchEvent(ev) }
    }

    private val hideHint = Runnable { hint.visibility = View.GONE }

    private fun showHint(text: String) {
        hint.text = text
        hint.visibility = View.VISIBLE
        handler.removeCallbacks(hideHint)
        handler.postDelayed(hideHint, 700)
    }

    private val autoHide = Runnable { if (player?.isPlaying == true) showControls(false) }

    private fun scheduleHide() {
        handler.removeCallbacks(autoHide)
        handler.postDelayed(autoHide, 3500)
    }

    private fun showControls(show: Boolean) {
        controls.visibility = if (show) View.VISIBLE else View.GONE
        if (show) scheduleHide()
    }

    private fun setLocked(on: Boolean) {
        locked = on
        showControls(!on)
        if (on) flashLock() else lockOnly.visibility = View.GONE
        if (on) Toast.makeText(this, "Экран заблокирован. Нажми на замок, чтобы разблокировать",
            Toast.LENGTH_SHORT).show()
    }

    private val hideLock = Runnable { lockOnly.visibility = View.GONE }

    private fun flashLock() {
        lockOnly.visibility = View.VISIBLE
        handler.removeCallbacks(hideLock)
        handler.postDelayed(hideLock, 2500)
    }

    // ---------- воспроизведение ----------
    override fun onStart() {
        super.onStart()
        hideSystemBars()
        val a = api ?: return finish()
        val http = DefaultHttpDataSource.Factory()
            .setDefaultRequestProperties(mapOf("X-Key" to a.key))
            .setConnectTimeoutMs(15000)
            .setReadTimeoutMs(30000)
        val p = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(http))
            .setSeekBackIncrementMs(10_000)
            .setSeekForwardIncrementMs(10_000)
            .build()
        p.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                Toast.makeText(this@PlayerActivity,
                    "Не удалось воспроизвести: ${error.errorCodeName}. Проверь связь с " +
                        "компьютером; видео в 10-битном H.264 телефоны часто не тянут.",
                    Toast.LENGTH_LONG).show()
                showControls(true)
            }

            override fun onIsPlayingChanged(isPlaying: Boolean) {
                playBtn.setImageResource(if (isPlaying) R.drawable.ic_pause else R.drawable.ic_play)
                if (isPlaying) scheduleHide() else handler.removeCallbacks(autoHide)
            }

            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED) {
                    saveHistory()
                    if (index + 1 < ids.size) playIndex(index + 1) else finish()
                }
            }
        })
        p.playbackParameters = PlaybackParameters(speed)
        view.player = p
        player = p
        playIndex(index, History.resumeAt(this, id))
        handler.post(ticker)
    }

    override fun onStop() {
        handler.removeCallbacks(ticker)
        saveHistory()
        player?.release()
        player = null
        view.player = null
        super.onStop()
    }

    private fun playIndex(i: Int, startAt: Long = 0) {
        val a = api ?: return
        val p = player ?: return
        if (i !in ids.indices) return
        if (i != index) saveHistory()
        index = i
        subtitle.text = epTitle + if (ids.size > 1) "  ·  серия ${i + 1} из ${ids.size}" else ""
        nextBtn.visibility = if (i + 1 < ids.size) View.VISIBLE else View.GONE
        p.setMediaItem(MediaItem.fromUri(a.videoUrl(id)), startAt)
        p.prepare()
        p.playWhenReady = true
        showControls(true)
    }

    private fun saveHistory() {
        val p = player ?: return
        if (p.duration <= 0 || p.currentPosition < 3000) return
        val pos = if (p.playbackState == Player.STATE_ENDED) p.duration else p.currentPosition
        History.save(this, id, epTitle, anime, pos, p.duration)
    }

    private var lastSave = 0L

    private val ticker = object : Runnable {
        override fun run() {
            val p = player ?: return
            val dur = p.duration
            if (dur > 0 && !dragging) {
                seek.progress = (p.currentPosition * 1000 / dur).toInt()
                seek.secondaryProgress = (p.bufferedPosition * 1000 / dur).toInt()
                timeNow.text = Ui.fmt(p.currentPosition)
                timeAll.text = Ui.fmt(dur)
            }
            if (System.currentTimeMillis() - lastSave > 10_000) {
                lastSave = System.currentTimeMillis()
                saveHistory()
            }
            handler.postDelayed(this, 500)
        }
    }

    private fun togglePlay() {
        val p = player ?: return
        if (p.isPlaying) p.pause() else {
            if (p.playbackState == Player.STATE_ENDED) p.seekTo(0)
            p.play()
        }
        scheduleHide()
    }

    private fun seekBy(ms: Long) {
        val p = player ?: return
        val dur = if (p.duration > 0) p.duration else Long.MAX_VALUE
        p.seekTo((p.currentPosition + ms).coerceIn(0, dur))
        if (controls.visibility == View.VISIBLE) scheduleHide()
    }

    // ---------- диалоги ----------
    private fun chooseSpeed() {
        val speeds = floatArrayOf(0.5f, 0.75f, 1f, 1.25f, 1.5f, 1.75f, 2f)
        val labels = speeds.map { "${it}×" }.toTypedArray()
        AlertDialog.Builder(this).setTitle("Скорость")
            .setSingleChoiceItems(labels, speeds.indexOfFirst { it == speed }) { d, which ->
                speed = speeds[which]
                player?.playbackParameters = PlaybackParameters(speed)
                speedBtn.text = "${speed}×"
                d.dismiss()
            }.show()
    }

    private data class Option(val label: String, val apply: () -> Unit, val selected: Boolean)

    private fun trackOptions(type: Int): List<Option> {
        val p = player ?: return emptyList()
        val out = ArrayList<Option>()
        var n = 0
        for (group in p.currentTracks.groups) {
            if (group.type != type) continue
            for (t in 0 until group.length) {
                if (!group.isTrackSupported(t)) continue
                n++
                val f = group.getTrackFormat(t)
                val label = f.label ?: f.language?.uppercase() ?: "Дорожка $n"
                out.add(Option(label, {
                    p.trackSelectionParameters = p.trackSelectionParameters.buildUpon()
                        .setTrackTypeDisabled(type, false)
                        .setOverrideForType(TrackSelectionOverride(group.mediaTrackGroup, t))
                        .build()
                }, group.isTrackSelected(t)))
            }
        }
        return out
    }

    private fun showOptions(title: String, options: List<Option>, empty: String) {
        if (options.isEmpty()) {
            Toast.makeText(this, empty, Toast.LENGTH_SHORT).show()
            return
        }
        AlertDialog.Builder(this).setTitle(title)
            .setSingleChoiceItems(options.map { it.label }.toTypedArray(),
                options.indexOfFirst { it.selected }) { d, which ->
                options[which].apply()
                d.dismiss()
            }.show()
    }

    private fun chooseAudio() =
        showOptions("Озвучка", trackOptions(C.TRACK_TYPE_AUDIO), "В серии одна звуковая дорожка")

    private fun chooseSubs() {
        val p = player ?: return
        val off = Option("Выключить", {
            p.trackSelectionParameters = p.trackSelectionParameters.buildUpon()
                .setTrackTypeDisabled(C.TRACK_TYPE_TEXT, true).build()
        }, p.trackSelectionParameters.disabledTrackTypes.contains(C.TRACK_TYPE_TEXT))
        val subs = trackOptions(C.TRACK_TYPE_TEXT)
        if (subs.isEmpty()) {
            Toast.makeText(this, "В серии нет субтитров", Toast.LENGTH_SHORT).show()
            return
        }
        showOptions("Субтитры", listOf(off) + subs, "")
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
