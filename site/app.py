"""WBnator — Gradio UI for marketplace product card generation.

ONE-PAGE ARCHITECTURE:
  Tab 1: ⚡ Генерация — combined Brief / Advanced / Pool / Variations via mode picker
  Tab 2: 🎬 Видео + per-card эффекты — animate cards from a previous run
  Tab 3: 📚 История

Run:  python app.py   (opens http://127.0.0.1:7860)
"""

import os
import sys
import json
import zipfile
import gradio as gr

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import formats as fmt
import generator as G  # installs log hygiene on import
import transcribe as T

CFG = G.load_config()


# ─── Choices ─────────────────────────────────────────────────────────────────

PROVIDER_CHOICES = [(label, key) for key, label in G.PROVIDER_LABELS.items()]
VIDEO_PROVIDER_CHOICES = [(label, key) for key, label in G.VIDEO_PROVIDER_LABELS.items()]
FORMAT_CHOICES = [(label, key) for key, label in fmt.list_format_choices()]
STYLE_CHOICES = [(label, key) for key, label in fmt.list_style_choices()]
CATEGORIES = ["Электроника", "Одежда и обувь", "Дом и быт", "Косметика",
              "БАДы и здоровье", "Детям", "Авто и инструмент", "Спорт",
              "Продукты", "Канцелярия", "Прочее"]
VIDEO_EFFECT_CHOICES = [
    ("Авто (Claude подберёт)", "auto"),
    ("🔄 Кручение товара (как WB стенд)", "rotate"),
    ("🔍 Микро-зум (медленный наезд)", "zoom"),
    ("✨ Свечение / световой блик", "glow"),
    ("🪟 Параллакс (фон смещается)", "parallax"),
    ("💨 Частицы (пар / пыль)", "particles"),
]

MODE_CHOICES = [
    ("📝 Один товар — кратко (текст или голос)", "brief"),
    ("🛠 Один товар — подробно (структурно)",     "advanced"),
    ("🎨 Пул товаров — несколько разных",         "pool"),
    ("🌈 Вариации стилей — один товар, N подач",  "variations"),
]

MAX_PERCARD_SLOTS = 16


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _to_zip(files, run_dir):
    if not files or not run_dir:
        return None
    zip_path = os.path.join(run_dir, "all_cards.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                if os.path.isfile(f):
                    zf.write(f, os.path.basename(f))
        return zip_path
    except Exception:
        return None


def _photo_paths(photos):
    out = []
    if not photos:
        return out
    if isinstance(photos, (str, bytes)):
        return [photos]
    for p in photos:
        if isinstance(p, str):
            out.append(p)
        elif hasattr(p, "name"):
            out.append(p.name)
        elif isinstance(p, dict) and "path" in p:
            out.append(p["path"])
    return out


def _features_to_list(text):
    if not text:
        return []
    return [ln.strip().lstrip("-•·*").strip()
            for ln in text.splitlines() if ln.strip()]


def _palette(accent, secondary):
    return {"accent": accent or None, "secondary": secondary or None}


# ─── Voice transcribe handler ────────────────────────────────────────────────

def on_voice_to_text(audio_path):
    """Returns (textbox_update, status_md_update). Used by every voice input on the page."""
    if not audio_path or not os.path.isfile(audio_path):
        return (gr.update(),
                gr.update(visible=True,
                          value="⚠️ Аудио не получено. Проверь, что разрешил микрофон "
                                "(в адресной строке Chrome — иконка 🔒 → Site settings → Microphone → Allow), "
                                "затем нажми ⏺ → говори → ⏹."))
    try:
        size_kb = os.path.getsize(audio_path) // 1024
        text = T.transcribe(audio_path, CFG)
        if not text or not text.strip():
            return (gr.update(),
                    gr.update(visible=True,
                              value=f"⚠️ Распознал пустую строку (файл {size_kb}KB). "
                                    f"Скажи громче / ближе к микрофону."))
        return (gr.update(value=text),
                gr.update(visible=True,
                          value=f"✓ Распознано {len(text)} символов · аудио {size_kb}KB"))
    except Exception as e:
        return (gr.update(),
                gr.update(visible=True, value=f"❌ Распознавание не удалось: {str(e)[:200]}"))


# ─── Unified generate dispatcher ─────────────────────────────────────────────

def on_generate_unified(
    mode,
    # Common inputs
    brief_text, photos, style_refs,
    marketplace_radio, formats_check,
    style, accent, secondary,
    image_provider, video_provider, video_effect,
    write_description, generate_videos,
    # Brief / Pool / Variations slider
    count_brief,
    # Advanced fields
    a_name, a_brand, a_category, a_usp, a_features, a_audience, a_price,
    a_n_main, a_n_feature, a_n_lifestyle, a_n_compare,
    # Variations
    v_n_variations, v_providers,
):
    """Single handler that dispatches to the right backend by mode.
    Always yields 7-tuple: (log, gallery, zip, desc, video_gallery, video_files, info_md)
    """
    palette_override = _palette(accent, secondary)

    photo_list = _photo_paths(photos)
    style_ref_list = _photo_paths(style_refs)

    # ─── BRIEF / POOL / VARIATIONS use the textbox; ADVANCED uses structured fields ──

    if mode == "brief":
        if not (brief_text or "").strip():
            yield ("⚠️ Опиши товар в текстовом поле или голосом.",
                   None, None, "", None, None, gr.update(visible=False))
            return
        formats_selected = ([marketplace_radio] if marketplace_radio else ["wb_main"])
        yield ("Запускаюсь…", None, None, "", None, None,
               gr.update(visible=True, value="🚀 Brief mode · запуск"))
        try:
            for partial in G.generate_pack_brief_streaming(
                brief=brief_text.strip(),
                formats_selected=formats_selected,
                card_count=int(count_brief or 4),
                style_key=style or "premium",
                ref_paths=photo_list,
                style_ref_paths=style_ref_list,
                image_provider=image_provider or "veononstop",
                video_provider=video_provider or "fastgen_keyframes",
                palette_override=palette_override,
                write_description=bool(write_description),
                generate_videos=bool(generate_videos),
                video_effect=video_effect or "rotate",
                cfg=CFG,
            ):
                yield _format_partial(partial)
        except Exception as e:
            yield (f"❌ {e}", None, None, "", None, None,
                   gr.update(visible=True, value=f"❌ {e}"))
        return

    if mode == "advanced":
        features = _features_to_list(a_features)
        counts = {"main": int(a_n_main or 0), "feature": int(a_n_feature or 0),
                  "lifestyle": int(a_n_lifestyle or 0), "comparison": int(a_n_compare or 0)}
        plan = {k: v for k, v in counts.items() if v > 0}
        errs = []
        if not (a_name or "").strip(): errs.append("название")
        if not (a_usp or "").strip():  errs.append("USP")
        if not features:               errs.append("преимущества")
        if not formats_check:          errs.append("формат")
        if not plan:                   errs.append("кол-во карточек")
        if errs:
            yield (f"⚠️ Заполни: {', '.join(errs)}", None, None, "", None, None,
                   gr.update(visible=False))
            return
        product = {
            "name": a_name.strip(), "brand": (a_brand or "").strip(),
            "category": a_category or "Прочее", "usp": a_usp.strip(),
            "features": features,
            "audience": (a_audience or "").strip(),
            "price_range": (a_price or "").strip(),
        }
        yield ("Запускаюсь…", None, None, "", None, None,
               gr.update(visible=True, value="🚀 Advanced mode · запуск"))
        try:
            for partial in G.generate_pack_streaming(
                product=product,
                formats_selected=formats_check,
                card_plan=plan,
                style_key=style or "premium",
                ref_paths=photo_list,
                style_ref_paths=style_ref_list,
                image_provider=image_provider or "veononstop",
                video_provider=video_provider or "fastgen_keyframes",
                palette_override=palette_override,
                write_description=bool(write_description),
                generate_videos=bool(generate_videos),
                video_effect=video_effect or "rotate",
                cfg=CFG,
            ):
                yield _format_partial(partial)
        except Exception as e:
            yield (f"❌ {e}", None, None, "", None, None,
                   gr.update(visible=True, value=f"❌ {e}"))
        return

    if mode == "pool":
        if not (brief_text or "").strip():
            yield ("⚠️ Опиши все товары в одном описании (текстом или голосом).",
                   None, None, "", None, None, gr.update(visible=False))
            return
        if not photo_list:
            yield ("⚠️ Загрузи минимум 2 фото — по одному на каждый товар.",
                   None, None, "", None, None, gr.update(visible=False))
            return
        formats_selected = ([marketplace_radio] if marketplace_radio else ["wb_main"])
        yield ("Запускаюсь…", None, None, "", None, None,
               gr.update(visible=True,
                         value=f"🚀 Pool mode · {len(photo_list)} фото"))
        try:
            for partial in G.generate_pack_pool_streaming(
                brief=brief_text.strip(),
                photo_paths=photo_list,
                formats_selected=formats_selected,
                card_count=int(count_brief or 4),
                style_key=style or "premium",
                style_ref_paths=style_ref_list,
                image_provider=image_provider or "veononstop",
                video_provider=video_provider or "fastgen_keyframes",
                palette_override=palette_override,
                write_description=bool(write_description),
                generate_videos=bool(generate_videos),
                video_effect=video_effect or "rotate",
                cfg=CFG,
            ):
                yield _format_partial(partial)
        except Exception as e:
            yield (f"❌ {e}", None, None, "", None, None,
                   gr.update(visible=True, value=f"❌ {e}"))
        return

    if mode == "variations":
        if not (brief_text or "").strip():
            yield ("⚠️ Опиши товар (текстом или голосом).",
                   None, None, "", None, None, gr.update(visible=False))
            return
        format_key = marketplace_radio or "wb_main"
        providers = v_providers or None
        try:
            res = G.generate_style_variations(
                brief_or_product=brief_text.strip(),
                n_variations=int(v_n_variations or 6),
                format_key=format_key,
                providers=providers,
                ref_paths=photo_list,
                style_ref_paths=style_ref_list,
                palette_override=palette_override,
                progress_cb=None, cfg=CFG,
            )
        except Exception as e:
            yield (f"❌ {e}", None, None, "", None, None,
                   gr.update(visible=True, value=f"❌ {e}"))
            return
        files = res.get("files") or []
        log_text = "\n".join(res.get("log") or [])
        run_dir = res.get("run_dir", "")
        zip_path = _to_zip(files, run_dir)
        info = f"✅ {len(files)} вариантов · 📁 `{run_dir}`"
        yield (log_text, files or None, zip_path, "", None, None,
               gr.update(visible=True, value=info))
        return

    yield (f"⚠️ Неизвестный режим: {mode}", None, None, "", None, None,
           gr.update(visible=False))


def _format_partial(partial):
    """Convert a streaming-partial dict into the 7-tuple yield shape."""
    files = partial.get("files") or []
    videos = partial.get("videos") or []
    desc = partial.get("description") or ""
    log_text = "\n".join(partial.get("log") or [])
    info_msg = partial.get("info") or ""
    run_dir = partial.get("run_dir", "")
    inferred = partial.get("inferred_product") or {}
    done = partial.get("done", False)

    zip_path = _to_zip(files, run_dir) if (done and files and run_dir) else None

    head_lines = []
    if inferred.get("name"):
        head_lines.append(f"📦 **{inferred['name']}** · {inferred.get('category','—')}")
    if run_dir:
        head_lines.append(f"📁 `{run_dir}`")
    if info_msg:
        head_lines.append(info_msg)
    head = "\n".join(head_lines) if head_lines else "…"

    return (log_text, files or None, zip_path, desc,
            videos or None, videos or None,
            gr.update(visible=True, value=head))


# ─── Mode-toggle visibility ──────────────────────────────────────────────────

def toggle_mode_visibility(mode):
    """8 updates in declaration order:
       brief_input_grp, advanced_input_grp,
       single_format_grp, multi_format_grp,
       count_brief_grp, count_advanced_grp, count_variations_grp,
       pool_hint
    """
    is_brief = mode == "brief"
    is_advanced = mode == "advanced"
    is_pool = mode == "pool"
    is_var = mode == "variations"
    return [
        gr.update(visible=(is_brief or is_pool or is_var)),  # brief textarea+voice
        gr.update(visible=is_advanced),                       # advanced fields
        gr.update(visible=(is_brief or is_pool or is_var)),  # single format radio
        gr.update(visible=is_advanced),                       # multi format checkbox
        gr.update(visible=(is_brief or is_pool)),            # slider 1-8
        gr.update(visible=is_advanced),                       # 4 number inputs
        gr.update(visible=is_var),                            # variations group
        gr.update(visible=is_pool),                           # pool hint
    ]


# ─── History tab handler ─────────────────────────────────────────────────────

def list_history():
    out = []
    if not os.path.isdir(CFG["output_dir"]):
        return []
    for entry in sorted(os.listdir(CFG["output_dir"]), reverse=True)[:30]:
        path = os.path.join(CFG["output_dir"], entry)
        if not os.path.isdir(path):
            continue
        meta_path = os.path.join(path, "_meta.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    m = json.load(f)
                p = m.get("inferred_product") or m.get("product") or {}
                name = p.get("name") if isinstance(p, dict) else None
                out.append([
                    m.get("created_at", "")[:19].replace("T", " "),
                    m.get("mode", "?"),
                    name or entry,
                    m.get("image_provider") or m.get("provider", "?"),
                    len(m.get("files", []) or []),
                    path,
                ])
            except Exception:
                out.append(["", "?", entry, "?", "?", path])
        else:
            out.append(["", "?", entry, "?", "?", path])
    return out


# ─── Per-card videos handlers ────────────────────────────────────────────────

def on_load_run_for_videos(run_dir):
    files = G.list_run_files(run_dir or "")
    n = len(files)
    updates = []
    for i in range(MAX_PERCARD_SLOTS):
        if i < n:
            path, name = files[i]
            updates.append(gr.update(visible=True))
            updates.append(gr.update(value=path, label=f"{i+1}. {name}"))
            updates.append(gr.update(visible=True))
        else:
            updates.append(gr.update(visible=False))
            updates.append(gr.update(value=None, label=""))
            updates.append(gr.update(visible=False))
    if n == 0:
        status = "⚠️ Нет .jpg/.png в этой папке. Проверь путь из «Истории»."
    else:
        status = f"✓ Загружено {n} карточек. Выбери эффект для каждой и жми «Сделать видео»."
    return [status] + updates


def on_make_per_card_videos(run_dir, video_provider, default_effect, *slot_args):
    log_buffer = []
    if not run_dir or not os.path.isdir(run_dir):
        return "⚠️ Укажи существующую папку.", None
    effects_per_file = {}
    for i in range(MAX_PERCARD_SLOTS):
        path = slot_args[i * 2]
        eff  = slot_args[i * 2 + 1]
        if path and os.path.isfile(path):
            effects_per_file[path] = eff or default_effect or "rotate"
    try:
        res = G.generate_videos_for_run(
            run_dir=run_dir,
            video_provider=video_provider or "fastgen_keyframes",
            video_effect=default_effect or "rotate",
            effects_per_file=effects_per_file,
            progress_cb=log_buffer.append, cfg=CFG,
        )
    except Exception as e:
        log_buffer.append(f"❌ {e}")
        return "\n".join(log_buffer), None
    return ("\n".join(res.get("log", log_buffer)),
            res.get("videos") or None)


# ─── WB Конкурент tab handlers ───────────────────────────────────────────────

def on_comp_detect(photos, brief):
    import wb_analyzer as A
    photo_paths = _photo_paths(photos)
    if not photo_paths:
        yield "⚠️ Загрузи хотя бы одно фото товара.", "", "", ""
        return
    yield "⏳ Анализирую фото через Claude Vision…", "", "", ""
    try:
        result = A.detect_category_from_photos(photo_paths, brief or "", CFG)
    except Exception as e:
        yield f"❌ {e}", "", "", ""
        return
    if result.get("error"):
        yield f"❌ {result['error']}", "", "", ""
        return
    conf = result.get('confidence', '—')
    desc = result.get('product_description', '')[:150]
    status = f"✅ Готово · уверенность: **{conf}** · {desc}"
    yield (
        status,
        result.get("search_query_specific", ""),
        result.get("search_query_broad", ""),
        result.get("category_wb", ""),
    )


def on_comp_search(q_specific, q_broad, limit):
    import wb_analyzer as A
    if not (q_specific or "").strip():
        yield "⚠️ Специфичный запрос не заполнен.", "", None
        return
    yield "⏳ Ищу конкурентов на Wildberries…", "", None
    try:
        result = A.smart_search_competitors(
            q_specific.strip(),
            (q_broad or q_specific.split()[0]).strip(),
            limit=int(limit or 5),
        )
    except Exception as e:
        yield f"❌ {e}", "", None
        return
    if result.get("error") and not result.get("products"):
        yield f"❌ {result['error']}", "", None
        return
    products = result.get("products") or []
    fallback_note = "  *(fallback)*" if result.get("fallback_used") else ""
    lines = [
        f"**Запрос:** «{result.get('query_used','')}»{fallback_note}",
        f"**Найдено:** {len(products)}  (raw: {result.get('raw_count',0)}, "
        f"avg отз.: {result.get('avg_feedbacks',0):.1f})",
        "",
    ]
    for i, p in enumerate(products, 1):
        price = f"{p.price:.0f} ₽" if p.price else "—"
        lines.append(f"{i}. **{p.name[:60]}** — {price}  ★{p.rating or '—'}  {p.feedbacks or 0} отз.")
    yield "## ✅ Конкуренты найдены — переходи к шагу 3", "\n".join(lines), products


def on_comp_analyze(products_state, card_count, no_photos):
    """Analyze competitors. Yields (status_md, analysis_md, analysis_state, gallery_items)."""
    import wb_analyzer as A
    if not products_state:
        yield "⚠️ Сначала найди конкурентов (шаг 2).", "", None, []
        return
    yield "⏳ Шаг 3/3: скачиваю фото конкурентов с WB…", "", None, []

    # Save competitor photos under tmp dir so the UI can render them.
    import time, os
    photos_dir = os.path.join(
        CFG.get("tmp_dir", "/tmp"),
        f"competitors_{int(time.time())}",
    )
    os.makedirs(photos_dir, exist_ok=True)

    try:
        analysis = A.analyze_competitors(
            products_state, CFG,
            card_count=int(card_count or 6),
            include_photos=not bool(no_photos),
            save_photos_dir=photos_dir if not bool(no_photos) else None,
        )
    except Exception as e:
        yield f"❌ {e}", "", None, []
        return
    if analysis.get("error"):
        yield f"❌ Ошибка анализа: {analysis['error']}", "", None, []
        return

    display = A.format_analysis_for_display(analysis)

    # Build gallery items: each is (path, caption)
    gallery_items = []
    for i, c in enumerate((analysis.get("competitors_summary") or []), 1):
        path = c.get("photo_path") or ""
        if not path or not os.path.exists(path):
            continue
        price = f"{c['price']:.0f} ₽" if c.get("price") else "—"
        rating = f"★{c['rating']}" if c.get("rating") else ""
        fb = f"{c.get('feedbacks') or 0} отз."
        caption = f"#{i} · {(c.get('name') or '')[:50]} · {price} {rating} · {fb}"
        gallery_items.append((path, caption))

    yield ("## ✅ Анализ готов — прокрути вниз для редактирования текстов или сразу сгенерируй пак",
           display, analysis, gallery_items)


def _cards_to_table(cards):
    """Convert design.cards → 2D list for gr.Dataframe editor.

    Columns: [#, type, headline_ru, sub_ru, badges_ru (comma), footer_ru]
    """
    rows = []
    for i, c in enumerate(cards or [], 1):
        rows.append([
            i,
            (c.get("type") or "—"),
            (c.get("headline_ru") or "").strip(),
            (c.get("sub_ru") or "").strip(),
            ", ".join((c.get("badges_ru") or [])),
            (c.get("footer_ru") or "").strip(),
        ])
    return rows


def _table_to_cards(rows, original_cards):
    """Merge edited table rows back into original cards (preserves non-text fields).

    rows can be a list of lists OR a pandas DataFrame (Gradio returns DataFrame
    when type='pandas'). We accept whatever Gradio gives.
    """
    if hasattr(rows, "values"):  # pandas DataFrame
        rows = rows.values.tolist()
    if not rows:
        return original_cards or []
    out = []
    for i, c in enumerate(original_cards or []):
        cc = dict(c)
        if i < len(rows):
            r = rows[i]
            # r = [#, type, headline_ru, sub_ru, badges_ru, footer_ru]
            try:
                cc["headline_ru"] = (str(r[2] or "")).strip()
                cc["sub_ru"]      = (str(r[3] or "")).strip()
                badges_raw = (str(r[4] or "")).strip()
                cc["badges_ru"]   = [b.strip() for b in badges_raw.split(",") if b.strip()]
                cc["footer_ru"]   = (str(r[5] or "")).strip()
            except Exception:
                pass
        out.append(cc)
    return out


def _uncertainties_to_md(unc):
    """Format uncertainties list as Markdown for display."""
    if not unc:
        return ""
    lines = ["### ⚠️ Claude не уверен в этих фактах — проверь:\n"]
    for u in unc:
        field = u.get("field", "?")
        q = u.get("question", "")
        guess = u.get("guess", "")
        affects = u.get("affects_cards") or []
        aff_str = (" · карточки: " + ", ".join(affects)) if affects else ""
        lines.append(f"- **{field}** — {q}  _(сейчас написано: «{guess}»)_{aff_str}")
    lines.append("\n> Если предположения неверны — отредактируй тексты карточек ниже и нажми «Сгенерировать пак».")
    return "\n".join(lines)


def on_comp_prepare_design(
    comp_photos, comp_style_refs, comp_brief, category_wb,
    analysis_state, card_count,
    comp_format, comp_style_val, comp_accent, comp_secondary,
    comp_img_prov, comp_vid_prov, comp_vid_eff,
    comp_write_desc, comp_gen_vid,
):
    """Phase 1 of the two-phase flow: ask Claude to design the pack (no rendering).
    Returns the prepared dict via gr.State so the user can edit texts before render.

    Outputs: (status_md, prepared_state, cards_table, uncertainties_md, edit_panel_visible)
    """
    if not analysis_state:
        yield ("⚠️ Сначала выполни анализ конкурентов (шаг 3).",
               None, [], "", gr.update(visible=False))
        return
    photo_paths = _photo_paths(comp_photos)
    style_ref_paths = _photo_paths(comp_style_refs)
    yield ("⏳ Claude проектирует пак (тексты, сцены, неопределённости)…",
           None, [], "", gr.update(visible=False))
    try:
        prepared = G.prepare_competitor_design(
            ref_paths=photo_paths,
            brief=comp_brief or "",
            competitor_analysis=analysis_state,
            formats_selected=[comp_format or "wb_main"],
            card_count=int(card_count or 6),
            style_key=comp_style_val or "premium",
            category_confirmed=category_wb or "",
            image_provider=comp_img_prov or "veononstop",
            video_provider=comp_vid_prov or "fastgen_keyframes",
            palette_override=_palette(comp_accent, comp_secondary),
            style_ref_paths=style_ref_paths,
            write_description=bool(comp_write_desc),
            generate_videos=bool(comp_gen_vid),
            video_effect=comp_vid_eff or "auto",
            cfg=CFG,
        )
    except Exception as e:
        yield (f"❌ Дизайн упал: {e}", None, [], "", gr.update(visible=False))
        return

    design = prepared.get("design") or {}
    cards = design.get("cards") or []
    table = _cards_to_table(cards)
    unc_md = _uncertainties_to_md(design.get("uncertainties") or [])
    concept = design.get("concept", "—")
    status = (f"## ✅ Дизайн готов — отредактируй тексты ниже и нажми «Сгенерировать пак»\n\n"
              f"**Концепт:** {concept}\n\n"
              f"**Карточек:** {len(cards)}")

    yield (status, prepared, table, unc_md, gr.update(visible=True))


def on_comp_render_edited(
    prepared_state, edited_table, comp_gen_vid,
):
    """Phase 2 of the two-phase flow: take the edited design and render images.
    Outputs match on_comp_generate's output spec.
    """
    if not prepared_state:
        yield ("⚠️ Сначала подготовь дизайн.",
               None, None, "", None, None, gr.update(visible=False))
        return

    prepared = dict(prepared_state)
    design = dict(prepared.get("design") or {})
    original_cards = design.get("cards") or []
    edited_cards = _table_to_cards(edited_table, original_cards)
    design["cards"] = edited_cards
    prepared["design"] = design
    # Honor a fresh choice for video generation (user may have toggled it after design)
    prepared["generate_videos"] = bool(comp_gen_vid)

    yield ("Запускаюсь…", None, None, "", None, None,
           gr.update(visible=True, value="🚀 Рендер с отредактированными текстами"))
    try:
        for partial in G.render_pack_from_design(prepared, cfg=CFG):
            yield _format_partial(partial)
    except Exception as e:
        yield (f"❌ {e}", None, None, "", None, None,
               gr.update(visible=True, value=f"❌ {e}"))


def on_comp_generate(
    comp_photos, comp_style_refs, comp_brief, category_wb,
    analysis_state, card_count,
    comp_format, comp_style_val, comp_accent, comp_secondary,
    comp_img_prov, comp_vid_prov, comp_vid_eff,
    comp_write_desc, comp_gen_vid,
):
    if not analysis_state:
        yield ("⚠️ Сначала выполни анализ конкурентов (шаг 3).",
               None, None, "", None, None, gr.update(visible=False))
        return
    photo_paths = _photo_paths(comp_photos)
    style_ref_paths = _photo_paths(comp_style_refs)
    yield ("Запускаюсь…", None, None, "", None, None,
           gr.update(visible=True, value="🚀 Competitor mode · запуск"))
    try:
        for partial in G.generate_pack_competitor_streaming(
            ref_paths=photo_paths,
            brief=comp_brief or "",
            competitor_analysis=analysis_state,
            formats_selected=[comp_format or "wb_main"],
            card_count=int(card_count or 6),
            style_key=comp_style_val or "premium",
            category_confirmed=category_wb or "",
            image_provider=comp_img_prov or "veononstop",
            video_provider=comp_vid_prov or "fastgen_keyframes",
            palette_override=_palette(comp_accent, comp_secondary),
            style_ref_paths=style_ref_paths,
            write_description=bool(comp_write_desc),
            generate_videos=bool(comp_gen_vid),
            video_effect=comp_vid_eff or "auto",
            cfg=CFG,
        ):
            yield _format_partial(partial)
    except Exception as e:
        yield (f"❌ {e}", None, None, "", None, None,
               gr.update(visible=True, value=f"❌ {e}"))


# ─── UI ──────────────────────────────────────────────────────────────────────

CSS = """
#wb-header {padding:14px 20px; border-radius:14px;
  background:linear-gradient(135deg,#1a1a26 0%,#2A1230 60%,#401A2E 100%);
  border:1px solid #23232E; margin-bottom:12px; color:#EFEFF6;}
#wb-header h1 {margin:0; font-size:24px; font-weight:900; letter-spacing:-0.01em;}
#wb-header p {margin:4px 0 0; opacity:0.7; font-size:13px;}
.wb-pill {display:inline-block; padding:3px 10px; border-radius:999px;
  background:rgba(255,77,109,0.15); color:#FF6B85; font-size:11px;
  font-weight:700; letter-spacing:0.06em;}
#wb-log textarea {font-family:'Consolas','Cascadia Mono',monospace !important; font-size:12px !important;}
.mode-card {padding:8px; border:1px solid #2A2A36; border-radius:10px; margin-bottom:8px;}
"""


def build_ui():
    with gr.Blocks(title="WBnator — генератор карточек товара") as demo:
        gr.HTML("""<div id="wb-header">
            <span class="wb-pill">WBnator · v0.4</span>
            <h1>Генератор карточек товара</h1>
            <p>Один товар (текст/голос) · подробно · пул разных товаров · вариации стилей · per-card видео</p>
        </div>""")

        with gr.Tabs():

            # ═══════════════════════════════════════════════════════════════
            # TAB 1: ⚡ Генерация (consolidated)
            # ═══════════════════════════════════════════════════════════════
            with gr.TabItem("⚡ Генерация"):
                with gr.Row():
                    # ─────── LEFT — INPUT ───────
                    with gr.Column(scale=5):
                        gr.Markdown("### 🎯 Режим работы")
                        mode = gr.Radio(
                            choices=MODE_CHOICES, value="brief",
                            label=None, show_label=False, container=False,
                        )

                        pool_hint = gr.Markdown(
                            "ℹ️ В режиме «Пул товаров» загрузи несколько разных товаров (фото) "
                            "и опиши их одним описанием — система сама определит, "
                            "какое фото к какому товару, и сделает отдельный пак для каждого.",
                            visible=False,
                        )

                        # ─── INPUT: brief textarea + voice (brief/pool/variations) ───
                        with gr.Group(visible=True) as brief_input_grp:
                            brief_text = gr.Textbox(
                                label="📝 Описание товара (или товаров для Пул-режима)",
                                placeholder=(
                                    "Пример: продаю термокружку 500мл из стали 304, держит тепло 12ч, "
                                    "не протекает в сумке, гарантия год. Цена 990–1490 ₽."
                                ),
                                lines=7,
                            )
                            brief_voice = gr.Audio(
                                sources=["microphone", "upload"], type="filepath",
                                label=("🎤 Или надиктуй / загрузи mp3-wav. После остановки текст "
                                       "появится в поле выше"),
                            )
                            brief_voice_status = gr.Markdown(visible=False)

                        # ─── INPUT: advanced structured fields ───
                        with gr.Group(visible=False) as advanced_input_grp:
                            with gr.Row():
                                a_name = gr.Textbox(label="Название *",
                                                    placeholder="Термокружка 500мл «Northwind»")
                                a_brand = gr.Textbox(label="Бренд", placeholder="Northwind")
                            with gr.Row():
                                a_category = gr.Dropdown(choices=CATEGORIES, value="Прочее",
                                                         label="Категория", filterable=False)
                                a_price = gr.Textbox(label="Цена", placeholder="990–1490 ₽")
                            a_usp = gr.Textbox(label="USP — главная фишка *",
                                               placeholder="Держит тепло 12 часов")
                            a_features = gr.Textbox(
                                label="Преимущества (по одному на строку) *", lines=5,
                                placeholder="Сталь 304\nГерметичная крышка\nГарантия 1 год",
                            )
                            a_audience = gr.Textbox(label="Аудитория",
                                                    placeholder="Студенты, офис, путешественники")

                        # ─── PHOTOS (universal) ───
                        gr.Markdown("### 📷 Фото")
                        photos = gr.File(
                            label=("Реф-фото товара(-ов) — для Pool-режима загрузи разные "
                                   "товары; для других режимов до 5 фото одного товара"),
                            file_count="multiple", file_types=["image"],
                        )
                        style_refs = gr.File(
                            label="🎨 Реф-карточки стиля (опц., будет приоритетом стиля)",
                            file_count="multiple", file_types=["image"],
                        )

                        # ─── FORMATS ───
                        gr.Markdown("### 📐 Форматы")
                        with gr.Group(visible=True) as single_format_grp:
                            marketplace_radio = gr.Radio(
                                choices=FORMAT_CHOICES, value="wb_main",
                                label="Формат маркетплейса (один)",
                            )
                        with gr.Group(visible=False) as multi_format_grp:
                            formats_check = gr.CheckboxGroup(
                                choices=FORMAT_CHOICES, value=["wb_main"],
                                label="Форматы (можно несколько — для Подробно)",
                            )

                        # ─── COUNTS ───
                        gr.Markdown("### 🔢 Сколько карточек")
                        with gr.Group(visible=True) as count_brief_grp:
                            count_brief = gr.Slider(label="Количество карточек на товар",
                                                    minimum=1, maximum=8, value=4, step=1)
                        with gr.Group(visible=False) as count_advanced_grp:
                            with gr.Row():
                                a_n_main = gr.Number(value=1, label="Главных",
                                                     minimum=0, maximum=3, step=1, precision=0)
                                a_n_feature = gr.Number(value=3, label="Инфографик",
                                                        minimum=0, maximum=8, step=1, precision=0)
                                a_n_lifestyle = gr.Number(value=0, label="Лайфстайл",
                                                          minimum=0, maximum=4, step=1, precision=0)
                                a_n_compare = gr.Number(value=0, label="Сравнение",
                                                        minimum=0, maximum=2, step=1, precision=0)
                        with gr.Group(visible=False) as count_variations_grp:
                            v_n_variations = gr.Slider(label="Сколько разных вариаций",
                                                       minimum=2, maximum=12, value=6, step=1)
                            v_providers = gr.CheckboxGroup(
                                choices=PROVIDER_CHOICES,
                                value=["veononstop", "fastgen_nanopro", "fastgen_openai"],
                                label="По каким нейронкам циклически",
                            )

                        # ─── STYLE / PALETTE / PROVIDERS / VIDEO (common) ───
                        gr.Markdown("### 🎨 Стиль")
                        with gr.Row():
                            style = gr.Dropdown(choices=STYLE_CHOICES, value="premium",
                                                label="Стиль (если нет реф-карточки)",
                                                filterable=False)
                        with gr.Row():
                            accent = gr.ColorPicker(label="Акцент", value="#FF4D6D")
                            secondary = gr.ColorPicker(label="Фон", value="#1B1B2F")

                        gr.Markdown("### 🤖 Нейронки")
                        with gr.Row():
                            image_provider = gr.Dropdown(
                                choices=PROVIDER_CHOICES, value="veononstop",
                                label="Картинки", filterable=False,
                            )
                            video_provider = gr.Dropdown(
                                choices=VIDEO_PROVIDER_CHOICES, value="fastgen_keyframes",
                                label="Видео", filterable=False,
                            )

                        gr.Markdown("### 🎬 Дополнительно")
                        with gr.Row():
                            write_desc = gr.Checkbox(value=True,
                                                     label="📝 Описание для маркетплейса")
                            gen_videos = gr.Checkbox(value=False,
                                                     label="🎬 Видео параллельно")
                        video_effect = gr.Dropdown(choices=VIDEO_EFFECT_CHOICES,
                                                    value="rotate",
                                                    label="🎥 Эффект видео",
                                                    filterable=False)

                        run_btn = gr.Button("⚡ Сгенерировать пак",
                                            variant="primary", size="lg")

                    # ─────── RIGHT — OUTPUT ───────
                    with gr.Column(scale=5):
                        out_info = gr.Markdown(visible=False)
                        out_gallery = gr.Gallery(
                            label="Готовые карточки", show_label=False,
                            columns=2, rows=4, height=1500,
                            preview=True, allow_preview=True, object_fit="contain",
                        )
                        out_zip = gr.File(label="Скачать .zip", interactive=False)
                        out_desc = gr.Textbox(label="Описание для карточки маркетплейса",
                                              lines=8, max_lines=15)
                        out_video_gallery = gr.Gallery(
                            label="Видео — клик для просмотра",
                            columns=2, rows=4, height=900,
                            preview=True, allow_preview=True, object_fit="contain",
                        )
                        out_videos = gr.File(label="Все видео (.mp4)",
                                             file_count="multiple", interactive=False)
                        out_log = gr.Textbox(label="Лог", lines=10, max_lines=30,
                                             interactive=False, elem_id="wb-log")

                # ─── Wiring ──────────────────────────────────────────────────
                mode.change(
                    fn=toggle_mode_visibility, inputs=[mode],
                    outputs=[brief_input_grp, advanced_input_grp,
                             single_format_grp, multi_format_grp,
                             count_brief_grp, count_advanced_grp, count_variations_grp,
                             pool_hint],
                )

                brief_voice.change(
                    fn=on_voice_to_text, inputs=[brief_voice],
                    outputs=[brief_text, brief_voice_status],
                )

                run_btn.click(
                    fn=on_generate_unified,
                    inputs=[
                        mode,
                        brief_text, photos, style_refs,
                        marketplace_radio, formats_check,
                        style, accent, secondary,
                        image_provider, video_provider, video_effect,
                        write_desc, gen_videos,
                        count_brief,
                        a_name, a_brand, a_category, a_usp, a_features,
                        a_audience, a_price,
                        a_n_main, a_n_feature, a_n_lifestyle, a_n_compare,
                        v_n_variations, v_providers,
                    ],
                    outputs=[out_log, out_gallery, out_zip, out_desc,
                             out_video_gallery, out_videos, out_info],
                )

            # ═══════════════════════════════════════════════════════════════
            # TAB 2: 🎬 Видео + per-card эффекты
            # ═══════════════════════════════════════════════════════════════
            with gr.TabItem("🎬 Видео + per-card эффекты"):
                gr.Markdown(
                    "1️⃣ Скопируй путь к папке из вкладки «История» → 2️⃣ нажми «Загрузить» → "
                    "3️⃣ выбери эффект **для каждой карточки** → 4️⃣ жми «Сделать видео»."
                )
                with gr.Row():
                    vfr_run_dir = gr.Textbox(label="Путь к папке run_dir",
                                             placeholder=os.path.join(CFG["output_dir"], "<slug_runid>"),
                                             scale=4)
                    vfr_load_btn = gr.Button("📂 Загрузить", variant="secondary", scale=1)
                with gr.Row():
                    vfr_provider = gr.Dropdown(choices=VIDEO_PROVIDER_CHOICES,
                                               value="fastgen_keyframes",
                                               label="Нейронка-видео", filterable=False)
                    vfr_default_effect = gr.Dropdown(choices=VIDEO_EFFECT_CHOICES,
                                                     value="rotate",
                                                     label="Дефолт-эффект",
                                                     filterable=False)
                vfr_status = gr.Markdown(value="")

                per_card_slots = []
                for i in range(MAX_PERCARD_SLOTS):
                    with gr.Row(visible=False) as row:
                        with gr.Column(scale=2):
                            img = gr.Image(label="", height=240, interactive=False, show_label=True)
                        with gr.Column(scale=1):
                            eff = gr.Dropdown(choices=VIDEO_EFFECT_CHOICES, value="rotate",
                                              label=f"Эффект для карточки {i+1}",
                                              filterable=False, visible=False)
                    per_card_slots.append((row, img, eff))

                vfr_btn = gr.Button("🎬 Сделать видео по выбранным эффектам",
                                    variant="primary", size="lg")
                vfr_log = gr.Textbox(label="Лог", lines=12, max_lines=30,
                                     interactive=False, elem_id="wb-log")
                vfr_videos = gr.File(label="Готовые .mp4", file_count="multiple",
                                     interactive=False)

                vfr_load_btn.click(
                    fn=on_load_run_for_videos,
                    inputs=[vfr_run_dir],
                    outputs=[vfr_status] + [w for slot in per_card_slots for w in slot],
                )
                vfr_btn.click(
                    fn=on_make_per_card_videos,
                    inputs=[vfr_run_dir, vfr_provider, vfr_default_effect]
                           + [w for slot in per_card_slots for w in (slot[1], slot[2])],
                    outputs=[vfr_log, vfr_videos],
                )

            # ═══════════════════════════════════════════════════════════════
            # TAB 3: 🔍 WB Конкурент
            # ═══════════════════════════════════════════════════════════════
            # TAB 3: 🔍 WB Конкурент
            # ═══════════════════════════════════════════════════════════════
            with gr.TabItem("🔍 WB Конкурент"):
                comp_products_state = gr.State(None)
                comp_analysis_state = gr.State(None)
                comp_prepared_state = gr.State(None)

                with gr.Row():
                    # ─────── LEFT — STEPS (always visible) ───────
                    with gr.Column(scale=5):

                        # ── ШАГ 1 ──────────────────────────────────────────
                        gr.Markdown("### Шаг 1: Определи категорию")
                        comp_photos = gr.File(
                            label="📷 Фото твоего товара",
                            file_count="multiple", file_types=["image"],
                        )
                        comp_brief = gr.Textbox(
                            label="Краткое описание (опц.)",
                            placeholder="Напр.: незамерзающая жидкость -40°C, 4 литра",
                            lines=2,
                        )
                        comp_style_refs = gr.File(
                            label="🎨 Реф-карточка стиля (опц. — задаёт палитру/типографику)",
                            file_count="multiple", file_types=["image"],
                        )
                        comp_detect_btn = gr.Button("🔍 Определить категорию",
                                                    variant="secondary")
                        comp_detect_status = gr.Markdown("")
                        comp_q_specific = gr.Textbox(
                            label="Специфичный запрос (отредактируй если нужно)",
                            interactive=True, placeholder="напр.: незамерзающая жидкость -40 4л",
                        )
                        comp_q_broad = gr.Textbox(
                            label="Широкий fallback-запрос (если < 5 конкурентов)",
                            interactive=True, placeholder="напр.: незамерзающая жидкость",
                        )
                        comp_category_wb = gr.Textbox(
                            label="WB-категория",
                            interactive=True, placeholder="напр.: Автохимия",
                        )

                        gr.Markdown("---")
                        # ── ШАГ 2 ──────────────────────────────────────────
                        gr.Markdown("### Шаг 2: Найди конкурентов")
                        comp_limit = gr.Slider(
                            label="Кол-во конкурентов",
                            minimum=3, maximum=10, value=5, step=1,
                        )
                        comp_search_btn = gr.Button("🔎 Найти конкурентов",
                                                    variant="secondary")
                        comp_search_status = gr.Markdown("")
                        comp_list_md = gr.Markdown("")

                        gr.Markdown("---")
                        # ── ШАГ 3 ──────────────────────────────────────────
                        gr.Markdown("### Шаг 3: Анализ конкурентов")
                        comp_card_count = gr.Slider(
                            label="Кол-во карточек (= кол-во фото конкурента для анализа)",
                            minimum=1, maximum=10, value=6, step=1,
                        )
                        comp_no_photos_chk = gr.Checkbox(
                            label="Только текстовый анализ (не скачивать фото с WB)",
                            value=False,
                        )
                        comp_analyze_btn = gr.Button("📊 Анализировать конкурентов",
                                                     variant="secondary")
                        comp_analyze_status = gr.Markdown("")

                        # Galleria of competitor main cards (visual context)
                        comp_competitors_gallery = gr.Gallery(
                            label="🏆 Конкуренты — главные карточки с WB",
                            columns=5, height=320,
                            preview=True, allow_preview=True,
                            object_fit="contain", show_label=True,
                        )
                        # Detailed analysis text (collapsible)
                        with gr.Accordion("📋 Подробный анализ конкурентов",
                                          open=False):
                            comp_analysis_md = gr.Markdown("")

                        gr.Markdown("---")
                        # ── ШАГ 4 (NEW): подготовка и редактирование текстов
                        gr.Markdown("### Шаг 4: Подготовь тексты")
                        comp_prepare_btn = gr.Button(
                            "📝 Подготовить тексты карточек (без рендера)",
                            variant="secondary",
                        )
                        comp_prepare_status = gr.Markdown("")

                        comp_edit_panel = gr.Group(visible=False)
                        with comp_edit_panel:
                            comp_uncertainties_md = gr.Markdown("")
                            gr.Markdown(
                                "**📝 Тексты карточек** — отредактируй любую "
                                "ячейку прямо в таблице. "
                                "Бейджи — через запятую."
                            )
                            comp_cards_table = gr.Dataframe(
                                headers=["#", "Тип", "Заголовок",
                                         "Подзаголовок", "Бейджи (через запятую)",
                                         "Низ карточки"],
                                datatype=["number", "str", "str", "str", "str", "str"],
                                interactive=True,
                                column_count=(6, "fixed"),
                                wrap=True,
                            )

                        gr.Markdown("---")
                        # ── ШАГ 5: Рендеринг ──────────────────────────────
                        gr.Markdown("### Шаг 5: Сгенерируй пак (рендер картинок)")
                        with gr.Row():
                            comp_format = gr.Dropdown(
                                choices=FORMAT_CHOICES, value="wb_main",
                                label="Формат", filterable=False,
                            )
                            comp_style_dd = gr.Dropdown(
                                choices=STYLE_CHOICES, value="premium",
                                label="Стиль", filterable=False,
                            )
                        with gr.Row():
                            comp_accent = gr.ColorPicker(label="Акцент", value="#FF4D6D")
                            comp_secondary = gr.ColorPicker(label="Фон", value="#1B1B2F")
                        with gr.Row():
                            comp_img_prov = gr.Dropdown(
                                choices=PROVIDER_CHOICES, value="veononstop",
                                label="Картинки", filterable=False,
                            )
                            comp_vid_prov = gr.Dropdown(
                                choices=VIDEO_PROVIDER_CHOICES, value="fastgen_keyframes",
                                label="Видео", filterable=False,
                            )
                        comp_vid_eff = gr.Dropdown(
                            choices=VIDEO_EFFECT_CHOICES, value="auto",
                            label="Эффект видео", filterable=False,
                        )
                        with gr.Row():
                            comp_write_desc = gr.Checkbox(
                                value=True, label="📝 Описание для маркетплейса")
                            comp_gen_vid = gr.Checkbox(
                                value=False, label="🎬 Видео параллельно")
                        comp_gen_btn = gr.Button(
                            "⚡ Сгенерировать пак на основе анализа (без редактирования текстов)",
                            variant="primary", size="lg",
                        )
                        comp_render_edited_btn = gr.Button(
                            "🚀 Сгенерировать пак С отредактированными текстами",
                            variant="primary", size="lg",
                        )

                    # ─────── RIGHT — OUTPUT ───────
                    with gr.Column(scale=5):
                        comp_out_info = gr.Markdown(visible=False)
                        comp_out_gallery = gr.Gallery(
                            label="Готовые карточки", show_label=False,
                            columns=2, rows=4,
                            preview=False, allow_preview=True,
                            object_fit="scale-down",
                        )
                        comp_out_zip = gr.File(label="Скачать .zip", interactive=False)
                        comp_out_desc = gr.Textbox(
                            label="Описание для маркетплейса", lines=8, max_lines=15)
                        comp_out_video_gallery = gr.Gallery(
                            label="Видео", columns=2, rows=4,
                            preview=False, allow_preview=True,
                            object_fit="scale-down",
                        )
                        comp_out_videos = gr.File(
                            label="Видео (.mp4)", file_count="multiple", interactive=False)
                        comp_out_log = gr.Textbox(
                            label="Лог", lines=10, max_lines=30,
                            interactive=False, elem_id="wb-log")

                # ─── Wiring ──────────────────────────────────────────────────
                comp_detect_btn.click(
                    fn=on_comp_detect,
                    inputs=[comp_photos, comp_brief],
                    outputs=[comp_detect_status, comp_q_specific,
                             comp_q_broad, comp_category_wb],
                )
                comp_search_btn.click(
                    fn=on_comp_search,
                    inputs=[comp_q_specific, comp_q_broad, comp_limit],
                    outputs=[comp_search_status, comp_list_md, comp_products_state],
                )
                comp_analyze_btn.click(
                    fn=on_comp_analyze,
                    inputs=[comp_products_state, comp_card_count, comp_no_photos_chk],
                    outputs=[comp_analyze_status, comp_analysis_md,
                             comp_analysis_state, comp_competitors_gallery],
                )
                comp_prepare_btn.click(
                    fn=on_comp_prepare_design,
                    inputs=[
                        comp_photos, comp_style_refs, comp_brief, comp_category_wb,
                        comp_analysis_state, comp_card_count,
                        comp_format, comp_style_dd, comp_accent, comp_secondary,
                        comp_img_prov, comp_vid_prov, comp_vid_eff,
                        comp_write_desc, comp_gen_vid,
                    ],
                    outputs=[
                        comp_prepare_status, comp_prepared_state,
                        comp_cards_table, comp_uncertainties_md, comp_edit_panel,
                    ],
                )
                comp_render_edited_btn.click(
                    fn=on_comp_render_edited,
                    inputs=[comp_prepared_state, comp_cards_table, comp_gen_vid],
                    outputs=[
                        comp_out_log, comp_out_gallery, comp_out_zip,
                        comp_out_desc, comp_out_video_gallery, comp_out_videos,
                        comp_out_info,
                    ],
                )
                comp_gen_btn.click(
                    fn=on_comp_generate,
                    inputs=[
                        comp_photos, comp_style_refs, comp_brief, comp_category_wb,
                        comp_analysis_state, comp_card_count,
                        comp_format, comp_style_dd, comp_accent, comp_secondary,
                        comp_img_prov, comp_vid_prov, comp_vid_eff,
                        comp_write_desc, comp_gen_vid,
                    ],
                    outputs=[
                        comp_out_log, comp_out_gallery, comp_out_zip,
                        comp_out_desc, comp_out_video_gallery, comp_out_videos,
                        comp_out_info,
                    ],
                )

            # ═══════════════════════════════════════════════════════════════
            # TAB 4: 📚 История
            # ═══════════════════════════════════════════════════════════════
            with gr.TabItem("📚 История"):
                gr.Markdown("Последние 30 генераций.")
                hist_table = gr.Dataframe(
                    headers=["Когда", "Режим", "Товар", "Провайдер", "Файлов", "Папка"],
                    column_count=(6, "fixed"),
                    interactive=False, wrap=True,
                )
                refresh_btn = gr.Button("🔄 Обновить")
                refresh_btn.click(fn=list_history, outputs=hist_table)

    return demo


if __name__ == "__main__":
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"
    demo = build_ui()
    demo.queue(default_concurrency_limit=5).launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
        css=CSS,
        theme=gr.themes.Base(),
        allowed_paths=[CFG["output_dir"], CFG["tmp_dir"]],
    )
