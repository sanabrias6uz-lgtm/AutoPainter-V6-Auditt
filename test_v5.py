# -*- coding: utf-8 -*-
"""Pruebas automáticas de la V5. No tocan el ratón ni MediBang.  Uso:  python test_v5.py"""
import json, math, sys, types, time
import run_painter_strokes_v5 as v5
from PIL import Image

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))

settings = v5.load_settings()
a = settings["canvas_area"]; area = (a["left"], a["top"], a["width"], a["height"])
cres = (int(settings.get("canvas_width_resolution", 1800)), int(settings.get("canvas_height_resolution", 1200)))
cfg = dict(v5.CFG); cfg.update(settings.get("v5", {}))
geo = v5.Geometry(area, cres, 320, round(320 * cres[1] / cres[0]))

# ---------- sintéticas ----------
def synthetic(w, h, cells, base=v5.WHITE):
    g = v5.Geometry(area, cres, 320, 213)
    tgt = [[base] * w for _ in range(h)]
    for (x, y), c in cells.items():
        tgt[y][x] = c
    return v5.Prepared(g, w, h, 10, 10, tgt, [row[:] for row in tgt])

blank = lambda p: [[v5.WHITE] * p.pw for _ in range(p.ph)]

# T1: celdas aisladas de UNA celda (el error de V4) -> deben generar trazo
iso = {(2, 1): (200, 10, 10), (7, 3): (10, 200, 10), (11, 6): (10, 10, 200), (4, 5): (90, 90, 90)}
p = synthetic(14, 8, iso)
det, final = v5.plan_detail(p, blank(p), cfg, thr=1.0, merge=0.0)
singles = [(x0, x1, y) for _, ss in det["groups"] for (x0, x1, y) in ss if x0 == x1]
check("T1 tramos de una celda se dibujan", len(singles) == len(iso), f"{len(singles)} de {len(iso)}")
check("T1b cada celda aislada queda con su color", all(final[y][x] == c for (x, y), c in iso.items()))

# T2: todos los trazos son horizontales y centrados EXACTAMENTE en la fila (sin desplazamiento aleatorio)
img_name, image = v5.pick_image()
assert image is not None, "No hay imagen en la carpeta"
prep = v5.prepare(image, geo, int(settings.get("colors_count", 256)), cfg)
plan = v5.make_plan(prep, cfg, dict(name="test", grid_w=None, thr=cfg["ERROR_THRESHOLD"], merge=cfg["MERGE_TOLERANCE"]))
row_centers = {round((prep.y_off + y + 0.5) * geo.cell_h, 6) for y in range(prep.ph)}
ok = all(round(y, 6) in row_centers for _, ss in plan.detail["groups"] for (_, _, y) in ss)
check("T2 trazos de detalle: y = centro exacto de fila (sin jitter)", ok)
check("T2b trazos horizontales (x0 <= x1)", all(x0 <= x1 for st in (plan.base, plan.detail) for _, ss in st["groups"] for (x0, x1, _) in ss))

# T3: completitud. Ninguna celda de la foto queda con más error que max(umbral, tolerancia); ninguna queda en blanco por error
tol = max(cfg["ERROR_THRESHOLD"], cfg["MERGE_TOLERANCE"])
worst = max(v5.dE(plan.final[y][x], prep.tgt[y][x]) for y in range(prep.ph) for x in range(prep.pw))
check("T3 todas las celdas procesadas (error máx de celda <= tolerancia)", worst <= tol + 1e-9, f"peor {worst:.2f} <= {tol}")
# T3b: con tolerancia 0, el estado final es EXACTAMENTE el objetivo
det0, fin0 = v5.plan_detail(prep, blank(prep), cfg, thr=0.5, merge=0.0)
bad = sum(1 for y in range(prep.ph) for x in range(prep.pw) if v5.dE(fin0[y][x], prep.tgt[y][x]) > 0.5)
check("T3b modo exacto (lienzo vacío, merge=0): 0 celdas mal", bad == 0, f"{bad} celdas")

# T4: pincel de detalle cubre la celda entera con un clic
diag = math.hypot(geo.cell_w, geo.cell_h)
check("T4 pincel de detalle >= diagonal de celda", plan.detail["brush"] >= diag - 1e-9, f"{plan.detail['brush']} >= {diag:.2f}")

# T5: trazos largos partidos con solape y cobertura continua
longrow = synthetic(250, 2, {(x, 0): (50, 60, 70) for x in range(200)})
d2, _ = v5.plan_detail(longrow, blank(longrow), cfg, thr=1.0, merge=0.0)
segs = sorted(s for _, ss in d2["groups"] for s in ss)
cw = geo.cell_w
cells_cov = set()
for (x0, x1, y) in segs:
    cells_cov.update(range(round(x0 / cw - 0.5) - longrow.x_off, round(x1 / cw - 0.5) - longrow.x_off + 1))
check("T5 trazo largo partido sin dejar celdas", cells_cov >= set(range(200)) and max((x1 - x0) / cw + 1 for x0, x1, _ in segs) <= cfg["MAX_STROKE"] + 1e-9,
      f"{len(segs)} trazos")

# T6: el ratón nunca sale del lienzo en pantalla
xs = [geo.screen(x, y) for st in (plan.base, plan.detail) for _, ss in st["groups"] for (x0, x1, y) in ss for x in (x0, x1)]
inside = all(area[0] <= sx <= area[0] + area[2] and area[1] <= sy <= area[1] + area[3] for sx, sy in xs)
check("T6 todos los puntos de pantalla dentro del lienzo", inside)

# T7: render simulado (Pillow con suavizado) -> huecos blancos donde la foto no es blanca
prev = v5.render_preview(plan)
sc = min(cres[0] / image.width, cres[1] / image.height)
rw, rh = round(image.width * sc), round(image.height * sc)
ref = image.resize((rw, rh), Image.Resampling.LANCZOS)
ox, oy = (cres[0] - rw) // 2, (cres[1] - rh) // 2
pp, rp = prev.load(), ref.load()
holes = tot = 0
for y in range(12, rh - 12, 2):
    for x in range(12, rw - 12, 2):
        r = rp[x, y]
        if min(r) < 235:
            tot += 1
            p_ = pp[x + ox, y + oy]
            holes += (min(p_) >= 250)
pct = holes / tot * 100
check("T7 simulador: píxeles blancos sin cubrir <= 0.02 %", pct <= 0.02, f"{pct:.3f} % de {tot} muestras")

# T8: la parte del programa que dibuja, con ratón y teclado FALSOS: se ejecutan todos los trazos y todos los colores
class FakePG:
    def __init__(self): self.log = []; self.PAUSE = None; self.FAILSAFE = True; self.down = False
    def moveTo(self, x, y, *a, **k): self.log.append(("move", x, y))
    def mouseDown(self, *a, **k): self.down = True; self.log.append(("down",))
    def mouseUp(self, *a, **k): self.down = False; self.log.append(("up",))
    def click(self, *a, **k): self.log.append(("click",))
    def hotkey(self, *a, **k): self.log.append(("hotkey",))
    def write(self, *a, **k): self.log.append(("write",))
class FakeKB:
    def __init__(self): self.stop_after = None; self.calls = 0
    def is_pressed(self, k):
        self.calls += 1
        return self.stop_after is not None and self.calls > self.stop_after
fpg, fkb = FakePG(), FakeKB()
sys.modules["pyautogui"], sys.modules["keyboard"] = fpg, fkb
fast = dict(cfg, STEP_DELAY=0, DOWN_DELAY=0, END_HOLD=0, COLOR_OPEN_DELAY=0, COLOR_UI_DELAY=0, COLOR_CONFIRM_DELAY=0)
drv = v5.Driver(geo, fast, tuple(settings["color_picker_spot"]), tuple(settings["color_input_spot"]), tuple(settings["color_confirmation_spot"]))
check("T8a pyautogui.PAUSE = 0 (V4 dejaba 0,1 s tras cada llamada)", fpg.PAUSE == 0)
small = v5.Plan(prep, {"brush": plan.base["brush"], "groups": plan.base["groups"][:3]}, {"brush": plan.detail["brush"], "groups": plan.detail["groups"][:3]}, plan.final, plan.preset)
state = {"done": 0}
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    v5.run_stage(drv, small.base, "b", small.strokes, state, time.time())
    v5.run_stage(drv, small.detail, "d", small.strokes, state, time.time())
check("T8b se ejecutan TODOS los trazos planificados", state["done"] == small.strokes, f"{state['done']}/{small.strokes}")
downs, ups = sum(1 for e in fpg.log if e[0] == "down"), sum(1 for e in fpg.log if e[0] == "up")
check("T8c cada mouseDown tiene su mouseUp", downs == ups == small.strokes, f"{downs}/{ups}")
# trazo de una celda: el ratón debe MOVERSE (segmento corto)
fpg.log.clear(); drv.stroke(500.0, 500.0, 300.0)
moves = [e for e in fpg.log if e[0] == "move"]
check("T8d trazo de una celda genera un segmento corto", len({m[1] for m in moves}) >= 2, f"{len(moves)} movimientos")
# ESC: debe soltar el botón
fkb.calls, fkb.stop_after = 0, 0
fpg.log.clear()
try:
    drv.stroke(500.0, 900.0, 300.0)
    esc_ok = False
except KeyboardInterrupt:
    esc_ok = not fpg.down
check("T8e ESC detiene y deja el botón suelto", esc_ok)

# T9: ajuste de tiempos
samples = [(L, 0.020 + 0.0004 * L) for L in range(5, 200, 3)]
a_, b_ = v5.fit_stroke_model(samples)
check("T9 ajuste lineal de tiempos", abs(a_ - 0.020) < 1e-6 and abs(b_ - 0.0004) < 1e-8, f"a={a_:.4f} b={b_:.5f}")

print(f"\n{sum(results)}/{len(results)} pruebas correctas")
sys.exit(0 if all(results) else 1)
