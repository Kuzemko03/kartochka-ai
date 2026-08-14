"""Final-step image post-processing: resize + center-crop to marketplace dimensions.

NO text overlay — text is rendered by the image API itself (banana / GEM_PIX_2 /
NARWHAL handle Cyrillic and stylized layouts well). This module exists only to
hit the EXACT pixel size that WB / Ozon / Я.Маркет require, without distortion.
"""

import os
from PIL import Image


from PIL import ImageFilter


def _cover_crop(img, target_size):
    """Scale to fully cover target, center-crop. Used when source aspect ≈ target."""
    tw, th = target_size
    iw, ih = img.size
    if iw == tw and ih == th:
        return img
    scale = max(tw / iw, th / ih)
    new_w, new_h = int(iw * scale + 0.5), int(ih * scale + 0.5)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - tw) // 2
    top = (new_h - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _letterbox_fit(img, target_size, blur_extend=True):
    """Fit ENTIRE image inside target (preserve everything). Fill bars by extending the
    image content (heavy blur of the same image scaled up) so it never looks like a black
    bar — bars look like a continuation of the background.

    Used when source aspect differs SIGNIFICANTLY from target (e.g. 9:16 source → 3:4 target,
    which loses 30% of content if cover-cropped).
    """
    tw, th = target_size
    iw, ih = img.size
    # Inner: scale to FIT inside target (preserve everything, may have margins)
    scale = min(tw / iw, th / ih)
    inner_w, inner_h = int(iw * scale + 0.5), int(ih * scale + 0.5)
    inner = img.resize((inner_w, inner_h), Image.LANCZOS)

    # Background = same image scaled to COVER target, then blurred heavily.
    if blur_extend:
        bg_scale = max(tw / iw, th / ih) * 1.15
        bg_w, bg_h = int(iw * bg_scale + 0.5), int(ih * bg_scale + 0.5)
        bg = img.resize((bg_w, bg_h), Image.LANCZOS)
        left = (bg_w - tw) // 2
        top = (bg_h - th) // 2
        bg = bg.crop((left, top, left + tw, top + th))
        bg = bg.filter(ImageFilter.GaussianBlur(radius=42))
        # Slight darkening so the centered inner image stands out
        from PIL import ImageEnhance
        bg = ImageEnhance.Brightness(bg).enhance(0.85)
    else:
        bg = Image.new("RGB", (tw, th), (12, 12, 16))

    # Paste inner centered
    px = (tw - inner_w) // 2
    py = (th - inner_h) // 2
    bg.paste(inner, (px, py))
    return bg


def _smart_upscale(img, target_size, max_factor=1.6):
    tw, th = target_size
    iw, ih = img.size
    factor = max(tw / iw, th / ih, 1.0)
    if factor <= 1.0:
        return img
    factor = min(factor, max_factor)
    new_w, new_h = int(iw * factor + 0.5), int(ih * factor + 0.5)
    return img.resize((new_w, new_h), Image.LANCZOS)


def finalize_card(src_path, target_size, out_path, quality=95,
                  aspect_tolerance=0.12, upscale_long_side=2048):
    """Smart fit: cover-crop when aspect close, letterbox-extend when far.

    AI providers (esp. FastGen NanoPro) return 9:16 (~0.56) when target is 3:4 (0.75).
    Cover-cropping loses 30% of vertical content — including potentially headlines and
    badges placed at top/bottom of the AI render. Solution: detect the aspect mismatch
    and use letterbox-fit (with blurred edge-extend background) when the ratio difference
    exceeds the tolerance.

    После fit'а карточка апскейлится Lanczos-фильтром так, чтобы длинная сторона
    была upscale_long_side (по умолчанию 2048 — это ~2K). Аспект формата при
    этом сохраняется: для 3:4 выходит 1536×2048, для 1:1 → 2048×2048, для
    16:9 → 2048×1152 и т.д. Передай upscale_long_side=0 чтобы отключить
    апскейл (выйдет исходный target_size). Алгоритм Lanczos — это то же,
    что использует ffmpeg scale=flags=lanczos, только без вызова бинаря.
    """
    img = Image.open(src_path).convert("RGB")
    iw, ih = img.size
    tw, th = target_size
    src_ratio = iw / ih
    tgt_ratio = tw / th
    ratio_diff = abs(src_ratio - tgt_ratio) / max(src_ratio, tgt_ratio)

    if ratio_diff > aspect_tolerance:
        # Too different to safely cover-crop — letterbox with blurred extend.
        out = _letterbox_fit(img, target_size, blur_extend=True)
    else:
        # Close enough — cover-crop is fine.
        out = _smart_upscale(img, target_size)
        out = _cover_crop(out, target_size)

    # Финальный апскейл до 2K (длинная сторона). Аспект формата сохранён —
    # просто масштабируем фрейм, а не меняем композицию. Если карточка уже
    # больше или равна upscale_long_side по длинной стороне — skip.
    if upscale_long_side and upscale_long_side > 0:
        ow, oh = out.size
        long_side = max(ow, oh)
        if long_side < upscale_long_side:
            scale = upscale_long_side / long_side
            new_w = int(round(ow * scale))
            new_h = int(round(oh * scale))
            out = out.resize((new_w, new_h), Image.LANCZOS)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    out.save(out_path, "JPEG", quality=quality, optimize=True, subsampling=0)
    return out_path
