package com.animedub.mobile

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.res.Configuration
import android.os.Bundle
import android.text.InputType
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.drawerlayout.widget.DrawerLayout
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import java.util.concurrent.Executors

/** Универсальный адаптер: список элементов + функции создания и заполнения строк. */
class Rows(
    private val items: List<Any>,
    private val type: (Any) -> Int,
    private val create: (ViewGroup, Int) -> View,
    private val bind: (View, Any) -> Unit,
) : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
    override fun getItemCount() = items.size
    override fun getItemViewType(position: Int) = type(items[position])
    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
        object : RecyclerView.ViewHolder(create(parent, viewType)) {}
    override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) =
        bind(holder.itemView, items[position])
}

class MainActivity : Activity() {
    private enum class Screen { ALL, ANIME, HISTORY }

    private val bg = Executors.newSingleThreadExecutor()
    private var api: Api? = null
    private var episodes: List<Episode> = emptyList()
    private var screen = Screen.ALL
    private var opened: String? = null // открытое аниме (папка)

    private lateinit var drawer: DrawerLayout
    private lateinit var title: TextView
    private lateinit var body: FrameLayout
    private lateinit var drawerHost: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Ui.BG
        window.navigationBarColor = Ui.BG
        api = Api.saved(this)
        if (api == null) showConnect() else showMain()
    }

    override fun onResume() {
        super.onResume()
        // после плеера обновляем отметки «остановился на …»
        if (::body.isInitialized && episodes.isNotEmpty()) render()
    }

    override fun onDestroy() {
        bg.shutdownNow()
        super.onDestroy()
    }

    private fun dp(v: Int) = Ui.dp(this, v)

    private fun ui(block: () -> Unit) = runOnUiThread { if (!isDestroyed) block() }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        when {
            ::drawer.isInitialized && drawer.isDrawerOpen(Gravity.END) -> drawer.closeDrawer(Gravity.END)
            ::body.isInitialized && screen != Screen.ALL -> open(Screen.ALL)
            else -> super.onBackPressed()
        }
    }

    // ---------- подключение к компьютеру ----------
    private fun showConnect() {
        val p = Api.prefs(this)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(24), dp(32), dp(24), dp(24))
            background = Ui.gradient(0xFF2A1B4D.toInt(), Ui.BG, Ui.BG)
        }
        root.addView(ImageView(this).apply { setImageResource(R.drawable.mascot) },
            LinearLayout.LayoutParams(dp(150), dp(150)))
        root.addView(Ui.text(this, "AnimeDub", 30f, Ui.TEXT, true).apply {
            gravity = Gravity.CENTER
        })
        root.addView(Ui.text(this, "Включи на компьютере в AnimeDub галочку «Смотреть серии " +
            "в приложении на Android» и введи адрес и ключ, которые там показаны.\n\n" +
            "Дома подойдёт адрес из домашней сети, вне дома — адрес Tailscale.", 14f, Ui.SUB).apply {
            gravity = Gravity.CENTER
            setPadding(0, dp(12), 0, dp(8))
        })
        fun field(hintText: String, value: String?, type: Int) = EditText(this).apply {
            hint = hintText
            inputType = type
            setSingleLine()
            setText(value ?: "")
            setTextColor(Ui.TEXT)
            setHintTextColor(Ui.SUB)
            background = Ui.rounded(this@MainActivity, Ui.SURFACE, 12)
            setPadding(dp(16), dp(14), dp(16), dp(14))
        }
        val host = field("Адрес, например 192.168.1.5:8765", p.getString("host", ""),
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        val key = field("Ключ, например abcd-efgh", p.getString("key", ""),
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS)
        val status = Ui.text(this, "", 14f, Ui.ACCENT).apply { gravity = Gravity.CENTER }
        val btn = Button(this).apply {
            text = "Подключиться"
            setTextColor(Ui.TEXT)
            isAllCaps = false
            textSize = 16f
            background = Ui.rounded(this@MainActivity, Ui.ACCENT, 14)
        }
        btn.setOnClickListener {
            val candidate = Api(host.text.toString(), key.text.toString().trim().lowercase())
            btn.isEnabled = false
            status.text = "Подключение…"
            bg.execute {
                try {
                    candidate.check()
                    ui {
                        p.edit().putString("host", host.text.toString().trim())
                            .putString("key", candidate.key).apply()
                        api = candidate
                        Images.forget()
                        showMain()
                    }
                } catch (e: Exception) {
                    ui {
                        btn.isEnabled = true
                        status.text = e.message ?: e.toString()
                    }
                }
            }
        }
        listOf(host, key, btn, status).forEach {
            root.addView(it, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(14) })
        }
        setContentView(ScrollView(this).apply {
            isFillViewport = true
            setBackgroundColor(Ui.BG)
            addView(root)
        })
    }

    // ---------- главный экран с боковой вкладкой справа ----------
    private fun showMain() {
        drawer = DrawerLayout(this)
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ui.BG)
        }
        val bar = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(16), dp(6), dp(6), dp(6))
        }
        title = Ui.text(this, "", 20f, Ui.TEXT, true).apply {
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
        }
        bar.addView(title, LinearLayout.LayoutParams(0, -2, 1f))
        bar.addView(Ui.icon(this, R.drawable.ic_refresh).apply { setOnClickListener { load() } })
        bar.addView(Ui.icon(this, R.drawable.ic_menu).apply {
            setOnClickListener { drawer.openDrawer(Gravity.END) }
        })
        content.addView(bar)
        body = FrameLayout(this)
        content.addView(body, LinearLayout.LayoutParams(-1, 0, 1f))
        drawer.addView(content, DrawerLayout.LayoutParams(-1, -1))
        drawer.addView(buildDrawer(), DrawerLayout.LayoutParams(dp(290), -1, Gravity.END))
        setContentView(drawer)
        open(Screen.ALL)
        load()
    }

    private fun buildDrawer(): View {
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ui.SURFACE)
            isClickable = true
        }
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(28), dp(20), dp(20))
            background = Ui.gradient(Ui.ACCENT2, 0xFF3A2470.toInt(), Ui.SURFACE)
        }
        head.addView(ImageView(this).apply { setImageResource(R.drawable.mascot) },
            LinearLayout.LayoutParams(dp(84), dp(84)))
        head.addView(Ui.text(this, "AnimeDub", 22f, Ui.TEXT, true).apply {
            setPadding(0, dp(10), 0, 0)
        })
        drawerHost = Ui.text(this, api?.base ?: "", 13f, 0xCCFFFFFF.toInt())
        head.addView(drawerHost)
        val version = try {
            packageManager.getPackageInfo(packageName, 0).versionName
        } catch (e: Exception) {
            null
        }
        if (version != null) head.addView(Ui.text(this, "Версия $version", 12f, 0x99FFFFFF.toInt()))
        panel.addView(head)

        fun item(icon: Int, label: String, action: () -> Unit) = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(16), dp(4), dp(16), dp(4))
            addView(Ui.icon(this@MainActivity, icon, 22, Ui.ACCENT).apply { isClickable = false })
            addView(Ui.text(this@MainActivity, label, 16f).apply { setPadding(dp(12), 0, 0, 0) })
            setOnClickListener {
                drawer.closeDrawer(Gravity.END)
                action()
            }
            background = android.graphics.drawable.RippleDrawable(
                android.content.res.ColorStateList.valueOf(0x33FFFFFF), null, null)
        }
        panel.addView(item(R.drawable.ic_grid, "Все аниме") { open(Screen.ALL) })
        panel.addView(item(R.drawable.ic_history, "История просмотра") { open(Screen.HISTORY) })
        panel.addView(View(this).apply { setBackgroundColor(Ui.SURFACE2) },
            LinearLayout.LayoutParams(-1, dp(1)).apply { setMargins(0, dp(8), 0, dp(8)) })
        panel.addView(item(R.drawable.ic_link, "Сменить компьютер") { showConnect() })
        return panel
    }

    private fun open(s: Screen, anime: String? = null) {
        screen = s
        if (anime != null) opened = anime
        render()
    }

    private fun load() {
        val a = api ?: return
        if (episodes.isEmpty()) setBody(Ui.empty(this, "Загружаю список серий…"))
        bg.execute {
            try {
                val items = a.list()
                ui {
                    episodes = items
                    render()
                }
            } catch (e: Exception) {
                ui {
                    if (episodes.isEmpty()) {
                        setBody(Ui.empty(this, (e.message ?: e.toString()) +
                            "\n\nНажми ⟳ вверху, чтобы попробовать снова."))
                    }
                }
            }
        }
    }

    private fun setBody(v: View) {
        body.removeAllViews()
        body.addView(v, FrameLayout.LayoutParams(-1, -1))
    }

    private fun render() {
        when (screen) {
            Screen.ALL -> renderAll()
            Screen.ANIME -> renderAnime()
            Screen.HISTORY -> renderHistory()
        }
    }

    private fun recycler(adapter: Rows, layout: RecyclerView.LayoutManager) = RecyclerView(this).apply {
        layoutManager = layout
        this.adapter = adapter
        clipToPadding = false
        setPadding(dp(10), dp(4), dp(10), dp(24))
    }

    private fun span() =
        if (resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE) 4 else 2

    // ---------- все аниме ----------
    private fun renderAll() {
        title.text = "Все аниме"
        val a = api ?: return
        val animes = Api.group(episodes)
        if (animes.isEmpty()) {
            setBody(Ui.empty(this, "Готовых серий пока нет. Они появятся здесь после озвучки " +
                "на компьютере."))
            return
        }
        val last = History.all(this).firstOrNull { w -> episodes.any { it.id == w.id } }
        val items = ArrayList<Any>()
        if (last != null) items.add(last)
        items.addAll(animes)
        val spans = span()
        val grid = GridLayoutManager(this, spans)
        grid.spanSizeLookup = object : GridLayoutManager.SpanSizeLookup() {
            override fun getSpanSize(position: Int) = if (items[position] is Watch) spans else 1
        }
        setBody(recycler(Rows(items, { if (it is Watch) 0 else 1 }, { _, type ->
            val card = Ui.coverCard(this, if (type == 0) 16f / 8f else 16f / 11f)
            card.layoutParams = RecyclerView.LayoutParams(-1, -2).apply {
                setMargins(dp(6), dp(6), dp(6), dp(6))
            }
            card
        }) { v, item ->
            val img = v.findViewById<ImageView>(R.id.cover_image)
            val texts = v.findViewById<LinearLayout>(R.id.cover_texts)
            texts.removeAllViews()
            if (item is Watch) {
                Images.load(a, item.id, img)
                texts.addView(Ui.pill(this, "▶ Продолжить просмотр"),
                    LinearLayout.LayoutParams(-2, -2))
                texts.addView(Ui.text(this, item.anime, 18f, Ui.TEXT, true).apply {
                    setPadding(0, dp(8), 0, 0)
                    maxLines = 1
                    ellipsize = TextUtils.TruncateAt.END
                })
                texts.addView(Ui.text(this, "${item.title} · ${Ui.fmt(item.position)} из " +
                    Ui.fmt(item.duration), 13f, Ui.SUB))
                texts.addView(Ui.progressBar(this, item.progress),
                    LinearLayout.LayoutParams(-1, dp(3)).apply { topMargin = dp(6) })
                v.setOnClickListener { playById(item.id) }
            } else {
                val anime = item as Anime
                Images.load(a, anime.cover.id, img)
                texts.addView(Ui.text(this, anime.name, 15f, Ui.TEXT, true).apply {
                    maxLines = 2
                    ellipsize = TextUtils.TruncateAt.END
                })
                texts.addView(Ui.text(this, episodesLabel(anime.episodes.size), 12f, Ui.SUB))
                v.setOnClickListener { open(Screen.ANIME, anime.name) }
            }
        }, grid))
    }

    private fun episodesLabel(n: Int): String {
        val word = when {
            n % 100 in 11..14 -> "серий"
            n % 10 == 1 -> "серия"
            n % 10 in 2..4 -> "серии"
            else -> "серий"
        }
        return "$n $word"
    }

    // ---------- страница аниме ----------
    private fun renderAnime() {
        val a = api ?: return
        val anime = Api.group(episodes).firstOrNull { it.name == opened }
        if (anime == null) {
            open(Screen.ALL)
            return
        }
        title.text = anime.name
        val history = History.all(this).associateBy { it.id }
        val next = anime.episodes.firstOrNull { history[it.id]?.finished != true } ?: anime.episodes.first()
        val items = ArrayList<Any>()
        items.add(anime)
        items.addAll(anime.episodes)
        setBody(recycler(Rows(items, { if (it is Anime) 0 else 1 }, { _, type ->
            if (type == 0) {
                Ui.coverCard(this, 16f / 9f, 18).apply {
                    layoutParams = RecyclerView.LayoutParams(-1, -2).apply {
                        setMargins(dp(6), dp(6), dp(6), dp(12))
                    }
                }
            } else {
                episodeRow()
            }
        }) { v, item ->
            if (item is Anime) {
                Images.load(a, item.cover.id, v.findViewById(R.id.cover_image))
                val texts = v.findViewById<LinearLayout>(R.id.cover_texts)
                texts.removeAllViews()
                texts.addView(Ui.text(this, item.name, 22f, Ui.TEXT, true))
                texts.addView(Ui.text(this, episodesLabel(item.episodes.size) + " · озвучка " +
                    item.episodes.first().lang.uppercase(), 13f, Ui.SUB))
                val w = history[next.id]
                val label = if (w != null && !w.finished) "▶ Продолжить: ${next.title}"
                else "▶ Смотреть: ${next.title}"
                texts.addView(Ui.pill(this, label).apply {
                    setOnClickListener { play(item, next) }
                }, LinearLayout.LayoutParams(-2, -2).apply { topMargin = dp(10) })
            } else {
                val e = item as Episode
                bindEpisode(v, a, e.id, e.title, null, history[e.id])
                v.setOnClickListener { play(anime, e) }
            }
        }, LinearLayoutManager(this)))
    }

    private fun episodeRow(): View = LinearLayout(this).apply {
        gravity = Gravity.CENTER_VERTICAL
        setPadding(dp(6), dp(6), dp(6), dp(6))
        layoutParams = RecyclerView.LayoutParams(-1, -2)
        val thumb = Ui.coverCard(this@MainActivity, 16f / 9f, 10)
        addView(thumb, LinearLayout.LayoutParams(dp(140), -2))
        addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), 0, 0, 0)
            id = View.generateViewId()
            tag = "info"
        }, LinearLayout.LayoutParams(0, -2, 1f))
    }

    private fun bindEpisode(v: View, a: Api, id: String, name: String, sub: String?, w: Watch?) {
        val row = v as LinearLayout
        val thumb = row.getChildAt(0) as ViewGroup
        Images.load(a, id, thumb.findViewById(R.id.cover_image))
        (thumb.findViewById<LinearLayout>(R.id.cover_texts)).removeAllViews()
        val info = row.getChildAt(1) as LinearLayout
        info.removeAllViews()
        if (sub != null) info.addView(Ui.text(this, sub, 12f, Ui.ACCENT, true).apply {
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
        })
        info.addView(Ui.text(this, name, 15f, Ui.TEXT, true).apply {
            maxLines = 2
            ellipsize = TextUtils.TruncateAt.END
        })
        val state = when {
            w == null -> "Не смотрел"
            w.finished -> "Просмотрено ✓"
            else -> "Остановился на ${Ui.fmt(w.position)} из ${Ui.fmt(w.duration)}"
        }
        info.addView(Ui.text(this, state, 12f, Ui.SUB).apply { setPadding(0, dp(2), 0, 0) })
        if (w != null) {
            info.addView(Ui.progressBar(this, if (w.finished) 1f else w.progress),
                LinearLayout.LayoutParams(-1, dp(3)).apply { topMargin = dp(6) })
        }
    }

    // ---------- история ----------
    private fun renderHistory() {
        title.text = "История просмотра"
        val a = api ?: return
        val list = History.all(this)
        if (list.isEmpty()) {
            setBody(Ui.empty(this, "Здесь появятся серии, которые ты начал смотреть."))
            return
        }
        val items = ArrayList<Any>()
        items.add("clear")
        items.addAll(list)
        setBody(recycler(Rows(items, { if (it is Watch) 1 else 0 }, { _, type ->
            if (type == 0) {
                LinearLayout(this).apply {
                    gravity = Gravity.END
                    setPadding(dp(6), dp(4), dp(6), dp(4))
                    layoutParams = RecyclerView.LayoutParams(-1, -2)
                    addView(Ui.pill(this@MainActivity, "Очистить историю", Ui.SURFACE2))
                }
            } else {
                episodeRow()
            }
        }) { v, item ->
            if (item is Watch) {
                bindEpisode(v, a, item.id, item.title, item.anime, item)
                v.setOnClickListener { playById(item.id) }
            } else {
                v.setOnClickListener {
                    AlertDialog.Builder(this).setMessage("Очистить историю просмотра?")
                        .setPositiveButton("Очистить") { _, _ ->
                            History.clear(this)
                            render()
                        }
                        .setNegativeButton("Отмена", null).show()
                }
            }
        }, LinearLayoutManager(this)))
    }

    // ---------- воспроизведение ----------
    private fun playById(id: String) {
        val e = episodes.firstOrNull { it.id == id }
        val anime = e?.let { ep -> Api.group(episodes).firstOrNull { it.name == ep.folder } }
        if (e == null || anime == null) {
            AlertDialog.Builder(this)
                .setMessage("Этой серии сейчас нет на компьютере. Проверь, что папка с ней " +
                    "добавлена в AnimeDub, и нажми ⟳.")
                .setPositiveButton("Понятно", null).show()
            return
        }
        play(anime, e)
    }

    private fun play(anime: Anime, e: Episode) {
        startActivity(Intent(this, PlayerActivity::class.java)
            .putExtra("anime", anime.name)
            .putExtra("ids", anime.episodes.map { it.id }.toTypedArray())
            .putExtra("titles", anime.episodes.map { it.title }.toTypedArray())
            .putExtra("index", anime.episodes.indexOf(e)))
    }
}
