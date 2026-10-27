package com.animedub.mobile

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView

/** FrameLayout с постоянным соотношением сторон (ширина / высота). */
class AspectFrame(ctx: Context, private val ratio: Float) : FrameLayout(ctx) {
    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val w = MeasureSpec.getSize(widthMeasureSpec)
        super.onMeasure(MeasureSpec.makeMeasureSpec(w, MeasureSpec.EXACTLY),
            MeasureSpec.makeMeasureSpec((w / ratio).toInt(), MeasureSpec.EXACTLY))
    }
}

/** Цвета и мелкие помощники для интерфейса, который собирается из кода. */
object Ui {
    const val BG = 0xFF0F0E17.toInt()
    const val SURFACE = 0xFF1C1A2B.toInt()
    const val SURFACE2 = 0xFF2A2740.toInt()
    const val ACCENT = 0xFFFF4F8B.toInt()
    const val ACCENT2 = 0xFF7C4DFF.toInt()
    const val TEXT = 0xFFFFFFFF.toInt()
    const val SUB = 0xFFA9A6C2.toInt()

    fun dp(ctx: Context, v: Int) = (v * ctx.resources.displayMetrics.density).toInt()

    fun rounded(ctx: Context, color: Int, radiusDp: Int) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(ctx, radiusDp).toFloat()
    }

    fun gradient(vararg colors: Int, orientation: GradientDrawable.Orientation =
        GradientDrawable.Orientation.TOP_BOTTOM) = GradientDrawable(orientation, colors)

    fun text(ctx: Context, s: String, sizeSp: Float, color: Int = TEXT, bold: Boolean = false) =
        TextView(ctx).apply {
            text = s
            textSize = sizeSp
            setTextColor(color)
            if (bold) typeface = Typeface.DEFAULT_BOLD
        }

    fun icon(ctx: Context, res: Int, sizeDp: Int = 24, tint: Int = TEXT) = ImageView(ctx).apply {
        setImageResource(res)
        setColorFilter(tint)
        val pad = dp(ctx, 8)
        setPadding(pad, pad, pad, pad)
        layoutParams = ViewGroup.LayoutParams(dp(ctx, sizeDp + 16), dp(ctx, sizeDp + 16))
        background = android.graphics.drawable.RippleDrawable(
            android.content.res.ColorStateList.valueOf(0x33FFFFFF), null,
            GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(Color.WHITE) })
    }

    /** Розовая «таблетка» с текстом — кнопки и бейджи. */
    fun pill(ctx: Context, s: String, color: Int = ACCENT) = text(ctx, s, 13f, TEXT, true).apply {
        background = rounded(ctx, color, 16)
        setPadding(dp(ctx, 14), dp(ctx, 7), dp(ctx, 14), dp(ctx, 7))
        gravity = Gravity.CENTER
    }

    /** Полоска прогресса просмотра. */
    fun progressBar(ctx: Context, fraction: Float): View {
        val f = fraction.coerceIn(0f, 1f)
        return LinearLayout(ctx).apply {
            background = rounded(ctx, 0x55FFFFFF, 2)
            addView(View(ctx).apply { background = rounded(ctx, ACCENT, 2) },
                LinearLayout.LayoutParams(0, -1, f))
            addView(View(ctx), LinearLayout.LayoutParams(0, -1, 1f - f))
        }
    }

    /** Пустой экран с талисманом. */
    fun empty(ctx: Context, message: String) = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        gravity = Gravity.CENTER
        setPadding(dp(ctx, 32), dp(ctx, 48), dp(ctx, 32), dp(ctx, 48))
        addView(ImageView(ctx).apply { setImageResource(R.drawable.mascot) },
            LinearLayout.LayoutParams(dp(ctx, 140), dp(ctx, 140)))
        addView(text(ctx, message, 15f, SUB).apply {
            gravity = Gravity.CENTER
            setPadding(0, dp(ctx, 16), 0, 0)
        })
    }

    /**
     * Карточка с кадром из серии: картинка, затемнение снизу и подписи поверх.
     * Возвращает саму карточку; картинка и подписи доступны через теги.
     */
    fun coverCard(ctx: Context, ratio: Float, radiusDp: Int = 14): AspectFrame {
        val card = AspectFrame(ctx, ratio).apply {
            background = rounded(ctx, SURFACE2, radiusDp)
            clipToOutline = true
        }
        card.addView(ImageView(ctx).apply {
            setImageResource(R.drawable.petal)
            scaleType = ImageView.ScaleType.CENTER_CROP
            id = R.id.cover_image
        }, FrameLayout.LayoutParams(-1, -1))
        card.addView(View(ctx).apply {
            background = gradient(0x00000000, 0x22000000, 0xDD0F0E17.toInt())
        }, FrameLayout.LayoutParams(-1, -1))
        val texts = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(ctx, 10), 0, dp(ctx, 10), dp(ctx, 10))
            id = R.id.cover_texts
        }
        card.addView(texts, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
        return card
    }

    fun fmt(ms: Long): String {
        val s = (ms / 1000).coerceAtLeast(0)
        return if (s >= 3600) "%d:%02d:%02d".format(s / 3600, s / 60 % 60, s % 60)
        else "%d:%02d".format(s / 60, s % 60)
    }
}
