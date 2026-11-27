"""Real-photo pigment extraction and face-aware portrait framing helper (Single Source of Truth).

Computes authentic in-gamut RGB hex pigments directly from downsampled real photograph pixels
(matching MindWeave v16's True Apple Music crystal refraction optics) and estimates upper-frame
portrait horizontal salience (`faceZone`: 'left' | 'center' | 'right') so Desktop & iPhone layouts
keep faces unobstructed.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True, slots=True)
class PhotoChromaProfile:
    """In-gamut real-photo pigments and face-aware framing hints derived from actual pixels."""

    real_warm: str
    real_cool: str
    real_light: str
    real_vivid: str
    palette: tuple[str, str, str]
    face_zone: str  # "left" | "center" | "right"
    desk_focus: str
    phone_focus: str

    def to_visual_token_dict(self) -> dict[str, str]:
        """Return scalar visualTokens compatible with `PersonRow.visual_tokens`."""
        return {
            "realWarm": self.real_warm,
            "realCool": self.real_cool,
            "realLight": self.real_light,
            "realVivid": self.real_vivid,
            "faceZone": self.face_zone,
            "deskFocus": self.desk_focus,
            "phoneFocus": self.phone_focus,
        }

    def to_card_meta_dict(self) -> dict[str, object]:
        """Return structured framing + palette metadata for photo `RecordRow.card_meta_enc`."""
        return {
            "palette": list(self.palette),
            "faceZone": self.face_zone,
            "deskFocus": self.desk_focus,
            "phoneFocus": self.phone_focus,
        }


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{max(0, min(255, r)):02X}{max(0, min(255, g)):02X}{max(0, min(255, b)):02X}"


def _is_skin_like(r: int, g: int, b: int) -> bool:
    """Lightweight YCbCr + RGB skin-tone heuristic for upper-frame portrait salience estimation."""
    if r <= 85 or g <= 40 or b <= 20:
        return False
    if not (r > g and r > b and abs(r - g) > 12):
        return False
    cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return 77.0 <= cb <= 127.0 and 133.0 <= cr <= 173.0


def extract_photo_chroma_and_framing(img: Image.Image) -> PhotoChromaProfile:
    """Extract 100% real-photo pigments and face-aware framing from an already-decoded Pillow Image."""
    sample = img.convert("RGB").resize((48, 48), Image.Resampling.BILINEAR)
    raw = sample.tobytes()

    best_warm: tuple[float, tuple[int, int, int]] = (-1.0, (224, 186, 134))
    best_cool: tuple[float, tuple[int, int, int]] = (-1.0, (132, 162, 154))
    best_light: tuple[float, tuple[int, int, int]] = (-1.0, (242, 232, 218))
    best_vivid: tuple[float, tuple[int, int, int]] = (-1.0, (206, 146, 114))

    r_sum = 0
    g_sum = 0
    b_sum = 0
    n_px = 48 * 48

    # Track horizontal salience in upper 62% of frame for face-aware composition
    skin_x_weight_sum = 0.0
    skin_weight_total = 0.0
    left_skin = 0.0
    right_skin = 0.0
    center_skin = 0.0

    for idx in range(n_px):
        off = idx * 3
        r = raw[off]
        g = raw[off + 1]
        b = raw[off + 2]
        r_sum += r
        g_sum += g
        b_sum += b

        h, l_val, s_val = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        deg = h * 360.0

        # Warm real-photo pigment (amber / peach / ochre / terracotta / rose)
        if 0.22 <= l_val <= 0.84:
            warm_hue_bonus = 1.0 if (deg <= 68.0 or deg >= 325.0) else 0.15
            warm_score = (s_val * 1.4 + 0.2) * warm_hue_bonus * (1.0 - abs(l_val - 0.56))
            if warm_score > best_warm[0]:
                best_warm = (warm_score, (r, g, b))

        # Cool/calm real-photo pigment (foliage green / slate / dusk blue / teal)
        if 0.18 <= l_val <= 0.78:
            cool_hue_bonus = 1.0 if (72.0 <= deg <= 265.0) else 0.15
            cool_score = (s_val * 1.3 + 0.25) * cool_hue_bonus * (1.0 - abs(l_val - 0.46))
            if cool_score > best_cool[0]:
                best_cool = (cool_score, (r, g, b))

        # Luminous highlight pigment (soft ivory / morning sky / warm silk)
        if 0.58 <= l_val <= 0.94:
            light_score = l_val * 0.9 + (0.25 - abs(s_val - 0.25))
            if light_score > best_light[0]:
                best_light = (light_score, (r, g, b))

        # Vivid focal pigment (highest clean chroma in photo)
        if 0.24 <= l_val <= 0.76:
            vivid_score = s_val * 1.6 * (1.0 - abs(l_val - 0.50))
            if vivid_score > best_vivid[0]:
                best_vivid = (vivid_score, (r, g, b))

        # Upper 62% portrait salience
        y = idx // 48
        if y < 30 and _is_skin_like(r, g, b):
            x_norm = (idx % 48) / 47.0
            # Slight vertical preference for head/face band (y in 4..26)
            y_weight = 1.35 if 4 <= y <= 26 else 0.7
            skin_x_weight_sum += x_norm * y_weight
            skin_weight_total += y_weight
            if x_norm < 0.38:
                left_skin += y_weight
            elif x_norm > 0.60:
                right_skin += y_weight
            else:
                center_skin += y_weight

    avg_rgb = (r_sum // n_px, g_sum // n_px, b_sum // n_px)
    warm_rgb = best_warm[1] if best_warm[0] > 0 else avg_rgb
    cool_rgb = best_cool[1] if best_cool[0] > 0 else avg_rgb
    light_rgb = best_light[1] if best_light[0] > 0 else avg_rgb
    vivid_rgb = best_vivid[1] if best_vivid[0] > 0 else warm_rgb

    warm_hex = _rgb_to_hex(*warm_rgb)
    cool_hex = _rgb_to_hex(*cool_rgb)
    light_hex = _rgb_to_hex(*light_rgb)
    vivid_hex = _rgb_to_hex(*vivid_rgb)

    face_zone = "center"
    desk_focus = "50% 22%"
    phone_focus = "50% 22%"

    if skin_weight_total >= 18.0:
        x_mean = skin_x_weight_sum / skin_weight_total
        if x_mean >= 0.58 and right_skin > left_skin * 1.6 and right_skin >= center_skin * 0.75:
            face_zone = "right"
            desk_focus = "62% 20%"
            phone_focus = "62% 20%"
        elif x_mean <= 0.42 and left_skin > right_skin * 1.6 and left_skin >= center_skin * 0.75:
            face_zone = "left"
            desk_focus = "38% 20%"
            phone_focus = "38% 20%"

    return PhotoChromaProfile(
        real_warm=warm_hex,
        real_cool=cool_hex,
        real_light=light_hex,
        real_vivid=vivid_hex,
        palette=(warm_hex, cool_hex, vivid_hex),
        face_zone=face_zone,
        desk_focus=desk_focus,
        phone_focus=phone_focus,
    )
