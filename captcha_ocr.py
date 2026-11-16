"""Lectura del captcha numérico de ICutLink ("Selecciona el número N").

El desafío muestra 6 insignias oscuras; dentro de cada una está el número
dibujado en gris medio. El pipeline comprobado es:

1. Localizar la insignia (zona oscura mayor del PNG).
2. Recortar su interior con margen.
3. Aislar los píxeles de tono medio (los dígitos), en varias bandas.
4. Separar los dos glifos por la proyección de columnas.
5. Reconocer cada glifo con easyocr y votar entre bandas/escalas.

Las dependencias (easyocr, opencv, numpy) se importan de forma perezosa:
si faltan, ``available()`` devuelve False y el bot cae al modo manual.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from collections import Counter
from typing import Any

_READER: Any = None
_READER_FAILED = False

# Bandas de gris (0-255) donde se dibujan los dígitos. Los dígitos son más
# claros que la insignia (~55) y más oscuros que el fondo (~210).
_BANDS = ((100, 200), (110, 190), (95, 205), (120, 200), (105, 175))
_SCALES = (140, 190)
_DARK_TH = 140
_NOISE_AREA = 18

_PATTERNS = (
    re.compile(r"n[uú]mero\s*\[?\s*(\d{1,3})", re.I),
    re.compile(r"number\s*\[?\s*(\d{1,3})", re.I),
    re.compile(r"(\d{1,3})\s*(?:de los botones|from the buttons)", re.I),
)


def available() -> bool:
    """¿Están easyocr y opencv disponibles?"""
    global _READER_FAILED
    if _READER_FAILED:
        return False
    try:
        import cv2  # noqa: F401
        import easyocr  # noqa: F401
        import numpy  # noqa: F401
    except Exception:
        _READER_FAILED = True
        return False
    return True


def _reader() -> Any:
    global _READER, _READER_FAILED
    if _READER is not None:
        return _READER
    if _READER_FAILED:
        return None
    try:
        import easyocr

        _READER = easyocr.Reader(["en"], gpu=False, verbose=False)
        return _READER
    except Exception:
        _READER_FAILED = True
        return None


def _normalize(text: str) -> str:
    low = unicodedata.normalize("NFKD", text or "")
    low = "".join(ch for ch in low if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", low).strip().lower()


def parse_target_number(text: str) -> int | None:
    """Extrae el número pedido ("Selecciona el número [82]" -> 82)."""
    low = _normalize(text)
    for pattern in _PATTERNS:
        match = pattern.search(low)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 999:
            return value
    return None


def decode_png(src: str) -> bytes | None:
    if not src:
        return None
    if src.startswith("data:"):
        try:
            return base64.b64decode(src.split(",", 1)[1])
        except Exception:
            return None
    return None


def _badge_box(gray: Any) -> tuple[int, int, int, int] | None:
    import cv2
    import numpy as np

    dark = (gray < _DARK_TH).astype(np.uint8)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        dark, 8
    )
    if count < 2:
        return None
    index = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, _area = (int(v) for v in stats[index])
    if w < 8 or h < 6:
        return None
    return x, y, w, h


def _mid_masks(gray: Any, box: tuple[int, int, int, int]) -> list[Any]:
    import cv2
    import numpy as np

    x, y, w, h = box
    pad = 2
    gs = gray[y + pad:y + h - pad, x + pad:x + w - pad]
    if gs.size == 0:
        return []
    masks = []
    for lo, hi in _BANDS:
        mask = ((gs >= lo) & (gs <= hi)).astype(np.uint8) * 255
        count, labels, stats, _c = cv2.connectedComponentsWithStats(mask, 8)
        clean = np.zeros_like(mask)
        for i in range(1, count):
            if stats[i, cv2.CC_STAT_AREA] >= _NOISE_AREA:
                clean[labels == i] = 255
        masks.append(clean)
    return masks


def _split_two(mask: Any) -> list[Any]:
    """Separa la máscara en los dos glifos."""
    import numpy as np

    cols = (mask > 0).sum(axis=0)
    xs = np.where(cols > 0)[0]
    if not len(xs):
        return []
    x0, x1 = int(xs.min()), int(xs.max())
    width = x1 - x0 + 1
    if width < 12:
        return [mask[:, x0:x1 + 1]]
    lo = x0 + int(width * 0.32)
    hi = x0 + max(int(width * 0.68), int(width * 0.32) + 1)
    cut = lo + int(np.argmin(cols[lo:hi + 1]))
    parts = []
    for a, b in ((x0, cut), (cut, x1 + 1)):
        part = mask[:, a:b]
        if (part > 0).sum() >= 25:
            parts.append(part)
    if len(parts) < 2:
        mid = x0 + width // 2
        parts = [mask[:, x0:mid], mask[:, mid:x1 + 1]]
        parts = [p for p in parts if (p > 0).sum() > 0]
    return parts


def _ocr_glyph(glyph: Any, scale: int) -> tuple[str, float] | None:
    import cv2
    import numpy as np

    reader = _reader()
    if reader is None:
        return None
    ys, xs = np.where(glyph > 0)
    if not len(xs):
        return None
    g = glyph[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    height, width = g.shape
    if height < 6 or width < 3:
        return None
    factor = float(scale) / height
    big = cv2.resize(g, (max(1, int(width * factor)), scale),
                     interpolation=cv2.INTER_CUBIC)
    big = cv2.bitwise_not(big)
    big = cv2.copyMakeBorder(big, 45, 45, 45, 45,
                             cv2.BORDER_CONSTANT, value=255)
    try:
        results = reader.recognize(
            big,
            horizontal_list=[[0, big.shape[1], 0, big.shape[0]]],
            free_list=[], allowlist="0123456789", detail=1,
            decoder="beamsearch",
        )
    except Exception:
        return None
    if not results:
        return None
    best = max(results, key=lambda item: item[2])
    text = "".join(ch for ch in str(best[1]) if ch.isdigit())
    if not text:
        return None
    return text[0], float(best[2])


def read_number(png: bytes) -> tuple[str, float, dict[str, int]]:
    """Lee el número de dos cifras de una insignia.

    Devuelve ``(texto, confianza, votos)``. ``votos`` cuenta cuántas veces
    salió cada candidato completo, útil para medir la fiabilidad.
    """
    import cv2
    import numpy as np

    buffer = np.frombuffer(png, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        return "", 0.0, {}
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    box = _badge_box(gray)
    if box is None:
        return "", 0.0, {}

    slot_votes: list[list[tuple[str, float]]] = [[], []]
    for mask in _mid_masks(gray, box):
        parts = _split_two(mask)
        if len(parts) < 2:
            continue
        for scale in _SCALES:
            for slot in (0, 1):
                result = _ocr_glyph(parts[slot], scale)
                if result is not None:
                    slot_votes[slot].append(result)

    def pick(votes: list[tuple[str, float]]) -> tuple[str, float]:
        if not votes:
            return "?", 0.0
        counts = Counter(ch for ch, _c in votes)
        by_char: dict[str, list[float]] = {}
        for ch, conf in votes:
            by_char.setdefault(ch, []).append(conf)
        best = max(
            counts,
            key=lambda ch: (counts[ch], sum(by_char[ch]) / len(by_char[ch])),
        )
        mean = sum(by_char[best]) / len(by_char[best])
        # Confianza = proporción de votos del ganador.
        ratio = counts[best] / len(votes)
        return best, ratio * mean

    first, first_conf = pick(slot_votes[0])
    second, second_conf = pick(slot_votes[1])
    text = f"{first}{second}"
    confidence = (first_conf + second_conf) / 2.0
    return text, confidence, {text: 1}


def _rank(votes: list[tuple[str, float]],
          limit: int = 4) -> list[tuple[str, float]]:
    """Ordena votos de un glifo por (frecuencia, confianza)."""
    if not votes:
        return []
    counts = Counter(ch for ch, _c in votes)
    by_char: dict[str, list[float]] = {}
    for ch, conf in votes:
        by_char.setdefault(ch, []).append(conf)
    ordered = sorted(
        counts,
        key=lambda ch: (counts[ch], sum(by_char[ch]) / len(by_char[ch])),
        reverse=True,
    )
    return [
        (ch, counts[ch] / len(votes) *
         (sum(by_char[ch]) / len(by_char[ch])))
        for ch in ordered[:limit]
    ]


def read_slots(png: bytes) -> tuple[list[tuple[str, float]],
                                    list[tuple[str, float]]]:
    """Devuelve los candidatos (carácter, confianza) de cada posición."""
    import cv2
    import numpy as np

    buffer = np.frombuffer(png, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        return [], []
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    box = _badge_box(gray)
    if box is None:
        return [], []

    slot_votes: list[list[tuple[str, float]]] = [[], []]
    for mask in _mid_masks(gray, box):
        parts = _split_two(mask)
        if len(parts) < 2:
            continue
        for scale in _SCALES:
            for slot in (0, 1):
                result = _ocr_glyph(parts[slot], scale)
                if result is not None:
                    slot_votes[slot].append(result)

    return _rank(slot_votes[0]), _rank(slot_votes[1])


def read_button_candidates(png: bytes) -> list[tuple[str, float]]:
    """Devuelve lecturas alternativas ordenadas por confianza."""
    left, right = read_slots(png)
    if not left or not right:
        return []
    combined = []
    for text_a, conf_a in left:
        for text_b, conf_b in right:
            combined.append((f"{text_a}{text_b}", conf_a * conf_b))
    combined.sort(key=lambda item: item[1], reverse=True)
    return combined


def solve_buttons(
    prompt: str, button_sources: list[str]
) -> tuple[list[int], list[list[tuple[str, float]]]]:
    """Devuelve (índices ordenados por probabilidad, lecturas por botón).

    Puntúa cada botón comparando **dígito a dígito** con el objetivo: un
    acierto en la posición 0 o en la 1 suma puntos aunque el otro dígito se
    haya leído mal. Esto tolera los errores parciales del OCR.
    """
    target = parse_target_number(prompt)
    readings: list[list[tuple[str, float]]] = []
    slots: list[tuple[list[tuple[str, float]], list[tuple[str, float]]]] = []
    for src in button_sources:
        png = decode_png(src)
        if not png:
            readings.append([])
            slots.append(([], []))
            continue
        left, right = read_slots(png)
        slots.append((left, right))
        combined = []
        for text_a, conf_a in left:
            for text_b, conf_b in right:
                combined.append((f"{text_a}{text_b}", conf_a * conf_b))
        combined.sort(key=lambda item: item[1], reverse=True)
        readings.append(combined)

    if target is None:
        return [], readings
    want = str(target)

    scored: list[tuple[float, int]] = []
    for index, (left, right) in enumerate(slots):
        score = 0.0
        for position, candidates in enumerate((left, right)):
            digit = want[position] if position < len(want) else ""
            for char, confidence in candidates:
                if char == digit:
                    score += 1.0 + confidence
                    break
        # Bonificación si el número completo fue leído.
        for text, confidence in readings[index]:
            if text == want:
                score += 2.0 + confidence
                break
        if score > 0:
            scored.append((score, index))

    if not scored:
        # Último recurso: el botón con mejor confianza global.
        fallback = []
        for index, candidates in enumerate(readings):
            best = candidates[0][1] if candidates else 0.0
            fallback.append((best, index))
        fallback.sort(reverse=True)
        return [index for _conf, index in fallback], readings

    scored.sort(reverse=True)
    return [index for _score, index in scored], readings