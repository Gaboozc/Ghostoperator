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


def run_tesseract(img: Image.Image) -> list[Word]:
    s = config.OCR_SCALE
    big = img.convert("L").resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
    # Pasada 1: gris normal (texto oscuro sobre claro). Pasada 2: binarizado invertido, que convierte el texto
    # blanco sobre fondos de color (botones azules, barra de título) en negro sobre blanco.
    words: list[Word] = []
    light_on_dark = big.point(lambda p: 0 if p > 150 else 255)
    from concurrent.futures import ThreadPoolExecutor

    def _one(variant: Image.Image) -> str:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "in.png"
            variant.save(p)
            res = subprocess.run(
                [config.TESSERACT_CMD, str(p), "stdout", "-l", config.OCR_LANG, "--psm", "11", "tsv"],
                capture_output=True, text=True, timeout=30, env={**__import__("os").environ, "OMP_THREAD_LIMIT": "1"},
            )
        return res.stdout

    with ThreadPoolExecutor(max_workers=2) as ex:  # las dos pasadas en paralelo
        outputs = list(ex.map(_one, (big, light_on_dark)))
    for out in outputs:
        for row in csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE):
            t = (row.get("text") or "").strip()
            try:
                conf = float(row.get("conf") or -1)
            except ValueError:
                conf = -1
            if not t or conf < 30:
                continue
            words.append(Word(t, int(int(row["left"]) / s), int(int(row["top"]) / s),
                              max(1, int(int(row["width"]) / s)), max(1, int(int(row["height"]) / s)), conf))
    return _dedupe(words)


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
