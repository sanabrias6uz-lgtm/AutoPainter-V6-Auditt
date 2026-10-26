# -*- coding: utf-8 -*-
"""
AutoPainter V5 para MediBang Paint  (no modifica ni sustituye a la V4)

Estrategia en dos etapas:
  Etapa 1 (capa base) : pincel grande, rejilla gruesa -> cubre TODA la foto, nunca queda blanco.
  Etapa 2 (detalle)   : pincel fino, rejilla fina    -> solo repinta las celdas cuyo color
                        actual (el de la base) se aleja del color objetivo.
Todos los trazos son horizontales, de altura constante, sin desplazamiento aleatorio.
Los tramos de una sola celda se dibujan.
"""
import json, os, sys, time, math, argparse
from PIL import Image, ImageDraw, ImageFilter, UnidentifiedImageError

# =====================================================================
#  CONFIGURACIÓN  (todo se puede sobreescribir desde settings.txt con una clave "v5")
#  Ejemplo en settings.txt:   "v5": {"BRUSH_PX": 8, "MAX_MINUTES": 90}
# =====================================================================
CFG = {
    # --- rejilla / colores (settings.txt: paint_grid_width, paint_grid_height, colors_count como en V4)
    "GRID_W": 320,            # celdas horizontales. Alto = proporción del lienzo (213 para 1800x1200)
    "KMEANS": 1,              # refinado de la paleta (0 = más rápido, 1-2 = algo más fiel)
    "SHARPEN_PERCENT": 0,     # 0 = sin nitidez. 30-60 acentúa pestañas/bordes al reducir la foto
    "SMOOTH_RADIUS": 0,       # filtro de moda (0 = desactivado). Si se activa, ya no desplaza contornos
    "SKIP_WHITE": True,       # no pintar celdas blancas sobre lienzo blanco
    # --- pincel (px de LIENZO, no de pantalla)
    "BRUSH_PX": None,         # etapa 2. None = automático: diagonal de la celda (8.0 px a 320 celdas) -> un clic cubre la celda entera
    "BASE_FACTOR": 4,         # etapa 1: cada celda gruesa = 4x4 celdas finas
    "BASE_COLORS": 64,        # colores de la capa base
    "BASE_BRUSH_FACTOR": 1.25,# pincel base = 1.25 x lado de la celda gruesa
    # --- selección de celdas a repintar (distancia de color CIE-Lab dE76; ~2.3 = diferencia apenas visible)
    "ERROR_THRESHOLD": 3.0,   # una celda de la base se repinta si su error supera esto
    "MERGE_TOLERANCE": 1.5,   # un tramo puede seguir sobre una celda de color casi igual (menos trazos)
    "MAX_BRIDGE": 2,          # celdas "ya correctas" que un tramo puede cruzar
    "MAX_STROKE": 64,         # celdas por trazo como máximo (los más largos se parten)
    "OVERLAP": 1,             # celdas de solape al partir un trazo largo (mismo color)
    # --- tiempo
    "MAX_MINUTES": 120,
    "SAFETY_FACTOR": 1.25,    # margen sobre la estimación (MediBang se puede ir ralentizando)
    "PRESET": None,           # None = automático (el más fiel que quepa en MAX_MINUTES); 0,1,2... = fijo
    # --- movimiento del ratón (pantalla)
    "STEP_PX": 10,            # distancia entre puntos intermedios de un trazo
    "STEP_DELAY": 0.002,
    "DOWN_DELAY": 0.004,      # espera tras pulsar el botón
    "END_HOLD": 0.006,        # espera en el punto final antes de soltar (útil con estabilizador)
    "DOT_PX": 2,              # longitud (px de pantalla) de un trazo de una sola celda
    # --- selector de color (misma cadencia efectiva que V4, que ya funciona)
    "COLOR_OPEN_DELAY": 0.30,
    "COLOR_UI_DELAY": 0.10,
    "COLOR_CONFIRM_DELAY": 0.16,
    # --- prueba corta
    "TEST_ROWS": 14,          # filas finas de la franja de prueba
    "TEST_COLORS": 10,        # colores que se dibujan en la prueba
}

# De más fiel a más rápido. El modo automático elige el primero que quepa en MAX_MINUTES.
PRESETS = [
    dict(name="A máxima",    grid_w=None, thr=3.0,  merge=1.5),
    dict(name="B alta",      grid_w=None, thr=4.5,  merge=2.5),
    dict(name="C media",     grid_w=None, thr=6.5,  merge=3.5),
    dict(name="D rápida",    grid_w=None, thr=9.0,  merge=5.0),
    dict(name="E mínima",    grid_w=None, thr=13.0, merge=7.0),
    dict(name="F 240x160",   grid_w=240,  thr=13.0, merge=7.0),   # último recurso: otra rejilla, pincel mayor
]

# Tiempos SUPUESTOS hasta que hagas la prueba corta (la prueba los sustituye por medidos)
ASSUMED_TIMING = dict(color_s=1.00, stroke_a=0.060, stroke_b=0.00050, source="SUPUESTO (sin medir): haz la prueba corta")
TIMING_FILE = "timing_v5.json"
PREVIEW_FILE = "preview_v5.png"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
WHITE = (255, 255, 255)


# =====================================================================
#  COLOR
# =====================================================================
_lab_cache, _de_cache = {}, {}

def lab(rgb):
    v = _lab_cache.get(rgb)
    if v is None:
        def lin(c):
            c /= 255.0
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = lin(rgb[0]), lin(rgb[1]), lin(rgb[2])
        X = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
        Y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
        Z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883
        f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
        fx, fy, fz = f(X), f(Y), f(Z)
        v = _lab_cache[rgb] = (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))
    return v

def dE(a, b):
    if a == b:
        return 0.0
    k = (a, b)
    v = _de_cache.get(k)
    if v is None:
        la, lb = lab(a), lab(b)
        v = _de_cache[k] = math.sqrt((la[0]-lb[0])**2 + (la[1]-lb[1])**2 + (la[2]-lb[2])**2)
    return v

def luminance_key(rgb):
    return -lab(rgb)[0]          # más claros primero -> los oscuros (pestañas, bordes) quedan encima


# =====================================================================
#  GEOMETRÍA  (lienzo real <-> pantalla)
# =====================================================================
class Geometry:
    def __init__(self, area, canvas_res, grid_w, grid_h):
        self.left, self.top, self.width, self.height = area
        self.cw, self.ch = canvas_res                  # resolución del lienzo en MediBang (px)
        self.grid_w, self.grid_h = grid_w, grid_h
        self.cell_w, self.cell_h = self.cw / grid_w, self.ch / grid_h   # px de lienzo por celda
        self.sx, self.sy = self.width / self.cw, self.height / self.ch  # px pantalla por px lienzo

    def screen(self, x, y):
        return (self.left + round(x * self.sx), self.top + round(y * self.sy))

    def screen_len(self, x0, x1):
        return abs(x1 - x0) * self.sx


# =====================================================================
#  PREPARACIÓN DE LA IMAGEN
# =====================================================================
class Prepared:
    """Foto reducida a celdas, cuantizada, colocada dentro de la rejilla (sin deformarla)."""
    def __init__(self, geo, pw, ph, x_off, y_off, tgt, ref):
        self.geo, self.pw, self.ph, self.x_off, self.y_off = geo, pw, ph, x_off, y_off
        self.tgt = tgt      # tgt[y][x] = color objetivo (de la paleta)
        self.ref = ref      # ref[y][x] = color real de la foto reducida (para medir fidelidad)


def _smooth_mode(rows, radius):
    """Filtro de moda sin sesgo: solo cambia una celda si otro color aparece ESTRICTAMENTE más veces
    que el suyo. (El de V4 devolvía el primer vecino -arriba-izquierda- en cada empate.)"""
    if radius <= 0:
        return rows
    h, w = len(rows), len(rows[0])
    out = []
    for y in range(h):
        row = []
        for x in range(w):
            cnt = {}
            for yy in range(max(0, y - radius), min(h, y + radius + 1)):
                for xx in range(max(0, x - radius), min(w, x + radius + 1)):
                    c = rows[yy][xx]
                    cnt[c] = cnt.get(c, 0) + 1
            own = rows[y][x]
            best, bn = own, cnt[own]
            for c, n in cnt.items():
                if n > bn:
                    best, bn = c, n
            row.append(best)
        out.append(row)
    return out


def prepare(image, geo, colors_count, cfg):
    src_w, src_h = image.size
    scale = min(geo.grid_w / src_w, geo.grid_h / src_h)
    pw, ph = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    photo = image.resize((pw, ph), Image.Resampling.LANCZOS)
    ref_img = photo
    if cfg["SHARPEN_PERCENT"] > 0:
        photo = photo.filter(ImageFilter.UnsharpMask(radius=1.0, percent=int(cfg["SHARPEN_PERCENT"]), threshold=2))
    q = photo.quantize(colors=colors_count, method=Image.Quantize.MEDIANCUT, kmeans=int(cfg["KMEANS"]))
    pal, px = q.getpalette(), q.load()
    tgt = [[tuple(pal[px[x, y] * 3: px[x, y] * 3 + 3]) for x in range(pw)] for y in range(ph)]
    tgt = _smooth_mode(tgt, int(cfg["SMOOTH_RADIUS"]))
    rp = ref_img.load()
    ref = [[rp[x, y] for x in range(pw)] for y in range(ph)]
    return Prepared(geo, pw, ph, (geo.grid_w - pw) // 2, (geo.grid_h - ph) // 2, tgt, ref)


# =====================================================================
#  PLANIFICACIÓN
#  Un trazo = (x0, x1, y) en px de LIENZO: horizontal, desde el centro de la primera celda
#  hasta el centro de la última. y es SIEMPRE el centro de la fila (sin desplazamientos).
# =====================================================================
def _cover(current, prep, color, x0, x1, y, radius, margin=1.0):
    """Marca en el modelo las celdas que el pincel cubre ENTERAS: sus 4 esquinas deben quedar dentro del
    trazo (con un margen para el borde suavizado). Comprobar solo el centro dejaba esquinas sin pintar."""
    geo = prep.geo
    r = radius - margin
    if r <= 0:
        return
    hw, hh = geo.cell_w / 2, geo.cell_h / 2
    gx0 = max(0, int(math.floor((x0 - r) / geo.cell_w - 0.5)) - prep.x_off)
    gx1 = min(prep.pw - 1, int(math.ceil((x1 + r) / geo.cell_w - 0.5)) - prep.x_off)
    gy0 = max(0, int(math.floor((y - r) / geo.cell_h - 0.5)) - prep.y_off)
    gy1 = min(prep.ph - 1, int(math.ceil((y + r) / geo.cell_h - 0.5)) - prep.y_off)
    r2 = r * r
    for cy in range(gy0, gy1 + 1):
        ycen = (cy + prep.y_off + 0.5) * geo.cell_h
        dyf = max(abs(ycen - hh - y), abs(ycen + hh - y))          # esquina más lejana en vertical
        if dyf * dyf > r2:
            continue
        row = current[cy]
        for cx in range(gx0, gx1 + 1):
            xcen = (cx + prep.x_off + 0.5) * geo.cell_w
            dxf = max(max(x0 - (xcen - hw), 0.0, (xcen - hw) - x1),
                      max(x0 - (xcen + hw), 0.0, (xcen + hw) - x1))  # esquina más lejana en horizontal
            if dxf * dxf + dyf * dyf <= r2:
                row[cx] = color


def plan_base(prep, cfg):
    geo, F = prep.geo, max(1, int(cfg["BASE_FACTOR"]))
    bw, bh = -(-prep.pw // F), -(-prep.ph // F)
    avg = Image.new("RGB", (bw, bh))
    cells = []
    for by in range(bh):
        for bx in range(bw):
            sr = sg = sb = n = 0
            for y in range(by * F, min(prep.ph, (by + 1) * F)):
                for x in range(bx * F, min(prep.pw, (bx + 1) * F)):
                    p = prep.ref[y][x]; sr += p[0]; sg += p[1]; sb += p[2]; n += 1
            cells.append((round(sr / n), round(sg / n), round(sb / n)))
    avg.putdata(cells)
    q = avg.quantize(colors=int(cfg["BASE_COLORS"]), method=Image.Quantize.MEDIANCUT, kmeans=int(cfg["KMEANS"]))
    pal, px = q.getpalette(), q.load()
    bcol = [[tuple(pal[px[x, y] * 3: px[x, y] * 3 + 3]) for x in range(bw)] for y in range(bh)]

    brush = F * (geo.cell_w + geo.cell_h) / 2 * cfg["BASE_BRUSH_FACTOR"]
    groups = {}
    for by in range(bh):
        yc = (prep.y_off + by * F + min(F, prep.ph - by * F) / 2) * geo.cell_h
        bx = 0
        while bx < bw:
            c, e = bcol[by][bx], bx
            while e + 1 < bw and bcol[by][e + 1] == c:
                e += 1
            def xc(b):
                return (prep.x_off + b * F + min(F, prep.pw - b * F) / 2) * geo.cell_w
            groups.setdefault(c, []).append((xc(bx), xc(e), yc))
            bx = e + 1
    ordered = sorted(groups.items(), key=lambda kv: luminance_key(kv[0]))
    current = [[WHITE] * prep.pw for _ in range(prep.ph)]
    for c, strokes in ordered:
        for (x0, x1, y) in strokes:
            _cover(current, prep, c, x0, x1, y, brush / 2)
    return dict(brush=brush, groups=ordered), current


def detail_brush(cfg, geo):
    """Pincel de la etapa 2 en px de lienzo. Automático = diagonal de la celda redondeada hacia arriba."""
    auto = math.ceil(math.hypot(geo.cell_w, geo.cell_h) * 10) / 10
    return auto if cfg["BRUSH_PX"] is None else float(cfg["BRUSH_PX"])


def plan_detail(prep, current, cfg, thr=None, merge=None):
    """Repinta solo lo que la base no resolvió. Devuelve grupos por color y el estado final por celda."""
    thr = cfg["ERROR_THRESHOLD"] if thr is None else thr
    merge = cfg["MERGE_TOLERANCE"] if merge is None else merge
    geo = prep.geo
    final = [row[:] for row in current]
    groups = {}
    skip_white = cfg["SKIP_WHITE"]
    max_len, ov = max(2, int(cfg["MAX_STROKE"])), max(0, int(cfg["OVERLAP"]))
    for y in range(prep.ph):
        trow, crow = prep.tgt[y], current[y]
        need = [dE(crow[x], trow[x]) > thr or (not skip_white and trow[x] == WHITE) for x in range(prep.pw)]
        yc = (prep.y_off + y + 0.5) * geo.cell_h
        x = 0
        while x < prep.pw:
            if not need[x]:
                x += 1
                continue
            c, start, last, gap = trow[x], x, x, 0
            x += 1
            while x < prep.pw:
                if dE(trow[x], c) > merge:
                    break
                if need[x]:
                    last, gap = x, 0
                elif gap < cfg["MAX_BRIDGE"]:
                    gap += 1
                else:
                    break
                x += 1
            x = last + 1
            for cx in range(start, last + 1):
                final[y][cx] = c
            a = start                                   # partir trazos largos (mismo color, solape fijo)
            while True:
                b = min(last, a + max_len - 1)
                groups.setdefault(c, []).append(
                    ((prep.x_off + a + 0.5) * geo.cell_w, (prep.x_off + b + 0.5) * geo.cell_w, yc))
                if b >= last:
                    break
                a = max(a + 1, b - ov + 1)
    ordered = sorted(groups.items(), key=lambda kv: luminance_key(kv[0]))
    return dict(brush=detail_brush(cfg, geo), groups=ordered), final


def quality(prep, final):
    """Fidelidad a nivel de celda frente a la foto reducida (incluye el error de la paleta)."""
    errs = [dE(final[y][x], prep.ref[y][x]) for y in range(prep.ph) for x in range(prep.pw)]
    errs.sort()
    n = len(errs)
    return dict(mean=sum(errs) / n, p95=errs[int(n * 0.95)], over10=sum(1 for e in errs if e > 10) / n * 100)


def count(stage):
    return sum(len(s) for _, s in stage["groups"]), len(stage["groups"])


class Plan:
    def __init__(self, prep, base, detail, final, preset):
        self.prep, self.base, self.detail, self.final, self.preset = prep, base, detail, final, preset
        self.geo = prep.geo
        self.nb, self.cb = count(base)
        self.nd, self.cd = count(detail)
        self.screen_len_total = sum(self.geo.screen_len(x0, x1) for st in (base, detail)
                                    for _, ss in st["groups"] for (x0, x1, _) in ss)
        self.strokes_lengths = [self.geo.screen_len(x0, x1) for st in (base, detail)
                                for _, ss in st["groups"] for (x0, x1, _) in ss]

    @property
    def strokes(self):
        return self.nb + self.nd

    @property
    def color_changes(self):
        return self.cb + self.cd


def make_plan(prep, cfg, preset):
    base, current = plan_base(prep, cfg)
    detail, final = plan_detail(prep, current, cfg, preset["thr"], preset["merge"])
    return Plan(prep, base, detail, final, preset)


# =====================================================================
#  TIEMPO
# =====================================================================
def estimate_seconds(plan, timing):
    return plan.color_changes * timing["color_s"] + sum(timing["stroke_a"] + timing["stroke_b"] * L
                                                       for L in plan.strokes_lengths)

def fmt_t(sec):
    sec = int(round(sec))
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m {sec % 60:02d}s"

def load_timing():
    try:
        with open(TIMING_FILE, "r", encoding="utf-8") as f:
            t = json.load(f)
        if all(k in t for k in ("color_s", "stroke_a", "stroke_b")):
            return t
    except (OSError, ValueError):
        pass
    return dict(ASSUMED_TIMING)

def fit_stroke_model(samples):
    """Mínimos cuadrados t = a + b*L con (L px de pantalla, t s)."""
    n = len(samples)
    mean_t = sum(t for _, t in samples) / n
    mean_l = sum(l for l, _ in samples) / n
    var = sum((l - mean_l) ** 2 for l, _ in samples)
    if n < 20 or var < 1e-9:
        return mean_t, ASSUMED_TIMING["stroke_b"] if var < 1e-9 else 0.0
    b = sum((l - mean_l) * (t - mean_t) for l, t in samples) / var
    b = max(0.0, b)
    return max(0.0, mean_t - b * mean_l), b


# =====================================================================
#  VISTA PREVIA  (solo Pillow; no toca el ratón)
# =====================================================================
def render_preview(plan, ss=2):
    """Simulación aproximada: pincel redondo duro con borde suavizado por sobremuestreo. Las posiciones se
    ajustan al píxel de PANTALLA entero (el ratón solo puede ir a píxeles enteros)."""
    geo = plan.geo
    snap_x = lambda v: round(v * geo.sx) / geo.sx
    snap_y = lambda v: round(v * geo.sy) / geo.sy
    img = Image.new("RGB", (geo.cw * ss, geo.ch * ss), "white")
    d = ImageDraw.Draw(img)
    for stage in (plan.base, plan.detail):
        w = max(1, round(stage["brush"] * ss)); r = w / 2
        for color, strokes in stage["groups"]:
            for (x0, x1, y) in strokes:
                X0, X1, Y = snap_x(x0) * ss, snap_x(x1) * ss, snap_y(y) * ss
                d.line([(X0, Y), (X1, Y)], fill=color, width=w)
                d.ellipse([X0 - r, Y - r, X0 + r, Y + r], fill=color)
                d.ellipse([X1 - r, Y - r, X1 + r, Y + r], fill=color)
    return img.resize((geo.cw, geo.ch), Image.Resampling.LANCZOS)


# =====================================================================
#  CONTROL DEL RATÓN
# =====================================================================
class Driver:
    def __init__(self, geo, cfg, picker, color_input, confirm):
        import pyautogui, keyboard          # import tardío: el planificador se puede probar sin ellos
        pyautogui.PAUSE = 0                 # V4 dejaba 0,1 s tras CADA llamada (~ 2,7 h solo en pausas)
        self.pg, self.kb, self.geo, self.cfg = pyautogui, keyboard, geo, cfg
        self.picker, self.color_input, self.confirm = picker, color_input, confirm

    def esc(self):
        return self.kb.is_pressed("esc")

    def select_color(self, rgb):
        pg, c = self.pg, self.cfg
        pg.click(*self.picker);       time.sleep(c["COLOR_OPEN_DELAY"])
        pg.click(*self.color_input);  time.sleep(c["COLOR_UI_DELAY"])
        pg.hotkey("ctrl", "a");       time.sleep(c["COLOR_UI_DELAY"])
        pg.write("#{0:02X}{1:02X}{2:02X}".format(*rgb), interval=.003)
        time.sleep(c["COLOR_UI_DELAY"])
        pg.click(*self.confirm);      time.sleep(c["COLOR_CONFIRM_DELAY"])

    def stroke(self, x0, x1, y):
        pg, c, geo = self.pg, self.cfg, self.geo
        (sx0, sy), (sx1, _) = geo.screen(x0, y), geo.screen(x1, y)
        if sx1 - sx0 < 1:                       # tramo de UNA celda: segmento corto, nunca se omite
            sx0, sx1 = sx0 - c["DOT_PX"] // 2, sx0 + (c["DOT_PX"] + 1) // 2
        if self.esc():
            raise KeyboardInterrupt
        pg.moveTo(sx0, sy)
        pg.mouseDown()
        try:
            time.sleep(c["DOWN_DELAY"])
            n = max(1, math.ceil((sx1 - sx0) / c["STEP_PX"]))
            for i in range(1, n + 1):
                pg.moveTo(sx0 + round((sx1 - sx0) * i / n), sy)
                time.sleep(c["STEP_DELAY"])
                if i % 8 == 0 and self.esc():
                    raise KeyboardInterrupt
            time.sleep(c["END_HOLD"])
        finally:
            pg.mouseUp()

    def release(self):
        try:
            self.pg.mouseUp()
        except Exception:
            pass


def countdown(n):
    for s in range(n, 0, -1):
        print(s, end=" ", flush=True); time.sleep(1)
    print()


def run_stage(drv, stage, label, total_all, state, t0):
    for color, strokes in stage["groups"]:
        if drv.esc():
            raise KeyboardInterrupt
        drv.select_color(color)
        for (x0, x1, y) in strokes:
            drv.stroke(x0, x1, y)
            state["done"] += 1
            now = time.time()
            if state["done"] % 250 == 0 and now - state.get("last", 0) >= 20:
                state["last"] = now
                el = now - t0
                left = el / state["done"] * (total_all - state["done"])
                print(f"  {label}: {state['done']}/{total_all} trazos | transcurrido {fmt_t(el)} | restante ~ {fmt_t(left)}")


def run_full(drv, plan, cfg):
    print(f"\nPincel etapa 1 (base): {plan.base['brush']:.1f} px   |   pincel etapa 2 (detalle): {plan.detail['brush']:.1f} px")
    print("En MediBang: pincel redondo, dureza 100 %, opacidad 100 %, estabilizador al mínimo, lienzo visible entero")
    print("y en la misma posición que cuando hiciste el setup. Pulsa ESC para detener.")
    input("Pon el pincel de la ETAPA 1 y pulsa ENTER cuando MediBang esté listo... ")
    countdown(5)
    state, total, t0 = {"done": 0}, plan.strokes, time.time()
    try:
        print(f"ETAPA 1/2 (base): {plan.nb} trazos, {plan.cb} colores")
        run_stage(drv, plan.base, "base", total, state, t0)
        print(f"Etapa 1 terminada en {fmt_t(time.time() - t0)}.")
        input(f"\nCambia el pincel a {plan.detail['brush']:.1f} px (ETAPA 2) y pulsa ENTER... ")
        countdown(5)
        print(f"ETAPA 2/2 (detalle): {plan.nd} trazos, {plan.cd} colores")
        run_stage(drv, plan.detail, "detalle", total, state, t0)
        print(f"Terminado. Trazos dibujados: {state['done']} en {fmt_t(time.time() - t0)}")
    except KeyboardInterrupt:
        print("Proceso detenido con ESC.")
    except Exception as e:                                   # p. ej. FailSafe de pyautogui
        print("Proceso detenido:", repr(e))
    finally:
        drv.release()


def run_test(drv, plan, cfg):
    """Dibuja una franja con el pincel de detalle, mide tiempos reales y los guarda."""
    prep, geo = plan.prep, plan.geo
    r0 = max(0, min(prep.ph - cfg["TEST_ROWS"], int(prep.ph * 0.38)))      # zona de los ojos/pelo
    r1 = r0 + cfg["TEST_ROWS"]
    # Franja pintada como sobre lienzo vacío: todas las celdas no blancas necesitan trazo
    blank = [[WHITE] * prep.pw for _ in range(prep.ph)]
    band = Prepared(geo, prep.pw, prep.ph, prep.x_off, prep.y_off, prep.tgt, prep.ref)
    det, _ = plan_detail(band, blank, cfg, thr=0.5, merge=plan.preset["merge"])
    ylo, yhi = (prep.y_off + r0) * geo.cell_h, (prep.y_off + r1) * geo.cell_h
    groups = []
    for c, ss in det["groups"]:
        ss = [s for s in ss if ylo <= s[2] < yhi]
        if ss:
            groups.append((c, ss))
    groups.sort(key=lambda g: -len(g[1]))
    groups = groups[:cfg["TEST_COLORS"]]
    groups.sort(key=lambda g: luminance_key(g[0]))
    n_strokes = sum(len(s) for _, s in groups)
    print(f"\nPRUEBA CORTA: {len(groups)} colores, {n_strokes} trazos, filas {r0}-{r1} de la foto")
    print(f"Pon el pincel de detalle ({det['brush']:.1f} px). Se dibujará una franja; luego bórrala o deshazla (Ctrl+Z).")
    input("Pulsa ENTER cuando MediBang esté listo... ")
    countdown(5)
    color_times, samples = [], []
    try:
        for c, ss in groups:
            t = time.perf_counter(); drv.select_color(c); color_times.append(time.perf_counter() - t)
            for (x0, x1, y) in ss:
                t = time.perf_counter(); drv.stroke(x0, x1, y)
                samples.append((geo.screen_len(x0, x1), time.perf_counter() - t))
    except KeyboardInterrupt:
        print("Prueba detenida con ESC (no se guardan tiempos).")
        return None
    except Exception as e:
        print("Prueba detenida:", repr(e)); return None
    finally:
        drv.release()
    a, b = fit_stroke_model(samples)
    timing = dict(color_s=sum(color_times) / len(color_times), stroke_a=a, stroke_b=b,
                  source=f"medido ({len(samples)} trazos, {len(color_times)} cambios de color, {time.strftime('%Y-%m-%d %H:%M')})")
    with open(TIMING_FILE, "w", encoding="utf-8") as f:
        json.dump(timing, f, indent=1)
    print(f"Medido: cambio de color {timing['color_s']:.2f} s | trazo ~ {a*1000:.1f} ms + {b*1000:.3f} ms/px "
          f"(trazo medio {sum(t for _, t in samples)/len(samples)*1000:.0f} ms)")
    return timing


# =====================================================================
#  AUTO-AJUSTE Y TABLA
# =====================================================================
class Factory:
    def __init__(self, image, area, canvas_res, colors_count, cfg):
        self.image, self.area, self.canvas_res, self.colors, self.cfg = image, area, canvas_res, colors_count, cfg
        self._prep = {}

    def geo(self, grid_w):
        gh = round(grid_w * self.canvas_res[1] / self.canvas_res[0])
        return Geometry(self.area, self.canvas_res, grid_w, gh)

    def prep(self, grid_w):
        if grid_w not in self._prep:
            self._prep[grid_w] = prepare(self.image, self.geo(grid_w), self.colors, self.cfg)
        return self._prep[grid_w]

    def plan(self, preset):
        return make_plan(self.prep(preset["grid_w"] or self.cfg["GRID_W"]), self.cfg, preset)


def choose_plan(factory, timing, cfg, verbose=True):
    idx = cfg["PRESET"]
    order = [idx] if idx is not None else range(len(PRESETS))
    limit = cfg["MAX_MINUTES"] * 60
    best = None
    for i in order:
        p = factory.plan(PRESETS[i])
        est = estimate_seconds(p, timing) * cfg["SAFETY_FACTOR"]
        if verbose:
            print(f"  preset {PRESETS[i]['name']:<10} {p.strokes:>6} trazos, {p.color_changes:>3} colores -> ~ {fmt_t(est)} (con margen)")
        best = p
        if est <= limit:
            return p, est, True
    return best, est, False


def print_table(factory, timing, cfg):
    print(f"\nTiempos: {timing['source']}  (color {timing['color_s']:.2f}s, trazo {timing['stroke_a']*1000:.1f}ms + {timing['stroke_b']*1000:.3f}ms/px)")
    print(f"{'preset':<11}{'rejilla':>9}{'pincel':>7}{'trazos':>8}{'colores':>8}{'tiempo*':>10}{'error medio':>13}{'p95':>7}{'celdas>10':>11}")
    for pr in PRESETS:
        p = factory.plan(pr); q = quality(p.prep, p.final)
        g = p.geo
        print(f"{pr['name']:<11}{g.grid_w:>5}x{g.grid_h:<3}{p.detail['brush']:>7.1f}{p.strokes:>8}{p.color_changes:>8}"
              f"{fmt_t(estimate_seconds(p, timing) * cfg['SAFETY_FACTOR']):>10}{q['mean']:>12.2f} {q['p95']:>7.1f}{q['over10']:>10.1f}%")
    print("* tiempo con margen de seguridad. Error = distancia de color dE76 media entre la foto y el plan de ESA rejilla")
    print("  (no es comparable entre rejillas distintas; para eso usa el simulador de píxeles de test_v5.py).")


# =====================================================================
#  ENTRADA / MAIN
# =====================================================================
def load_settings(path=None):
    path = path or "settings.txt"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"No se encuentra {path}. Ejecuta esto desde la carpeta del AutoPainter.")
    except ValueError as e:
        sys.exit(f"{path} no es un JSON válido: {e}")

def pick_image(explicit=None):
    if explicit:
        return explicit, Image.open(explicit).convert("RGB")
    for fn in sorted(os.listdir(".")):
        low = fn.lower()
        if low.endswith(IMAGE_EXT) and not low.startswith("preview_"):
            try:
                return fn, Image.open(fn).convert("RGB")
            except (OSError, UnidentifiedImageError):
                pass
    return None, None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--imagen", help="ruta de la imagen (por defecto la primera de la carpeta)")
    ap.add_argument("--settings", default="settings.txt")
    args = ap.parse_args()
    cfg = dict(CFG)
    settings = load_settings(args.settings)
    for k, v in settings.get("v5", {}).items():
        if k in cfg:
            cfg[k] = v
    try:
        area = settings["canvas_area"]
        area = (area["left"], area["top"], area["width"], area["height"])
        picker, cinput, confirm = (tuple(settings[k]) for k in ("color_picker_spot", "color_input_spot", "color_confirmation_spot"))
    except KeyError as e:
        sys.exit(f"settings.txt no tiene la clave {e}. Necesita canvas_area con left/top/width/height (usa medir.py).")
    canvas_res = (int(settings.get("canvas_width_resolution", 1800)), int(settings.get("canvas_height_resolution", 1200)))
    if "canvas_width_resolution" not in settings:
        print("AVISO: settings.txt no tiene canvas_width_resolution/height; se asume 1800x1200.")
    cfg["GRID_W"] = int(settings.get("paint_grid_width", cfg["GRID_W"]))
    colors = int(settings.get("colors_count", 256))

    name, image = pick_image(args.imagen)
    if image is None:
        print("No se encontró ninguna imagen compatible en esta carpeta.")
        input("Pulsa ENTER para salir."); sys.exit(1)
    print("Imagen seleccionada:", name, image.size)
    print(f"Lienzo {canvas_res[0]}x{canvas_res[1]} px | área en pantalla {area[2]}x{area[3]} | colores máx. {colors}")

    factory = Factory(image, area, canvas_res, colors, cfg)
    timing = load_timing()
    print(f"Tiempos: {timing['source']}")
    print("Planificando...")
    plan, est, fits = choose_plan(factory, timing, cfg)
    g = plan.geo
    print(f"\nPlan '{plan.preset['name']}': rejilla {g.grid_w}x{g.grid_h} (celda {g.cell_w:.2f}x{g.cell_h:.2f} px), "
          f"foto {plan.prep.pw}x{plan.prep.ph} celdas")
    print(f"  base: {plan.nb} trazos/{plan.cb} colores, pincel {plan.base['brush']:.1f} px | detalle: {plan.nd} trazos/{plan.cd} colores, pincel {plan.detail['brush']:.1f} px")
    print(f"  estimación: {fmt_t(est)} con margen ({timing['source']})" + ("" if fits else f"  ¡SUPERA {cfg['MAX_MINUTES']} min!"))
    diag = math.hypot(g.cell_w, g.cell_h)
    if plan.detail["brush"] < diag - 0.05:
        print(f"AVISO: pincel de detalle {plan.detail['brush']} px < diagonal de celda {diag:.1f} px: pueden quedar esquinas sin cubrir.")
    render_preview(plan).save(PREVIEW_FILE)
    print(f"  vista previa simulada guardada en {PREVIEW_FILE}")

    while True:
        print("\n1) Prueba corta (dibuja una franja y MIDE los tiempos reales)\n2) Dibujo completo\n3) Tabla de configuraciones (no toca el ratón)\n0) Salir")
        op = input("Opción: ").strip()
        if op == "0":
            return
        if op == "3":
            print_table(factory, timing, cfg)
        elif op in ("1", "2"):
            drv = Driver(g, cfg, picker, cinput, confirm)
            if op == "1":
                t = run_test(drv, plan, cfg)
                if t:
                    timing = t
                    print("Replanificando con los tiempos medidos...")
                    plan, est, fits = choose_plan(factory, timing, cfg)
                    g = plan.geo
                    print(f"Plan '{plan.preset['name']}': ~ {fmt_t(est)} con margen" + ("" if fits else f"  ¡SUPERA {cfg['MAX_MINUTES']} min!"))
                    render_preview(plan).save(PREVIEW_FILE)
            else:
                if not fits:
                    if input(f"La estimación supera {cfg['MAX_MINUTES']} min. ¿Continuar igualmente? (s/N): ").strip().lower() != "s":
                        continue
                run_full(drv, plan, cfg)
                return

if __name__ == "__main__":
    main()
