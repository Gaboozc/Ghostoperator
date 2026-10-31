"""OCR local con Tesseract (sin servicios externos) + filtro de contraste contra texto oculto."""
from __future__ import annotations

import csv
import io
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from . import config


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float
    contrast: float = 255.0

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


@dataclass
class Phrase:
    """Grupo de palabras contiguas en una misma línea (un botón, una etiqueta, una celda)."""
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x(self) -> int:
        return min(w.x for w in self.words)

    @property
    def y(self) -> int:
        return min(w.y for w in self.words)

    @property
    def x2(self) -> int:
        return max(w.x + w.w for w in self.words)

    @property
    def y2(self) -> int:
        return max(w.y + w.h for w in self.words)

    @property
    def h(self) -> int:
        return self.y2 - self.y

    @property
    def cx(self) -> int:
        return (self.x + self.x2) // 2

    @property
    def cy(self) -> int:
        return (self.y + self.y2) // 2


def _luma(img: Image.Image) -> np.ndarray:
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    return 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]


def _dark_regions_inverted(img: Image.Image, k: int = 9) -> Image.Image:
    """Zonas donde la mayoría de píxeles vecinos son oscuros (fondo de botón o barra, no trazos de texto)."""
    L = _luma(img)
    H, W = L.shape
    ii = np.pad((L < 130).astype(np.float32), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    r = k // 2
    y0, y1 = np.clip(np.arange(H) - r, 0, H), np.clip(np.arange(H) + r + 1, 0, H)
    x0, x1 = np.clip(np.arange(W) - r, 0, W), np.clip(np.arange(W) + r + 1, 0, W)
    area = (y1 - y0)[:, None] * (x1 - x0)[None, :]
    frac = (ii[y1][:, x1] - ii[y0][:, x1] - ii[y1][:, x0] + ii[y0][:, x0]) / area
    out = np.where((frac > 0.45) & (L > 150), 0, 255).astype(np.uint8)  # texto claro dentro de zona oscura -> negro
    return Image.fromarray(out, "L")


def run_tesseract(img: Image.Image) -> list[Word]:
    s = config.OCR_SCALE
    gray = img.convert("L")
    big = gray.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
    # Pasada 1: gris normal (texto oscuro sobre claro). Pasada 2: solo las zonas oscuras (botones azules, barra
    # de título) invertidas y binarizadas, el resto en blanco: el texto blanco sobre color queda negro sobre blanco.
    # Pasada 3: gris a escala 2: Tesseract en modo disperso se salta palabras aisladas según la escala (en la app v4.2
    # "Exportar" y "Crear pedido" solo salían a escala 2), y esta pasada las recupera.
    light_on_dark = _dark_regions_inverted(img).resize(big.size, Image.LANCZOS)
    s2 = 2.0
    small = gray.resize((int(img.width * s2), int(img.height * s2)), Image.LANCZOS)
    from concurrent.futures import ThreadPoolExecutor

    def _one(job: tuple[Image.Image, float]) -> tuple[str, float]:
        variant, scale = job
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "in.png"
            variant.save(p)
            res = subprocess.run(
                [config.TESSERACT_CMD, str(p), "stdout", "-l", config.OCR_LANG, "--psm", "11", "tsv"],
                capture_output=True, text=True, timeout=30, env={**__import__("os").environ, "OMP_THREAD_LIMIT": "1"},
            )
        return res.stdout, scale

    def _parse(out: str, scale: float) -> list[Word]:
        found: list[Word] = []
        for row in csv.DictReader(io.StringIO(out), delimiter="	", quoting=csv.QUOTE_NONE):
            t = (row.get("text") or "").strip()
            try:
                conf = float(row.get("conf") or -1)
            except ValueError:
                conf = -1
            if not t or conf < 30:
                continue
            found.append(Word(t, int(int(row["left"]) / scale), int(int(row["top"]) / scale),
                              max(1, int(int(row["width"]) / scale)), max(1, int(int(row["height"]) / scale)), conf))
        return found

    with ThreadPoolExecutor(max_workers=3) as ex:  # las tres pasadas en paralelo
        outputs = list(ex.map(_one, ((big, s), (light_on_dark, s), (small, s2))))
    base = _dedupe(_parse(*outputs[0]) + _parse(*outputs[1]))
    # la pasada 3 solo rellena huecos: sus cajas incluyen a veces el borde del campo ("[Carrera") y duplicarían texto
    gaps = [w for w in _parse(*outputs[2]) if not any(_overlaps(w, b) for b in base)]
    return _dedupe(base + gaps)


def _overlaps(a: Word, b: Word) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h


def _dedupe(words: list[Word]) -> list[Word]:
    out: list[Word] = []
    for w in sorted(words, key=lambda w: -w.conf):
        if any(abs(w.cx - o.cx) < max(4, o.w // 3) and abs(w.cy - o.cy) < max(4, o.h // 2) for o in out):
            continue
        out.append(w)
    return out


def apply_contrast(img: Image.Image, words: list[Word]) -> tuple[list[Word], list[Word]]:
    """Separa texto visible de texto casi invisible (posible inyección de instrucciones)."""
    L = _luma(img)
    visible, hidden = [], []
    for w in words:
        patch = L[max(0, w.y - 1): w.y + w.h + 1, max(0, w.x - 1): w.x + w.w + 1]
        if patch.size == 0:
            continue
        w.contrast = float(np.percentile(patch, 97) - np.percentile(patch, 3))
        (visible if w.contrast >= config.MIN_TEXT_CONTRAST else hidden).append(w)
    return visible, hidden


def group_lines(words: list[Word]) -> list[list[Word]]:
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.cy, w.x)):
        for line in lines:
            ref = line[-1]
            if abs(w.cy - np.mean([v.cy for v in line])) <= max(4, 0.45 * max(ref.h, w.h)):
                line.append(w)
                break
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w.x)
    lines.sort(key=lambda ln: np.mean([w.cy for w in ln]))
    return lines


def group_phrases(line: list[Word], gap_factor: float = 0.85) -> list[Phrase]:
    phrases: list[Phrase] = []
    cur = Phrase([line[0]])
    for w in line[1:]:
        prev = cur.words[-1]
        gap = w.x - (prev.x + prev.w)
        if gap <= gap_factor * max(prev.h, w.h):
            cur.words.append(w)
        else:
            phrases.append(cur)
            cur = Phrase([w])
    phrases.append(cur)
    return phrases
