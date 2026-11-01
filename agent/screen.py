"""ScreenState: lo que el agente 've' en una captura, en forma estructurada y probable.

- elementos numerados [1..N] (texto + caja) -> el modelo elige un número, el código calcula el centro
- campos de formulario detectados junto a su etiqueta ("campo bajo 'NIT'")
- tabla principal (encabezados + filas) para contar y desambiguar sin modelo
- barra de menú, título de pantalla, huella de app, versión y la fecha "Hoy" de la app
- texto oculto (bajo contraste) separado: nunca se le pasa al modelo como instrucción
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from .ocr import Phrase, Word, apply_contrast, group_lines, group_phrases, run_tesseract, _luma

DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
VERSION_RE = re.compile(r"\bv\d+(?:\.\d+)+\b", re.I)


def norm(s: str) -> str:
    """Normaliza para comparar etiquetas (sin tildes, minúsculas). OJO: para buscar datos NO se usa esto,
    porque la app puede distinguir tildes."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


@dataclass
class Element:
    id: int
    text: str
    x: int
    y: int
    x2: int
    y2: int
    role: str = "texto"  # texto | menu | celda | campo | encabezado | titulo | enlace

    @property
    def cx(self) -> int:
        return (self.x + self.x2) // 2

    @property
    def cy(self) -> int:
        return (self.y + self.y2) // 2

    def as_dict(self) -> dict:
        return {"id": self.id, "texto": self.text, "rol": self.role, "caja": [self.x, self.y, self.x2, self.y2]}


@dataclass
class Table:
    columns: list[str]
    col_x: list[int]
    rows: list[dict]
    row_y: list[int]
    header_y: int

    def as_records(self) -> list[dict]:
        return self.rows


@dataclass
class ScreenState:
    img: Image.Image
    elements: list[Element]
    hidden_text: list[str]
    menu: list[Element]
    title: str
    fingerprint: str
    version: str
    hoy: Optional[str]
    table: Optional[Table]
    lines: list[str] = field(default_factory=list)
    view: Optional[Image.Image] = None  # captura tal cual (en tema oscuro, img es la invertida)
    dark: bool = False
    hoy_dia: Optional[str] = None  # día de la semana junto a la fecha (ej. 'martes')

    # -------------------------------------------------- búsqueda de elementos por texto (anclas)
    def find(self, text: str, role: Optional[str] = None, exact: bool = False) -> Optional[Element]:
        t = norm(text)
        cands = [e for e in self.elements if role is None or e.role == role]
        for e in cands:
            if norm(e.text) == t:
                return e
        if exact:
            return None
        for e in cands:
            if t and t in norm(e.text):
                return e
        return None

    def find_all(self, text: str) -> list[Element]:
        t = norm(text)
        return [e for e in self.elements if t and t in norm(e.text)]

    def input_point(self, e: Element) -> tuple[int, int]:
        """Dónde hacer clic para escribir en el campo al que se refiere N: si N es el valor que ya está dentro de una
        caja (bordes pegados arriba y abajo del texto) se pulsa sobre él; si es su etiqueta, dentro de la caja de abajo."""
        L = _luma(self.img)
        H, W = L.shape
        x = min(W - 1, e.x2 + 6)  # justo a la derecha del texto: fondo de la caja, sin trazos de letras

        def dist(step: int) -> Optional[int]:
            for d in range(1, 13):
                yy = e.cy + step * d
                if not 0 <= yy < H:
                    return None
                if L[yy, x] < 215:  # borde de la caja
                    return d
            return None

        up, down = dist(-1), dist(1)
        if up is not None and down is not None and abs(up - down) <= 4:  # centrado entre dos bordes: valor dentro de la caja
            return e.cx, e.cy
        x1, x2 = e.x + 2, min(W, e.x + 42)
        for y in range(e.cy + 1, min(H, e.y2 + 26)):
            # borde superior de la caja bajo la etiqueta: una línea continua (los trazos de letras dejan huecos claros)
            if float(np.percentile(L[y, x1:x2], 90)) < 225:
                return min(W - 1, e.x + 25), min(H - 1, y + 9)
        return e.cx, min(H - 1, e.y2 + 12)

    def field_for(self, label: str) -> Optional[Element]:
        t = norm(label)
        for e in self.elements:
            if e.role == "campo" and norm(e.text.removeprefix("campo ")) == t:
                return e
        for e in self.elements:
            if e.role == "campo" and t in norm(e.text):
                return e
        return None

    @property
    def signature(self) -> str:
        return norm(self.title)

    def text_blob(self) -> str:
        return "\n".join(self.lines)

    # -------------------------------------------------- representación para el modelo
    def describe(self, max_elems: int = 160) -> str:
        out = [f"Pantalla: {self.title or '(sin título)'} | Hoy (según la app): {(self.hoy_dia + ' ') if self.hoy_dia else ''}{self.hoy or 'desconocido'}"]
        if self.menu:
            out.append("Menú superior: " + " | ".join(f"[{m.id}] {m.text}" for m in self.menu))
        # primero controles, etiquetas y campos (los campos se numeran al final); las celdas, hasta el tope
        others = [e for e in self.elements if e.role not in ("menu", "celda")]
        cells = [e for e in self.elements if e.role == "celda"]
        for e in sorted(others, key=lambda e: (e.y, e.x)) + cells[:max(0, max_elems - len(others))]:
            out.append(f"[{e.id}] ({e.role}) {e.text}")
        if self.table:
            out.append(f"Tabla visible: columnas={self.table.columns}, filas visibles={len(self.table.rows)}")
        return "\n".join(out)

    def marked_image(self) -> Image.Image:
        im = (self.view or self.img).convert("RGB").copy()
        d = ImageDraw.Draw(im)
        for e in self.elements:
            color = (220, 40, 40) if e.role == "campo" else (30, 90, 200)
            d.rectangle([e.x - 1, e.y - 1, e.x2 + 1, e.y2 + 1], outline=color, width=1)
            tag = str(e.id)
            tx, ty = max(0, e.x - 2), max(0, e.y - 11)
            d.rectangle([tx, ty, tx + 7 * len(tag) + 2, ty + 10], fill=color)
            d.text((tx + 1, ty - 1), tag, fill=(255, 255, 255))
        return im


# ====================================================== construcción
def build_state(img: Image.Image) -> ScreenState:
    view = img
    dark = float(_luma(img).mean()) < 110
    if dark:  # tema oscuro: se percibe sobre la imagen invertida (cajas de campo claras, texto oscuro); el modelo ve la original
        img = ImageOps.invert(img.convert("RGB"))
    words = run_tesseract(img)
    visible, hidden = apply_contrast(img, words)
    lines = group_lines(visible)
    L = _luma(img)
    H, W = L.shape

    elements: list[Element] = []
    menu: list[Element] = []
    line_texts: list[str] = []
    line_phrases: list[list[Phrase]] = []

    for ln in lines:
        phrases = [q for p in group_phrases(ln) for q in _split_at_borders(p, L)]
        line_phrases.append(phrases)
        line_texts.append("   ".join(p.text for p in phrases))

    # barra de título (huella) = primera línea; barra de menú = primera línea corta con >= 3 entradas en el 20 % superior
    menu_idx = None
    for i, phrases in enumerate(line_phrases):
        y = phrases[0].y
        if y > 0.2 * H:
            break
        if i == 0 and len(line_phrases) > 1:
            continue
        if 3 <= len(phrases) <= 12 and all(len(p.words) <= 2 for p in phrases) and ":" not in line_texts[i]:
            menu_idx = i
            break

    title = ""
    if menu_idx is not None:
        for phrases in line_phrases[menu_idx + 1:]:
            if phrases[0].y > 0.35 * H:
                break
            if len(phrases) <= 2 and 1 <= len(phrases[0].words) <= 5:
                title = phrases[0].text
                break

    first = line_texts[0] if line_texts else ""
    version = (VERSION_RE.search(first) or [""])[0] if first else ""
    head = line_phrases[0][0].text if line_phrases and line_phrases[0] else first  # solo el nombre de la app
    fp_words = [w for w in re.split(r"\s+", head) if w and not any(c.isdigit() for c in w)]
    stop = {"hoy", "usuario", "hoy:", "usuario:", "·", "-", "|"}
    fingerprint = norm(" ".join(w for w in fp_words if w.lower() not in stop))[:60] or "app"

    blob = "\n".join(line_texts)
    hoy = None
    hoy_dia = None
    date_re = r"(\d{1,2}/\d{1,2}/\d{4})"
    m = re.search(r"hoy\s*:?\s*(?:([a-záéíóúñ]+)[,.]?\s+)?" + date_re, blob, re.I)
    if m:
        hoy, hoy_dia = m.group(2), (m.group(1) or "").lower() or None
    else:  # el encabezado puede decir "Fecha: ..." u otra palabra: la primera fecha de las primeras líneas
        m = re.search(date_re, "\n".join(line_texts[:3]))
        if m:
            hoy = m.group(1)

    table = parse_table(line_phrases)
    table_ys = set(table.row_y + [table.header_y]) if table else set()

    eid = 0
    for i, phrases in enumerate(line_phrases):
        for p in phrases:
            text = p.text.strip("|_[] ").strip()  # bordes de cajas que el OCR lee como '|'
            if not text:
                continue
            eid += 1
            role = "texto"
            if i == menu_idx:
                role = "menu"
            elif text == title:
                role = "titulo"
            elif table and any(abs(p.cy - y) <= 6 for y in table_ys):
                role = "encabezado" if abs(p.cy - table.header_y) <= 6 else "celda"
            if role == "texto":
                text = fix_ui_word(text)
            e = Element(eid, text, p.x, p.y, p.x2, p.y2, role)
            elements.append(e)
            if role == "menu":
                menu.append(e)

    # enlaces: texto azul (abren la ficha del registro, ej. el código en la tabla)
    rgb = np.asarray(img.convert("RGB"), dtype=np.int16)
    for e in elements:
        if e.role in ("texto", "celda") and _is_link(rgb, L, e):
            e.role = "enlace"

    # campos de formulario: caja blanca bajo (o a la derecha de) una etiqueta corta
    table_top = table.header_y - 10 if table else H
    for e in list(elements):
        if e.role not in ("texto",) or len(e.text.split()) > 4 or e.cy > table_top:
            continue
        if np.median(L[e.y:e.y2 + 1, e.x:e.x2 + 1]) < 180:  # texto de botón (fondo oscuro), no una etiqueta
            continue
        box = field_below(L, e) or field_right(L, e)
        if box:
            eid += 1
            elements.append(Element(eid, f"campo {e.text}", *box, role="campo"))

    return ScreenState(img=img, elements=elements, hidden_text=[w.text for w in hidden], menu=menu, title=title,
                       fingerprint=fingerprint, version=version, hoy=hoy, hoy_dia=hoy_dia, table=table, lines=line_texts,
                       view=view, dark=dark)


UI_WORDS = ("exportar", "filtrar", "limpiar", "guardar", "siguiente", "entendido")


def fix_ui_word(text: str) -> str:
    """El OCR lee mal algunos botones ('Expgytar', 'Hitrar', 'Guarder'). Con un vocabulario corto, que no choca con estados
    ni nombres de datos, una palabra suelta muy parecida se corrige: así el modelo y las guardas ven el botón de verdad."""
    n = norm(text)
    if " " in n or len(n) < 6 or n in UI_WORDS:
        return text
    best = max(UI_WORDS, key=lambda w: difflib.SequenceMatcher(None, n, w).ratio())
    return best.capitalize() if difflib.SequenceMatcher(None, n, best).ratio() >= 0.74 else text


def _is_link(rgb: np.ndarray, L: np.ndarray, e: "Element") -> bool:
    if np.median(L[e.y:e.y2 + 1, e.x:e.x2 + 1]) < 180:  # texto sobre fondo oscuro = botón, no enlace
        return False
    patch = rgb[e.y:e.y2 + 1, e.x:e.x2 + 1].reshape(-1, 3)
    ink = patch[L[e.y:e.y2 + 1, e.x:e.x2 + 1].reshape(-1) < 150]  # píxeles del texto
    if len(ink) < 8:
        return False
    r, g, b = ink.mean(axis=0)
    return b - r > 60 and b > 120


def _split_at_borders(p: Phrase, L: np.ndarray) -> list[Phrase]:
    """Dos botones vecinos ("Nuevo cliente" | "Exportar") pueden quedar en una sola frase: se parte donde hay
    un borde visual entre dos palabras (el fondo del hueco no es uniforme)."""
    if len(p.words) < 2:
        return [p]
    out, cur = [], [p.words[0]]
    y1, y2 = p.y, p.y2
    for w in p.words[1:]:
        if not w.text.strip("|_[]!"):  # el OCR leyó el borde como una "palabra" '|': separador
            out.append(Phrase(cur)) if cur else None
            cur = []
            continue
        if not cur:
            cur = [w]
            continue
        a, b = cur[-1].x + cur[-1].w + 1, w.x - 1  # sin el filo de las letras
        if b - a >= 2:
            cols = L[y1:y2 + 1, a:b].mean(axis=0)
            if cols.size and cols.max() - cols.min() > 45:
                out.append(Phrase(cur))
                cur = [w]
                continue
        cur.append(w)
    if cur:
        out.append(Phrase(cur))
    return out or [p]


def _box_from_interior(L: np.ndarray, x: int, y: int) -> Optional[tuple[int, int, int, int]]:
    H, W = L.shape
    if not (0 <= y < H and 0 <= x < W) or L[y, x] < 230:
        return None
    x1 = x
    while x1 > 0 and L[y, x1 - 1] >= 200:
        x1 -= 1
    x2 = x
    while x2 < W - 1 and L[y, x2 + 1] >= 200:
        x2 += 1
    y1 = y
    while y1 > 0 and L[y1 - 1, x + 2] >= 200:
        y1 -= 1
    y2 = y
    while y2 < H - 1 and L[y2 + 1, x + 2] >= 200:
        y2 += 1
    w, h = x2 - x1, y2 - y1
    if 90 <= w <= 900 and 12 <= h <= 48:  # < 90 px es un botón (ej. 'Volver'), no un campo
        return x1, y1, x2, y2
    return None


def field_below(L: np.ndarray, e: Element) -> Optional[tuple[int, int, int, int]]:
    for dy in range(3, 26):
        y = e.y2 + dy
        if y >= L.shape[0]:
            return None
        if L[y, e.x + 4] < 215:  # borde superior de la caja
            for k in range(2, 8):
                b = _box_from_interior(L, e.x + 6, y + k)
                if b:
                    return b
            return None
    return None


def field_right(L: np.ndarray, e: Element) -> Optional[tuple[int, int, int, int]]:
    for dx in range(4, 40):
        x = e.x2 + dx
        if x >= L.shape[1]:
            return None
        if L[e.cy, x] < 215:
            for k in range(2, 8):
                b = _box_from_interior(L, x + k, e.cy)
                if b:
                    return b
            return None
    return None


def parse_table(line_phrases: list[list[Phrase]]) -> Optional[Table]:
    """Encuentra la tabla principal: un encabezado con >= 3 columnas y filas alineadas debajo."""
    cells_per_line = []
    for phrases in line_phrases:
        merged = _merge_cells(phrases)
        cells_per_line.append(merged)

    best = None
    for i, header in enumerate(cells_per_line):
        if len(header) < 3:
            continue
        hx = [c.x for c in header]
        rows = []
        for j in range(i + 1, len(cells_per_line)):
            cells = cells_per_line[j]
            aligned = [c for c in cells if any(abs(c.x - x) <= 14 for x in hx)]
            if len(aligned) >= max(2, len(header) - 2) and len(aligned) >= 0.6 * len(cells):
                rows.append(j)
            elif rows:
                break
        if rows and (best is None or len(rows) > len(best[1])):
            best = (i, rows)
    if not best:
        return None
    i, row_idx = best
    header = cells_per_line[i]
    cols = [c.text for c in header]
    col_x = [c.x for c in header]
    records, ys = [], []
    for j in row_idx:
        rec = {c: "" for c in cols}
        for c in cells_per_line[j]:
            k = max([n for n, x in enumerate(col_x) if x <= c.x + 14] or [0])
            rec[cols[k]] = (rec[cols[k]] + " " + c.text).strip()
        records.append(rec)
        ys.append(cells_per_line[j][0].cy)
    return Table(cols, col_x, records, ys, header[0].cy)


def _merge_cells(phrases: list[Phrase]) -> list[Phrase]:
    """Une frases separadas por espacios medianos (palabras de una misma celda) pero no por columnas."""
    if not phrases:
        return []
    out = [Phrase(list(phrases[0].words))]
    for p in phrases[1:]:
        prev = out[-1]
        if p.x - prev.x2 <= 1.6 * max(prev.h, p.h):
            prev.words.extend(p.words)
        else:
            out.append(Phrase(list(p.words)))
    return out


def row_stripes(img: Image.Image, table: Table) -> list[tuple[int, int]]:
    """Cuenta las filas de la tabla por píxeles: zona clara bajo el encabezado, partida por las líneas
    divisorias (mínimos locales de luminancia). No depende del OCR: una fila mal leída no desaparece."""
    L = _luma(img)
    H, W = L.shape
    x0 = max(0, table.col_x[0] - 6)
    prof = np.median(L[:, x0:x0 + 4], axis=1)
    y = table.header_y + 4
    lim = min(H, table.header_y + 40)
    while y < lim and prof[y] < 236:
        y += 1
    if y >= lim:
        return []
    top = y
    low = 0
    while y < H:
        low = low + 1 if prof[y] < 236 else 0
        if low >= 6:
            break
        y += 1
    bottom = y - low
    cuts = [top]
    for yy in range(top + 6, bottom - 4):
        v = prof[yy]
        if v < 236 and v <= prof[yy - 1] and v <= prof[yy + 1] and v < min(prof[yy - 3], prof[yy + 3]) - 8:
            if yy - cuts[-1] >= 8:
                cuts.append(yy)
    cuts.append(bottom)
    segs = [(a + 1, b - 1) for a, b in zip(cuts, cuts[1:]) if b - a >= 8]
    if not segs:
        return []
    med = float(np.median([b - a for a, b in segs]))
    out: list[tuple[int, int]] = []
    for a, b in segs:  # una divisoria que no se vio: partir el bloque en filas de altura típica
        n = max(1, int(round((b - a) / med)))
        step = (b - a) / n
        out.extend((int(a + i * step), int(a + (i + 1) * step)) for i in range(n))
    return out


def read_table_rows(img: Image.Image, table: Table) -> list[dict]:
    """Relee la tabla fila por fila: filas por píxeles, texto por OCR del recorte de la tabla."""
    from . import config
    import subprocess, tempfile, csv, io
    from pathlib import Path

    stripes = row_stripes(img, table)
    if not stripes:
        return table.rows
    left = max(0, table.col_x[0] - 8)
    top, bottom = stripes[0][0], stripes[-1][1]
    crop = img.crop((left, top, img.width, bottom)).convert("L")
    s = 3.0
    big = crop.resize((int(crop.width * s), int(crop.height * s)), Image.LANCZOS)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.png"
        big.save(p)
        res = subprocess.run([config.TESSERACT_CMD, str(p), "stdout", "-l", config.OCR_LANG, "--psm", "6", "tsv"],
                             capture_output=True, text=True, timeout=30)
    rows = [{c: "" for c in table.columns} for _ in stripes]
    for r in csv.DictReader(io.StringIO(res.stdout), delimiter="\t", quoting=csv.QUOTE_NONE):
        t = (r.get("text") or "").strip()
        if not t:
            continue
        try:
            if float(r.get("conf") or -1) < 20:
                continue
        except ValueError:
            continue
        wx = left + int(r["left"]) / s
        wy = top + (int(r["top"]) + int(r["height"]) / 2) / s
        ri = next((i for i, (a, b) in enumerate(stripes) if a - 2 <= wy <= b + 2), None)
        if ri is None:
            continue
        k = max([n for n, x in enumerate(table.col_x) if x <= wx + 14] or [0])
        col = table.columns[k]
        rows[ri][col] = (rows[ri][col] + " " + t).strip()
    return rows


def label_at(img: Image.Image, x: int, y: int) -> str:
    """Texto del control que hay bajo (x, y), leído en un recorte: sirve cuando el OCR general no lo detectó
    (en la app v4.2 "Exportar" no salía) y las guardas de guardar/exportar necesitan saber qué se pulsa."""
    import csv, io, subprocess, tempfile
    from pathlib import Path
    from . import config

    box = (max(0, x - 70), max(0, y - 14), min(img.width, x + 70), min(img.height, y + 14))
    crop = img.convert("L").crop(box)
    if float(np.median(np.asarray(crop))) < 128:  # texto claro sobre fondo oscuro
        crop = ImageOps.invert(crop)
    crop = crop.resize((crop.width * 4, crop.height * 4), Image.LANCZOS)
    try:
        with tempfile.TemporaryDirectory() as td:
            pth = Path(td) / "c.png"
            crop.save(pth)
            res = subprocess.run([config.TESSERACT_CMD, str(pth), "stdout", "-l", config.OCR_LANG, "--psm", "7"],
                                 capture_output=True, text=True, timeout=10)
        return res.stdout.strip()
    except Exception:
        return ""


def parse_date(s: str) -> Optional[tuple[int, int, int]]:
    m = DATE_RE.search(s or "")
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return y, mo, d
