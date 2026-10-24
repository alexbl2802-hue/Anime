package com.animedub.mobile

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.SimpleAdapter
import android.widget.TextView
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private val bg = Executors.newSingleThreadExecutor()
    private var api: Api? = null
    private var episodes: List<Episode> = emptyList()
    private lateinit var status: TextView
    private var list: ListView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        api = Api.saved(this)
        if (api == null) showConnect() else showList()
    }

    override fun onResume() {
        super.onResume()
        // список уже загружен — обновим отметки «остановился на …» после плеера
        if (api != null && list != null && episodes.isNotEmpty()) refresh()
    }

    override fun onDestroy() {
        bg.shutdownNow()
        super.onDestroy()
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun ui(block: () -> Unit) = runOnUiThread { if (!isDestroyed) block() }

    // ---------- подключение к компьютеру ----------
    private fun showConnect() {
        list = null
        invalidateOptionsMenu()
        val p = Api.prefs(this)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(24), dp(20), dp(20))
        }
        root.addView(TextView(this).apply {
            textSize = 15f
            text = "Включи на компьютере в AnimeDub галочку «Смотреть серии в приложении " +
                "на Android» и введи адрес и ключ, которые там показаны.\n\n" +
                "Дома подойдёт адрес из домашней сети, вне дома — адрес Tailscale " +
                "(Tailscale должен быть включён на компьютере и на телефоне)."
        })
        val host = EditText(this).apply {
            hint = "Адрес, например 192.168.1.5:8765"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine()
            setText(p.getString("host", ""))
        }
        val key = EditText(this).apply {
            hint = "Ключ, например abcd-efgh"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setSingleLine()
            setText(p.getString("key", ""))
        }
        status = TextView(this).apply { setPadding(0, dp(12), 0, 0) }
        val btn = Button(this).apply { text = "Подключиться" }
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
                        showList()
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
            root.addView(it, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(12) })
        }
        setContentView(root)
    }

    // ---------- список серий ----------
    private fun showList() {
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        status = TextView(this).apply {
            setPadding(dp(16), dp(10), dp(16), dp(10))
            gravity = Gravity.CENTER_VERTICAL
        }
        val lv = ListView(this)
        lv.setOnItemClickListener { _, _, pos, _ -> play(episodes[pos]) }
        root.addView(status)
        root.addView(lv, LinearLayout.LayoutParams(-1, 0, 1f))
        setContentView(root)
        list = lv
        invalidateOptionsMenu()
        refresh()
    }

    private fun refresh() {
        val a = api ?: return
        status.visibility = View.VISIBLE
        status.text = "Загрузка списка…"
        bg.execute {
            try {
                val items = a.list()
                ui { render(items) }
            } catch (e: Exception) {
                ui { status.text = (e.message ?: e.toString()) + "\n\nМеню ⋮ → «Обновить»." }
            }
        }
    }

    private fun render(items: List<Episode>) {
        episodes = items
        val p = Api.prefs(this)
        val rows = items.map { e ->
            val pos = p.getLong("pos_${e.id}", 0)
            val watched = if (pos > 0) "  ·  остановился на ${fmt(pos)}" else ""
            mapOf(
                "title" to e.title,
                "sub" to "${e.folder}  ·  ${e.lang.uppercase()}  ·  ${e.size / (1 shl 20)} МБ$watched",
            )
        }
        list?.adapter = SimpleAdapter(this, rows, android.R.layout.simple_list_item_2,
            arrayOf("title", "sub"), intArrayOf(android.R.id.text1, android.R.id.text2))
        if (items.isEmpty()) {
            status.text = "Готовых серий пока нет. Они появятся здесь после озвучки на компьютере."
        } else {
            status.visibility = View.GONE
        }
    }

    private fun play(e: Episode) {
        val a = api ?: return
        startActivity(Intent(this, PlayerActivity::class.java)
            .putExtra("url", a.videoUrl(e))
            .putExtra("key", a.key)
            .putExtra("id", e.id)
            .putExtra("title", e.title))
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        if (list != null) {
            menu.add(0, 1, 0, "Обновить")
            menu.add(0, 2, 1, "Сменить компьютер")
        }
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            1 -> refresh()
            2 -> showConnect()
            else -> return super.onOptionsItemSelected(item)
        }
        return true
    }

    companion object {
        fun fmt(ms: Long): String {
            val s = ms / 1000
            return if (s >= 3600) "%d:%02d:%02d".format(s / 3600, s / 60 % 60, s % 60)
            else "%d:%02d".format(s / 60, s % 60)
        }
    }
}
