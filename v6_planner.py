# -*- coding: utf-8 -*-
"""
AutoPainter V6 - planificador de pinceladas (sin ratón; solo numpy + Pillow).

Idea (basada en la pintura "curved brush strokes" de Hertzmann, adaptada a este proyecto):
  * Hay 6 ETAPAS, cada una con su capa. Cada etapa tiene 1..n pasadas con pinceles de tamaño decreciente.
  * Cada pasada mide el ERROR entre la foto (desenfocada según el pincel) y un lienzo SIMULADO que ya
    contiene lo pintado hasta ese momento. Solo se pinta donde el error supera un umbral -> cada
    pincelada tiene un propósito: corregir un error visible.
  * El trazo nace en el punto de mayor error de su celda, toma el color de la foto en ese punto
    (ajustado a una paleta pequeña de la etapa) y avanza siguiendo el campo de orientación de la imagen
    (tensor de estructura: pelo, pliegues, contornos), hasta que el color de la foto deja de parecerse.
  * Donde la imagen no tiene dirección propia, la dirección sale de un campo suave con semilla.
  * La importancia visual (ojos, piel, contraste) baja el umbral: más trazos, más finos y más fieles.
Reutiliza de V5: lab/dE (color), Geometry (lienzo<->pantalla), fit_stroke_model (tiempos), PRESETS no.
"""
import math, json, gzip, hashlib, random
import numpy as np
from PIL import Image, ImageDraw
import run_painter_strokes_v5 as v5
import v6_analysis as an

WHITE = (255, 255, 255)

# ---------------------------------------------------------------------------------------------
#  Definición de etapas.  sizes = pinceles en px de LIENZO (de grande a pequeño). thr = umbral de
#  error medio por celda (dE76; ~2.3 es apenas visible). tol = cuánto puede desviarse el color de la
#  foto a lo largo de un trazo. follow = cuánto manda la orientación de la imagen frente al campo suave.
# ---------------------------------------------------------------------------------------------
STAGES = [
    dict(key="composicion", layer="01 Composicion", sizes=[72],   palette=12, thr=14.0, blur=0.50, tol=22, maxlen=40, minlen=3, fc=0.35, follow=0.25, lw=1.0, mask=None,  cap=None),
    dict(key="bloqueo",     layer="02 Bloqueo",     sizes=[36, 22], palette=28, thr=10.0, blur=0.40, tol=15, maxlen=32, minlen=2, fc=0.45, follow=0.55, lw=1.0, mask=None,  cap=None),
    dict(key="volumen",     layer="03 Volumen",     sizes=[20, 14], palette=40, thr=7.0,  blur=0.35, tol=10, maxlen=26, minlen=2, fc=0.55, follow=0.80, lw=1.6, mask=None,  cap=None),
    dict(key="textura",     layer="04 Textura",     sizes=[12, 8],  palette=64, thr=6.0,  blur=0.30, tol=9,  maxlen=22, minlen=2, fc=0.60, follow=0.95, lw=1.0, mask=None,  cap=None),
    dict(key="detalles",    layer="05 Detalles",    sizes=[5, 3.5], palette=96, thr=7.5,  blur=0.35, tol=9,  maxlen=18, minlen=2, fc=0.85, follow=1.00, lw=1.0, mask="detail", cap=None),
    dict(key="acabado",     layer="06 Acabado",     sizes=[2.5],    palette=24, thr=5.0,  blur=0.0,  tol=8,  maxlen=6,  minlen=1, fc=0.90, follow=1.00, lw=2.2, mask="focus",  cap=450),
]

# De más fiel a más rápido: multiplicador del umbral de cada etapa y del tope de trazos.
PRESETS6 = [
    dict(name="S densa",       thr_mul=0.55, min_stage_mul=1.0),
    dict(name="A+ fidelidad", thr_mul=0.75, min_stage_mul=1.0),
    dict(name="A maxima", thr_mul=1.00, min_stage_mul=1.0),
    dict(name="B alta",   thr_mul=1.30, min_stage_mul=1.0),
    dict(name="C media",  thr_mul=1.70, min_stage_mul=1.0),
    dict(name="D rapida", thr_mul=2.30, min_stage_mul=1.0),
    dict(name="E minima", thr_mul=3.20, min_stage_mul=1.0),
]


# ---------------------------------------------------------------------------------------------
#  Color vectorizado (misma fórmula que V5.lab)
# ---------------------------------------------------------------------------------------------
_M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]], np.float32)
_WP = np.array([0.95047, 1.0, 1.08883], np.float32)


def rgb2lab(a):
    c = np.asarray(a, np.float32) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = (lin @ _M.T) / _WP
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1).astype(np.float32)


# ---------------------------------------------------------------------------------------------
#  Escena: foto colocada en el lienzo, mapas de análisis a resolución de trabajo
# ---------------------------------------------------------------------------------------------
class Scene:
    def __init__(self, image, canvas_res, work_scale=0.5, focus_boxes=None, seed=0):
        self.cw, self.ch = canvas_res
        self.ws = work_scale
        sc = min(self.cw / image.width, self.ch / image.height)
        self.rw, self.rh = round(image.width * sc), round(image.height * sc)       # foto en px de lienzo
        self.ox, self.oy = (self.cw - self.rw) // 2, (self.ch - self.rh) // 2
        self.W, self.H = round(self.cw * self.ws), round(self.ch * self.ws)       # lienzo de trabajo
        pw, ph = round(self.rw * self.ws), round(self.rh * self.ws)
        self.pw, self.ph = pw, ph
        self.wox, self.woy = round(self.ox * self.ws), round(self.oy * self.ws)
        photo = image.convert("RGB").resize((pw, ph), Image.Resampling.LANCZOS)
        full = np.full((self.H, self.W, 3), 255, np.float32)
        full[self.woy:self.woy + ph, self.wox:self.wox + pw] = np.asarray(photo, np.float32)
        self.photo = full
        self.mask = np.zeros((self.H, self.W), bool)
        self.mask[self.woy:self.woy + ph, self.wox:self.wox + pw] = True
        A = an.analyze(photo, focus_boxes)
        self.maps = {}
        for k in ("lum", "edge", "coherence", "importance", "skin", "dark", "eye_like", "theta"):
            m = np.zeros((self.H, self.W), np.float32)
            m[self.woy:self.woy + ph, self.wox:self.wox + pw] = A[k]
            self.maps[k] = m
        self.seed = seed

    def to_canvas(self, x, y):
        return x / self.ws, y / self.ws


# ---------------------------------------------------------------------------------------------
#  Estructura del plan
# ---------------------------------------------------------------------------------------------
class Group:
    """Trazos del mismo tamaño y color (se ejecutan seguidos: un solo cambio de color/tamaño)."""
    __slots__ = ("size", "color", "strokes")

    def __init__(self, size, color, strokes):
        self.size, self.color, self.strokes = size, tuple(int(c) for c in color), strokes   # strokes: list[list[(x,y)]] px lienzo


class StagePlan:
    def __init__(self, key, layer, groups):
        self.key, self.layer, self.groups = key, layer, groups

    @property
    def n_strokes(self):
        return sum(len(g.strokes) for g in self.groups)

    @property
    def n_color_changes(self):
        return len(self.groups)

    @property
    def sizes(self):
        return sorted({g.size for g in self.groups}, reverse=True)


class Plan6:
    def __init__(self, stages, meta):
        self.stages, self.meta = stages, meta

    def flat(self):
        """Orden de ejecución lineal: (indice_global, etapa, size, color, pts). El índice es el identificador
        estable que usan los checkpoints."""
        i = 0
        for si, st in enumerate(self.stages):
            for g in st.groups:
                for pts in g.strokes:
                    yield i, si, g.size, g.color, pts
                    i += 1

    @property
    def n_strokes(self):
        return sum(s.n_strokes for s in self.stages)

    @property
    def n_color_changes(self):
        return sum(s.n_color_changes for s in self.stages)

    def digest(self):
        h = hashlib.sha256()
        for i, si, size, color, pts in self.flat():
            h.update(f"{si}|{size}|{color}|{[(round(x, 1), round(y, 1)) for x, y in pts]}".encode())
        return h.hexdigest()[:16]

    # --- (de)serialización compacta para los checkpoints
    def to_json(self):
        return dict(meta=self.meta, stages=[dict(key=s.key, layer=s.layer, groups=[
            dict(size=g.size, color=list(g.color), strokes=[[[round(x, 1), round(y, 1)] for x, y in p] for p in g.strokes])
            for g in s.groups]) for s in self.stages])

    @staticmethod
    def from_json(d):
        return Plan6([StagePlan(s["key"], s["layer"], [Group(g["size"], g["color"], [[tuple(p) for p in st] for st in g["strokes"]])
                                                      for g in s["groups"]]) for s in d["stages"]], d["meta"])

    def save(self, path):
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(self.to_json(), f, separators=(",", ":"))

    @staticmethod
    def load(path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return Plan6.from_json(json.load(f))


# ---------------------------------------------------------------------------------------------
#  Campos auxiliares
# ---------------------------------------------------------------------------------------------
def smooth_noise_angle(shape, sigma, rng):
    """Campo suave de ángulos (en 'eje' = doble ángulo, sin orientación privilegiada)."""
    r = np.random.default_rng(rng.randrange(1 << 30))
    a = r.standard_normal(shape).astype(np.float32)
    b = r.standard_normal(shape).astype(np.float32)
    a, b = an.gaussian(a, sigma), an.gaussian(b, sigma)
    return 0.5 * np.arctan2(b, a)                      # ángulo de eje en (-pi/2, pi/2]


def direction_field(scene, follow, size_work, rng):
    """Vector director por celda. Mezcla orientación de la imagen y campo suave; doble ángulo para no
    mezclar sentidos opuestos."""
    th, coh = scene.maps["theta"], scene.maps["coherence"]
    phi = smooth_noise_angle(th.shape, max(4.0, size_work * 2.0), rng)
    # base suave: leve inclinación diagonal descendente (mano diestra) + ruido, para evitar rejillas horizontales
    phi = phi * 0.8 - 0.35
    wt = np.clip((coh ** 0.7) * follow * 1.4, 0, 1)
    sx = wt * np.cos(2 * th) + (1 - wt) * np.cos(2 * phi)
    sy = wt * np.sin(2 * th) + (1 - wt) * np.sin(2 * phi)
    psi = 0.5 * np.arctan2(sy, sx)
    return np.cos(psi).astype(np.float32), np.sin(psi).astype(np.float32)


def stage_mask(scene, kind):
    imp, edge = scene.maps["importance"], scene.maps["edge"]
    if kind is None:
        return scene.mask.copy()
    if kind == "detail":
        return scene.mask & ((imp > 0.25) | (edge > 0.35))
    if kind == "focus":
        return scene.mask & (imp > 0.30)
    raise ValueError(kind)


# ---------------------------------------------------------------------------------------------
#  Una pasada
# ---------------------------------------------------------------------------------------------
def _palette(ref, mask, n):
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return np.array([[255, 255, 255]], np.float32)
    # imagen compacta con los píxeles de la máscara (muestreo para ir deprisa)
    idx = np.arange(0, len(ys), max(1, len(ys) // 20000))
    pix = ref[ys[idx], xs[idx]].astype(np.uint8)
    side = int(math.ceil(math.sqrt(len(pix))))
    pad = np.zeros((side * side, 3), np.uint8)
    pad[:len(pix)] = pix
    pad[len(pix):] = pix[-1]
    im = Image.fromarray(pad.reshape(side, side, 3), "RGB")
    q = im.quantize(colors=int(n), method=Image.Quantize.MEDIANCUT, kmeans=1)
    pal = np.array(q.getpalette()[:3 * int(n)], np.float32).reshape(-1, 3)
    used = np.unique(np.asarray(q))
    return pal[[u for u in used if u < len(pal)]]


def _blur_rgb(arr, sigma):
    return np.stack([an.gaussian(arr[..., c], sigma) for c in range(3)], -1) if sigma > 0 else arr.copy()


def run_pass(scene, canvas, size, st, thr, rng, stats, cap=None):
    """Genera los trazos de UNA pasada. canvas: array float32 HxWx3 (lienzo simulado actual)."""
    ws = scene.ws
    r = max(0.6, size * ws / 2.0)                        # radio del pincel en px de trabajo
    sigma = st["blur"] * size * ws
    ref = _blur_rgb(scene.photo, sigma)
    ref[~scene.mask] = 255
    mask = stage_mask(scene, st["mask"])
    pal = _palette(ref, scene.mask, st["palette"])
    pal_lab = rgb2lab(pal)
    ref_lab, can_lab = rgb2lab(ref), rgb2lab(canvas)
    d = ref_lab - can_lab
    lw = st["lw"]
    D = np.sqrt((lw * d[..., 0]) ** 2 + d[..., 1] ** 2 + d[..., 2] ** 2)
    D[~mask] = 0
    # umbral por píxel: más exigente donde importa
    imp = scene.maps["importance"]
    thr_map = thr * (1.35 - 0.6 * imp)
    dx, dy = direction_field(scene, st["follow"], r * 2, rng)

    g = max(2, int(round(r * 2.0)))                      # celda = diámetro del pincel
    H, W = D.shape
    gh, gw = -(-H // g), -(-W // g)
    seeds = []
    for cy in range(gh):
        y0, y1 = cy * g, min(H, (cy + 1) * g)
        for cx in range(gw):
            x0, x1 = cx * g, min(W, (cx + 1) * g)
            blk = D[y0:y1, x0:x1]
            if blk.size == 0:
                continue
            m = float(blk.mean() * blk.size / max(1, mask[y0:y1, x0:x1].sum()))   # media sobre píxeles válidos
            t = float(thr_map[y0:y1, x0:x1].mean())
            if m > t:
                iy, ix = np.unravel_index(int(np.argmax(blk)), blk.shape)
                seeds.append((m / t, x0 + ix, y0 + iy))
    rng.shuffle(seeds)
    if cap is not None and len(seeds) > cap:                                 # acabado: solo las más necesarias
        seeds.sort(key=lambda s: -s[0])
        seeds = seeds[:cap]

    covered = Image.new("L", (W, H), 0)
    cdraw = ImageDraw.Draw(covered)
    step = max(1.0, r * 0.8)
    strokes = []
    tol = st["tol"]
    ins = r * 0.7                                        # el pincel no se sale de la foto más de ~0.3 r
    bx0, bx1 = scene.wox + ins, scene.wox + scene.pw - 1 - ins
    by0, by1 = scene.woy + ins, scene.woy + scene.ph - 1 - ins
    for score, sx_, sy_ in seeds:
        sx_ = int(min(max(sx_, bx0), bx1)); sy_ = int(min(max(sy_, by0), by1))
        if covered.getpixel((sx_, sy_)) and cap is None:
            continue
        # color del trazo: paleta más cercana (Lab) al color de la foto en la semilla
        dl = pal_lab - ref_lab[sy_, sx_]
        ci = int(np.argmin((dl ** 2).sum(1)))
        col, col_lab = pal[ci], pal_lab[ci]
        x, y = float(sx_), float(sy_)
        pts = [(x, y)]
        prev = None
        for i in range(st["maxlen"]):
            vx, vy = float(dx[int(y), int(x)]), float(dy[int(y), int(x)])
            if prev is None:
                if vx < 0 or (abs(vx) < 1e-6 and vy < 0):
                    vx, vy = -vx, -vy
            else:
                if vx * prev[0] + vy * prev[1] < 0:
                    vx, vy = -vx, -vy
                fc = st["fc"]
                vx, vy = fc * vx + (1 - fc) * prev[0], fc * vy + (1 - fc) * prev[1]
                n = math.hypot(vx, vy) or 1.0
                vx, vy = vx / n, vy / n
            nx, ny = x + vx * step, y + vy * step
            ix, iy = int(nx), int(ny)
            if nx < bx0 or nx > bx1 or ny < by0 or ny > by1 or not mask[iy, ix]:
                break
            rl = ref_lab[iy, ix]
            dcol = float(np.sqrt(((rl - col_lab) ** 2).sum()))
            if dcol > tol and i >= st["minlen"]:
                break
            if i >= st["minlen"] and float(np.sqrt(((rl - can_lab[iy, ix]) ** 2).sum())) < dcol * 0.8:
                break
            x, y, prev = nx, ny, (vx, vy)
            pts.append((x, y))
        if len(pts) < 2:                                  # trazo de un punto: segmento corto en su dirección
            vx, vy = float(dx[sy_, sx_]), float(dy[sy_, sx_])
            if vx < 0:
                vx, vy = -vx, -vy
            pts = [(x - vx * r * 0.6, y - vy * r * 0.6), (x + vx * r * 0.6, y + vy * r * 0.6)]
            pts = [(min(max(px, 0), W - 1), min(max(py, 0), H - 1)) for px, py in pts]
        cdraw.line(pts, fill=255, width=max(1, int(round(r * 1.6))))
        strokes.append((tuple(int(round(c)) for c in col), pts))
    stats["seeds"] = stats.get("seeds", 0) + len(seeds)
    return strokes


def order_strokes(strokes):
    """Agrupa por color (de claro a oscuro: lo oscuro queda encima) y dentro de cada color recorre los
    trazos por el vecino más cercano para reducir saltos."""
    by = {}
    for col, pts in strokes:
        by.setdefault(col, []).append(pts)
    out = []
    for col in sorted(by, key=lambda c: v5.luminance_key(c)):
        lst = by[col]
        if len(lst) > 2:
            cur = min(range(len(lst)), key=lambda i: (lst[i][0][1], lst[i][0][0]))
            order, rest = [cur], set(range(len(lst))) - {cur}
            sx = np.array([p[0][0] for p in lst]); sy = np.array([p[0][1] for p in lst])
            ex = np.array([p[-1][0] for p in lst]); ey = np.array([p[-1][1] for p in lst])
            alive = np.ones(len(lst), bool); alive[cur] = False
            while alive.any():
                dd = (sx - ex[cur]) ** 2 + (sy - ey[cur]) ** 2
                dd[~alive] = np.inf
                cur = int(np.argmin(dd)); order.append(cur); alive[cur] = False
            lst = [lst[i] for i in order]
        out.append((col, lst))
    return out


def stamp(canvas_img, strokes_by_color, size, ws):
    """Pinta (en el lienzo simulado de trabajo, PIL) un grupo de trazos con pincel redondo duro."""
    d = ImageDraw.Draw(canvas_img)
    w = max(1, int(round(size * ws)))
    r = w / 2.0
    for col, lst in strokes_by_color:
        for pts in lst:
            d.line(pts, fill=col, width=w, joint="curve")
            for (x, y) in (pts[0], pts[-1]):
                d.ellipse([x - r, y - r, x + r, y + r], fill=col)


# ---------------------------------------------------------------------------------------------
#  Plan completo
# ---------------------------------------------------------------------------------------------
def make_plan6(scene, preset, seed=0, stages=STAGES, progress=None):
    rng = random.Random(seed)
    canvas_img = Image.new("RGB", (scene.W, scene.H), "white")
    out, stats = [], {}
    for si, st in enumerate(stages):
        groups = []
        thr = st["thr"] * preset["thr_mul"]
        for size in st["sizes"]:
            # umbral algo más exigente en pasadas más finas de la misma etapa
            canvas = np.asarray(canvas_img, np.float32)
            strokes = run_pass(scene, canvas, size, st, thr, rng, stats, cap=st["cap"])
            ordered = order_strokes(strokes)
            stamp(canvas_img, ordered, size, scene.ws)
            for col, lst in ordered:
                groups.append(Group(size, col, [[(round(x / scene.ws, 1), round(y / scene.ws, 1)) for x, y in pts] for pts in lst]))   # ya redondeado a 0,1 px: el plan en memoria == el guardado en disco
            if progress:
                progress(f"  etapa {st['key']:<11} pincel {size:>5} px -> {sum(len(l) for _, l in ordered):>6} trazos, {len(ordered):>3} colores")
        out.append(StagePlan(st["key"], st["layer"], groups))
    meta = dict(seed=seed, preset=preset["name"], thr_mul=preset["thr_mul"], canvas=[scene.cw, scene.ch],
                photo_rect=[scene.ox, scene.oy, scene.rw, scene.rh], work_scale=scene.ws)
    return Plan6(out, meta), canvas_img


# ---------------------------------------------------------------------------------------------
#  Simulación a resolución completa (para vistas previas y comparación)
# ---------------------------------------------------------------------------------------------
def render_plan6(plan, upto_stage=None, ss=2, geo=None):
    cw, ch = plan.meta["canvas"]
    img = Image.new("RGB", (cw * ss, ch * ss), "white")
    d = ImageDraw.Draw(img)
    for si, st in enumerate(plan.stages):
        if upto_stage is not None and si > upto_stage:
            break
        for g in st.groups:
            w = max(1, int(round(g.size * ss))); r = w / 2.0
            for pts in g.strokes:
                P = [(x * ss, y * ss) for x, y in pts]
                if geo is not None:    # ajustar al píxel de pantalla entero, como hará el ratón
                    P = [(round(x * geo.sx) / geo.sx * ss, round(y * geo.sy) / geo.sy * ss) for x, y in pts]
                d.line(P, fill=g.color, width=w, joint="curve")
                for (x, y) in (P[0], P[-1]):
                    d.ellipse([x - r, y - r, x + r, y + r], fill=g.color)
    return img.resize((cw, ch), Image.Resampling.LANCZOS)


def reference_canvas(scene):
    """Foto ideal colocada en el lienzo completo (para medir fidelidad)."""
    full = np.full((scene.ch, scene.cw, 3), 255, np.uint8)
    pil = Image.fromarray(scene.photo.astype(np.uint8)).resize((scene.cw, scene.ch), Image.Resampling.LANCZOS)
    return pil


def fidelity(sim_img, ref_img, mask_rect):
    """Métricas sobre el rectángulo de la foto: dE76 medio, p95, % de píxeles con dE>10, PSNR."""
    x, y, w, h = mask_rect
    a = np.asarray(sim_img.crop((x, y, x + w, y + h)).resize((w // 2, h // 2), Image.Resampling.BOX), np.float32)
    b = np.asarray(ref_img.crop((x, y, x + w, y + h)).resize((w // 2, h // 2), Image.Resampling.BOX), np.float32)
    de = np.sqrt(((rgb2lab(a) - rgb2lab(b)) ** 2).sum(-1))
    mse = float(((a - b) ** 2).mean())
    return dict(mean=float(de.mean()), p95=float(np.percentile(de, 95)), over10=float((de > 10).mean() * 100),
                psnr=float(10 * math.log10(255 ** 2 / max(mse, 1e-9))))
