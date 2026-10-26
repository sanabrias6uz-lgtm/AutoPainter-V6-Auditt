# -*- coding: utf-8 -*-
"""
Pruebas de V6. No tocan el ratón ni MediBang: usan un MediBang FALSO que dibuja en una imagen de Pillow.
Uso:  python test_v6.py
  Parte R  regresión: las 16 pruebas originales de V5 (test_v5.py, sin modificar) + V5 intacto (SHA-256)
  Parte N  pruebas nuevas de V6
"""
import hashlib, io, contextlib, json, math, os, shutil, subprocess, sys, tempfile, time, types
import numpy as np
from PIL import Image, ImageDraw
import run_painter_strokes_v5 as v5
import v6_planner as p6, v6_engine as eng, v6_checkpoint as ckm
from v6_driver import Driver6, UnsafeCondition, smooth_path, V6_DEFAULTS

results = []
def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""), flush=True)

# =============================================================== PARTE R: regresión V5
print("--- PARTE R: regresión V5 ---")
r = subprocess.run([sys.executable, "test_v5.py"], capture_output=True, text=True)
lines = [l for l in r.stdout.splitlines() if l.startswith(("PASS", "FAIL"))]
npass = sum(l.startswith("PASS") for l in lines)
check("R1 las 16 pruebas originales de V5 (test_v5.py sin modificar) pasan", r.returncode == 0 and len(lines) == 16 and npass == 16, f"{npass}/{len(lines)}")
for l in lines:
    if l.startswith("FAIL"): print("      ", l)
sha = hashlib.sha256(open("run_painter_strokes_v5.py", "rb").read()).hexdigest()
check("R2 run_painter_strokes_v5.py no ha sido modificado", sha == open("v5_sha256.txt").read().strip())
check("R3 V6 reutiliza funciones de V5 (lab/dE/Geometry/Driver/fit_stroke_model)",
      issubclass(Driver6, v5.Driver) and p6.v5 is v5 and eng.v5 is v5)

# =============================================================== preparación
print("--- PARTE N: pruebas nuevas de V6 ---")
settings = v5.load_settings()
a = settings["canvas_area"]; area = (a["left"], a["top"], a["width"], a["height"])
cres = (1800, 1200)
geo = v5.Geometry(area, cres, 320, 213)
name, image = v5.pick_image()
small = image.resize((480, round(480 * image.height / image.width)), Image.Resampling.LANCZOS)
scene = p6.Scene(small, cres, 0.25, seed=3)
PRE = p6.PRESETS6[-1]                                   # preset rápido para que las pruebas duren poco
t0 = time.time()
planA, _ = p6.make_plan6(scene, PRE, seed=3)
planA2, _ = p6.make_plan6(scene, PRE, seed=3)
planB, _ = p6.make_plan6(scene, PRE, seed=4)
print(f"      plan de pruebas: {planA.n_strokes} trazos ({time.time() - t0:.0f}s)")

# N1 reproducibilidad
check("N1 misma semilla -> mismo plan; otra semilla -> otro plan", planA.digest() == planA2.digest() and planA.digest() != planB.digest(), planA.digest())

# N2 geometría
ox, oy, rw, rh = planA.meta["photo_rect"]
bad = 0
for i, si, size, col, pts in planA.flat():
    if len(pts) < 2 or any(not (math.isfinite(x) and math.isfinite(y)) for x, y in pts): bad += 1; continue
    m = size * 0.5
    if any(x < ox - m or x > ox + rw + m or y < oy - m or y > oy + rh + m for x, y in pts): bad += 1
check("N2 todos los trazos: >=2 puntos finitos y dentro de la foto (margen = radio)", bad == 0, f"{bad} malos de {planA.n_strokes}")

# N3 etapas
keys = [s.key for s in planA.stages]
maxsz = [max(g.size for g in s.groups) for s in planA.stages if s.groups]
layers = [s.layer for s in planA.stages]
check("N3a seis etapas en orden, una capa distinta por etapa", keys == ["composicion", "bloqueo", "volumen", "textura", "detalles", "acabado"] and len(set(layers)) == 6, str(keys))
check("N3b pincel máximo de cada etapa decrece (grande -> fino)", all(maxsz[i] >= maxsz[i + 1] for i in range(len(maxsz) - 1)), str(maxsz))
check("N3c las etapas de pintura principales tienen trazos", all(s.n_strokes > 0 for s in planA.stages[:5]), str([s.n_strokes for s in planA.stages]))

# N4 no es un mosaico de líneas rectas idénticas
angs, turns, nseg = [], 0, 0
for i, si, size, col, pts in planA.flat():
    if len(pts) < 3: continue
    a_prev = None
    tot_turn = 0
    for k in range(len(pts) - 1):
        a_ = math.atan2(pts[k + 1][1] - pts[k][1], pts[k + 1][0] - pts[k][0])
        angs.append(a_)
        if a_prev is not None:
            d = abs((a_ - a_prev + math.pi) % (2 * math.pi) - math.pi); tot_turn += d
        a_prev = a_
    nseg += 1; turns += tot_turn > math.radians(12)
angs = np.array(angs)
hist, _ = np.histogram(np.abs(np.sin(angs)), bins=8, range=(0, 1))
frac_nonhoriz = float((np.abs(np.sin(angs)) > 0.35).mean())
check("N4a orientaciones variadas (>= 30 % de segmentos no horizontales)", frac_nonhoriz >= 0.30, f"{frac_nonhoriz * 100:.0f} %")
check("N4b trazos curvos (>= 20 % de los trazos largos giran más de 12°)", turns / max(1, nseg) >= 0.20, f"{turns / max(1, nseg) * 100:.0f} %")

# N5 simulación sin huecos blancos (preset realista B alta, resolución interna 0.5)
scene5 = p6.Scene(small, cres, 0.5, seed=3)
plan5_, _ = p6.make_plan6(scene5, next(p for p in p6.PRESETS6 if p["name"] == "B alta"), seed=3)
ox5, oy5, rw5, rh5 = plan5_.meta["photo_rect"]
def white_holes(plan, scn):
    sim_ = p6.render_plan6(plan, ss=1); ref_ = p6.reference_canvas(scn)
    sp, rp = sim_.load(), ref_.load()
    ox_, oy_, rw_, rh_ = plan.meta["photo_rect"]
    h = t = 0
    for y in range(oy_ + 10, oy_ + rh_ - 10, 3):
        for x in range(ox_ + 10, ox_ + rw_ - 10, 3):
            if min(rp[x, y]) < 235:
                t += 1; h += (min(sp[x, y]) >= 250)
    return h / t * 100, t, sim_, ref_
pct5, tot5, sim5_, ref5_ = white_holes(plan5_, scene5)
check("N5 simulador V6 (preset B alta): píxeles blancos sin cubrir <= 0.05 %", pct5 <= 0.05, f"{pct5:.3f} % de {tot5} muestras")
pctE, totE, simE, refE = white_holes(planA, scene)
print(f"      (informativo) preset E minima a resolución interna 0.25: {pctE:.2f} % de muestras claras quedan sin pintar -> compromiso del preset rápido")
f = p6.fidelity(simE, refE, planA.meta["photo_rect"])
check("N5b fidelidad mínima del preset rápido E (dE medio <= 9)", f["mean"] <= 9.0, f"dE medio {f['mean']:.2f}, PSNR {f['psnr']:.1f}")

# =============================================================== MediBang FALSO
class FakeScreen:
    """Simula pantalla + ratón + MediBang: dibuja en una imagen lo que el ratón pintaría."""
    SIZE_SPOT, LAYER_SPOT = (1700, 40), (1800, 40)
    PANEL = (1800, 100, 100, 300)
    def __init__(self, size_mode_input=True):
        self.img = Image.new("RGB", (1920, 1080), (60, 60, 60))
        ImageDraw.Draw(self.img).rectangle([area[0], area[1], area[0] + area[2], area[1] + area[3]], fill=(255, 255, 255))
        self.log = []; self.down = False; self.pos = (0, 0); self.color = (0, 0, 0); self.size = 1.0
        self.expect_size = False; self.layer_clicks = 0; self.PAUSE = None; self.FAILSAFE = True
        self.strokes_drawn = 0; self.stop_after = None; self.calls = 0; self.fail_on_move = None; self.moves = 0
        self.draw_enabled = True
    # pyautogui
    def moveTo(self, x, y, *a, **k):
        self.moves += 1
        if self.fail_on_move is not None and self.moves == self.fail_on_move: raise RuntimeError("FailSafe simulado")
        if self.down and self.draw_enabled:
            d = ImageDraw.Draw(self.img); w = max(1, round(self.size * geo.sx)); r = w / 2
            d.line([self.pos, (x, y)], fill=self.color, width=w)
            d.ellipse([x - r, y - r, x + r, y + r], fill=self.color)
        self.pos = (x, y); self.log.append(("move", x, y))
    def mouseDown(self, *a, **k): self.down = True; self.strokes_drawn += 1; self.log.append(("down",))
    def mouseUp(self, *a, **k): self.down = False; self.log.append(("up",))
    def click(self, x=None, y=None, *a, **k):
        self.log.append(("click", x, y))
        if (x, y) == self.SIZE_SPOT: self.expect_size = True
        if (x, y) == self.LAYER_SPOT:
            self.layer_clicks += 1
            ImageDraw.Draw(self.img).rectangle([self.PANEL[0] + 5, self.PANEL[1] + 20 * self.layer_clicks, self.PANEL[0] + 90, self.PANEL[1] + 20 * self.layer_clicks + 15], fill=(200, 120, 40))
    def hotkey(self, *a, **k): self.log.append(("hotkey",) + a)
    def press(self, *a, **k): pass
    def write(self, text, *a, **k):
        if self.expect_size: self.size = float(text); self.expect_size = False
        elif text.startswith("#"): self.color = tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))
    # keyboard
    def is_pressed(self, k):
        self.calls += 1
        return self.stop_after is not None and self.calls > self.stop_after
    # captura
    def grab(self, bbox): return self.img.crop(bbox).copy()

def make_drv(fs, title="MediBang Paint Pro", layer_mode="off", batch=10, **extra):
    sys.modules["pyautogui"], sys.modules["keyboard"] = fs, fs
    cfg5 = dict(v5.CFG); cfg5.update(STEP_DELAY=0, DOWN_DELAY=0, END_HOLD=0, COLOR_OPEN_DELAY=0, COLOR_UI_DELAY=0, COLOR_CONFIRM_DELAY=0)
    v6 = dict(V6_DEFAULTS); v6.update(BATCH=batch, LAYER_MODE=layer_mode, BRUSH_SIZE_MODE="input", SAVE_HOTKEY=None, LAYER_DELAY=0, SIZE_DELAY=0, RECHECKS=1)
    v6.update(extra)
    spots = dict(brush_size_input=fs.SIZE_SPOT, new_layer_button=fs.LAYER_SPOT, layer_panel_region=fs.PANEL)
    return Driver6(geo, cfg5, tuple(settings["color_picker_spot"]), tuple(settings["color_input_spot"]), tuple(settings["color_confirmation_spot"]),
                   v6, spots, grab=fs.grab, title_fn=lambda: title, say=lambda *a: None, ask=lambda *a: "")

# subplan pequeño (región alrededor del centro de la foto) para las pruebas de ejecución
import v6_compare as cmp_
rect = cmp_.focus_rects(scene, 0.22, 0.24)["ojos"]
sub = eng.region_plan(planA, rect)
NSUB = sub.n_strokes
print(f"      subplan de ejecución: {NSUB} trazos, región {rect}")
tmp = tempfile.mkdtemp(prefix="v6test_")

# N6 trayectorias y pares down/up
fs = FakeScreen(); drv = make_drv(fs)
ck = ckm.Checkpoint(os.path.join(tmp, "run1")); ck.start_new(sub, "t", "sha", "cfg")
ex = eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: "")
ok = ex.run(0)
downs = sum(1 for e in fs.log if e[0] == "down")
state, paired = False, True
for e in fs.log:                                       # cada down debe cerrarse con un up antes del siguiente down
    if e[0] == "down":
        paired &= (not state); state = True
    elif e[0] == "up":
        state = False
paired &= (not state)
check("N6a ejecución completa del subplan (todos los trazos)", ok and fs.strokes_drawn == NSUB, f"{fs.strokes_drawn}/{NSUB}")
check("N6b cada mouseDown se cierra con su mouseUp antes del siguiente y al final el botón está suelto", paired and downs == NSUB, f"{downs} pulsaciones")
mv = [e for e in fs.log if e[0] == "move"]
inside = all(area[0] <= e[1] <= area[0] + area[2] and area[1] <= e[2] <= area[1] + area[3] for e in mv)
check("N6c el ratón nunca sale del lienzo en pantalla", inside, f"{len(mv)} movimientos")
check("N6d trazos curvos: más de 2 puntos de ratón en la mayoría", sum(1 for e in mv) / NSUB > 3, f"{len(mv) / NSUB:.1f} mov/trazo")
check("N6e el checkpoint confirma todos los trazos y la verificación fue fuerte", ck.data["confirmed"] == NSUB and ck.data["verification"]["fuerte"] > 0 and ck.data["finished"], str(ck.data["verification"]))
# la pantalla simulada se parece al plan renderizado (la ejecución dibuja lo planificado)
scr = fs.img.crop((area[0], area[1], area[0] + area[2], area[1] + area[3])).resize(cres, Image.Resampling.BOX)
ideal = p6.render_plan6(sub, ss=1)
x0, y0, x1, y1 = rect
da = np.abs(np.asarray(scr.crop(rect), np.float32) - np.asarray(ideal.crop(rect), np.float32)).mean()
check("N6f lo dibujado en la pantalla simulada coincide con la simulación del plan", da < 12, f"diferencia media {da:.1f}/255")
full_img = fs.img.copy()

# N7 ESC: libera el botón, no pierde la confirmación previa y se puede reanudar sin repetir lo confirmado
fs = FakeScreen(); drv = make_drv(fs)
ck = ckm.Checkpoint(os.path.join(tmp, "run2")); ck.start_new(sub, "t", "sha", "cfg")
ex = eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: "")
fs.stop_after = 45                                     # ESC tras ~45 comprobaciones (a mitad del plan)
ok = ex.run(0)
conf1, att1 = ck.data["confirmed"], ck.data["attempted"]
check("N7a ESC detiene la ejecución y deja el botón suelto", (not ok) and ex.stopped == "ESC" and not fs.down, f"detenida con {conf1}/{NSUB} confirmados")
check("N7b hay progreso parcial confirmado y no se confirmó de más", 0 < conf1 < NSUB and conf1 <= att1 <= NSUB, f"confirmados {conf1}, enviados {att1}")
drawn_before = fs.strokes_drawn
# --- reanudar desde el disco (como tras un cierre inesperado)
ck2 = ckm.Checkpoint(os.path.join(tmp, "run2")); plan_loaded = ck2.load()
fs.stop_after = None
drv = make_drv(fs); drv.pg = fs; drv.kb = fs
ex2 = eng.Executor(drv, plan_loaded, ck2, say=lambda *a: None, ask=lambda *a: "")
ok2 = ex2.run(ck2.resume_index)
total_drawn = fs.strokes_drawn
repeated = total_drawn - NSUB
check("N7c reanudar completa el plan", ok2 and ck2.data["finished"] and ck2.data["confirmed"] == NSUB)
check("N7d solo se repiten los trazos enviados y no confirmados (<= 1 lote)", 0 <= repeated <= 10, f"repetidos {repeated} (lote=10)")
a1 = np.asarray(fs.img, np.int16); a2 = np.asarray(full_img, np.int16)
check("N7e el resultado tras interrumpir+reanudar es idéntico al de la ejecución limpia", int(np.abs(a1 - a2).max()) == 0, f"dif máx {int(np.abs(a1 - a2).max())}")

# N8 excepción en mitad de un trazo (p. ej. FailSafe de pyautogui) libera el botón
fs = FakeScreen(); drv = make_drv(fs); fs.fail_on_move = 30
ck = ckm.Checkpoint(os.path.join(tmp, "run3")); ck.start_new(sub, "t", "sha", "cfg")
ex = eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: "")
ok = ex.run(0)
check("N8 error en mitad de un trazo: parada controlada con el botón suelto", (not ok) and (not fs.down) and "error" in str(ex.stopped), str(ex.stopped))

# N9 verificación fallida (la pantalla no cambia) -> parada de seguridad, nada confirmado
fs = FakeScreen(); fs.draw_enabled = False; drv = make_drv(fs)
ck = ckm.Checkpoint(os.path.join(tmp, "run4")); ck.start_new(sub, "t", "sha", "cfg")
ex = eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: "")
ok = ex.run(0)
check("N9 si los trazos no aparecen en pantalla: parada de seguridad y NADA confirmado", (not ok) and "seguridad" in str(ex.stopped) and ck.data["confirmed"] == 0 and not fs.down, str(ex.stopped)[:70])

# N10 ventana equivocada
fs = FakeScreen(); drv = make_drv(fs, title="Bloc de notas")
ck = ckm.Checkpoint(os.path.join(tmp, "run5")); ck.start_new(sub, "t", "sha", "cfg")
ex = eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: "")
ok = ex.run(0)
check("N10 ventana en primer plano distinta de MediBang: no se dibuja nada", (not ok) and fs.strokes_drawn == 0 and "seguridad" in str(ex.stopped))

# N11 guardia de lienzo: márgenes blancos
fs = FakeScreen(); drv = make_drv(fs)
ok_g, det = drv.canvas_guard(planA.meta["photo_rect"])
ImageDraw.Draw(fs.img).rectangle([area[0], area[1], area[0] + 60, area[1] + area[3]], fill=(30, 30, 30))
ok_b, det2 = drv.canvas_guard(planA.meta["photo_rect"])
check("N11 guardia de lienzo: márgenes blancos -> OK; margen ensuciado -> alerta", ok_g is True and ok_b is False, f"{det} | {det2}")

# N12 capas automáticas verificables por el panel
fs = FakeScreen(); drv = make_drv(fs, layer_mode="auto")
r1 = drv.new_layer("x")
fs2 = FakeScreen(); drv2 = make_drv(fs2, layer_mode="auto"); fs2.click = lambda *a, **k: None       # el botón no hace nada
try:
    drv2.new_layer("x"); unsafe = False
except UnsafeCondition:
    unsafe = True
check("N12 capa nueva auto: verificada si el panel cambia; error si no cambia", r1 == "auto-verificada" and unsafe, r1)
fs3 = FakeScreen(); drv3 = make_drv(fs3, layer_mode="off")
check("N12b modo 'off' no finge capas (no se hace ningún clic)", drv3.new_layer("x") == "off" and fs3.layer_clicks == 0)

# N13 ejecución con capas automáticas: una capa por etapa con trazos
fs = FakeScreen(); drv = make_drv(fs, layer_mode="auto")
ck = ckm.Checkpoint(os.path.join(tmp, "run6")); ck.start_new(sub, "t", "sha", "cfg")
ex = eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: "")
ok = ex.run(0)
check("N13 ejecución con capas auto: se crean 6 capas (una por etapa) y se confirma todo", ok and fs.layer_clicks == len(sub.stages) and ck.data["confirmed"] == NSUB, f"{fs.layer_clicks} capas")

# N14 checkpoint atómico y robusto
d5 = os.path.join(tmp, "run7"); ck = ckm.Checkpoint(d5); ck.start_new(sub, "t", "sha", "cfg"); ck.confirm(25, "fuerte", 12.0)
open(os.path.join(d5, "progress_v6.json.tmp"), "w").write("{corrupto")      # restos de una escritura interrumpida
ck_b = ckm.Checkpoint(d5); pl = ck_b.load()
good = ck_b.resume_index == 25 and pl.n_strokes == NSUB and ck_b.compatible("sha", "cfg") and not ck_b.compatible("otra", "cfg")
with open(ck_b.plan_path, "ab") as f: f.write(b"basura")
try:
    ckm.Checkpoint(d5).load(); detect = False
except Exception:
    detect = True
check("N14 checkpoint: sobrevive a un .tmp corrupto, detecta imagen distinta y plan dañado", good and detect)

# N15 serialización del plan
pj = os.path.join(tmp, "p.json.gz"); sub.save(pj); back = p6.Plan6.load(pj)
check("N15 el plan se guarda y se recarga con la MISMA huella (sin pérdida de coordenadas)", back.digest() == sub.digest() and back.n_strokes == sub.n_strokes and back.n_color_changes == sub.n_color_changes)

# N16 modelo de tiempos
t_ = dict(color_s=1.0, stroke_a=0.05, stroke_b=0.001, verify_s=0.1, layer_s=2.0, size_s=0.5, save_s=1.0, source="x")
lens, sc_ = eng.plan_metrics(sub, geo)
manual = sum(t_["stroke_a"] + t_["stroke_b"] * L for L in lens) + sub.n_color_changes + len(sub.stages) * 3.0 + sc_ * 0.5 + math.ceil(NSUB / 40) * 0.1
check("N16a estimate_seconds6 = suma explícita de sus términos", abs(eng.estimate_seconds6(sub, t_, geo, 40) - manual) < 1e-6)
samples = [(L, 0.020 + 0.0004 * L) for L in range(5, 200, 3)]
a_, b_ = v5.fit_stroke_model(samples)
check("N16b ajuste lineal de tiempos reutilizado de V5", abs(a_ - 0.02) < 1e-6 and abs(b_ - 0.0004) < 1e-8)
tm = eng.timing_from_executor(ex, eng.ASSUMED)
check("N16c timing_from_executor produce tiempos medidos positivos", tm["stroke_a"] >= 0 and tm["color_s"] >= 0 and "medido" in tm["source"], tm["source"])

# N17 smooth_path
P = [(100, 100), (140, 120), (180, 100), (200, 160)]
sp_ = smooth_path(P, 6)
steps = [math.hypot(sp_[i + 1][0] - sp_[i][0], sp_[i + 1][1] - sp_[i][1]) for i in range(len(sp_) - 1)]
check("N17 smooth_path: pasa por los extremos y separa puntos <= paso+1", sp_[0] == (100, 100) and sp_[-1] == (200, 160) and max(steps) <= 7.5, f"{len(sp_)} puntos")


# =============================================================== VERIFICACIÓN: casos realistas y controles negativos
print("--- VERIFICACIÓN (render con suavizado + controles negativos) ---")
from PIL import ImageChops
SSV = 2
def render_screen(batch, color_shift=(0, 0, 0)):
    c = Image.new("RGB", (cres[0] * SSV, cres[1] * SSV), "white"); dd = ImageDraw.Draw(c)
    for pts, col, size in batch:
        col2 = tuple(min(255, max(0, a + b)) for a, b in zip(col, color_shift))
        w = max(1, round(size * SSV)); r = w / 2; P = [(x * SSV, y * SSV) for x, y in pts]
        dd.line(P, fill=col2, width=w, joint="curve")
        for (x, y) in (P[0], P[-1]): dd.ellipse([x - r, y - r, x + r, y + r], fill=col2)
    return c.resize((area[2], area[3]), Image.Resampling.BILINEAR)     # reducción suavizada, como la pantalla real
def batch_of(plan, size, n=40):
    for st in plan.stages:
        for g in st.groups:
            if g.size == size and len(g.strokes) >= n:
                return [(p, g.color, g.size) for p in g.strokes[:n]]
white_img = Image.new("RGB", (area[2], area[3]), "white")
def verifier_for(img):
    d_ = make_drv(FakeScreen()); d_.grab_canvas = lambda: img; return d_
def legacy_verify(batch, prev, img):
    """Copia del algoritmo ANTERIOR (v6_driver original) para demostrar qué hacía."""
    import v6_driver as dm
    g = geo; checked = match = 0; xs = []; ys = []
    for pts, color, size in batch[-8:]:
        x, y = pts[len(pts) // 2]; sx, sy = round(x * g.sx), round(y * g.sy); w = size * g.sx
        for (px, py) in pts: xs.append(px * g.sx); ys.append(py * g.sy)
        if w < 1.5: continue
        p = dm.mean_patch(img, sx, sy, r=0 if w < 3 else 1)
        if p is None: continue
        checked += 1
        if v5.dE((round(p[0]), round(p[1]), round(p[2])), tuple(color)) <= (14 if w >= 3 else 22): match += 1
    if checked and match >= max(1, math.ceil(checked * 0.5)): return "fuerte"
    box = (max(0, int(min(xs)) - 2), max(0, int(min(ys)) - 2), min(g.width, int(max(xs)) + 3), min(g.height, int(max(ys)) + 3))
    a_, b_ = prev.crop(box).convert("L"), img.crop(box).convert("L")
    da = sum(abs(p - q) for p, q in zip(a_.getdata(), b_.getdata())) / max(1, (box[2] - box[0]) * (box[3] - box[1]))
    return "debil" if da > 0.8 else "ninguno"

# N22 trazo BIEN dibujado y muestreo impreciso (desalineación 1-2 px, bordes suavizados, trazos gruesos y finos): se debe confirmar
res22, leg22 = [], 0
for size in (5, 3.5, 2.5):
    b = batch_of(plan5_, size); base = render_screen(b)
    for off in ((0, 0), (1, 0), (1, 1), (2, 0), (0, 2)):
        im = ImageChops.offset(base, *off)
        lvl, _, det = verifier_for(im).verify_batch(b, white_img)
        res22.append(lvl in ("fuerte",)); leg22 += legacy_verify(b, white_img, im) == "fuerte"
check("N22 trazos bien dibujados con muestreo impreciso (+-2 px, bordes suaves, 5/3,5/2,5 px): se confirman", all(res22), f"{sum(res22)}/{len(res22)} (el verificador anterior: {leg22}/{len(res22)})")

# N23 DEFECTO REAL del verificador anterior: color equivocado pero 'algo cambió' -> lo daba por bueno ('debil'). Ahora se rechaza.
b = batch_of(plan5_, 5); wrong = render_screen(b, color_shift=(45, -35, 30))
lvl, _, det = verifier_for(wrong).verify_batch(b, white_img)
check("N23a color equivocado (trazos visibles de otro color): el verificador anterior lo aceptaba como 'debil'", legacy_verify(b, white_img, wrong) == "debil")
check("N23b ahora se RECHAZA ('ninguno') y se diagnostica 'color_distinto'", lvl == "ninguno" and "color mal seleccionado" in det, det[-110:])

# N24 nada pintado
v_ = verifier_for(white_img); lvl, _, det = v_.verify_batch(b, white_img)
check("N24 nada pintado: se rechaza y se diagnostica 'sin_cambios'", lvl == "ninguno" and v_.last_diagnosis["kind"] == "sin_cambios", det[-110:])

# N25 lienzo desplazado ~10 px (fondo blanco): se rechaza y se diagnostica el desplazamiento
shifted = ImageChops.offset(render_screen(b), 10, 0)
v_ = verifier_for(shifted); lvl, _, det = v_.verify_batch(b, white_img)
offs = v_.last_diagnosis
dxy = offs.get("desplazamiento") or (0, 0)
check("N25 lienzo desplazado 10 px: se rechaza y se diagnostica 'desplazado' con ~10 px", lvl == "ninguno" and offs["kind"] == "desplazado" and abs(abs(dxy[0]) - 10) <= 2 and abs(dxy[1]) <= 2, offs["texto"][-90:])

# N26 repintado de trazos ya presentes -> 'presente' (no 'fuerte'); y 'debil' legítimo (color cercano, 1-2x tolerancia, zona cambiada)
ok_img = render_screen(b)
lvl_p, _, _ = verifier_for(ok_img).verify_batch(b, ok_img)
# color desplazado en proporción fija hasta que el error de color caiga entre 1x y 2x la tolerancia (14..28)
exp0 = b[0][1]; near_shift = None
for m_ in range(1, 60):
    cand = (round(1.3 * m_), round(-0.9 * m_), round(1.1 * m_))
    pai = tuple(min(255, max(0, a + s)) for a, s in zip(exp0, cand)); de0 = v5.dE(pai, tuple(exp0))
    if 16 <= de0 <= 26:
        near_shift = cand; break
near_img = render_screen(b, color_shift=near_shift or (0, 0, 0))
lvl_w, _, det_w = verifier_for(near_img).verify_batch(b, white_img)
check("N26a repintar sobre lo ya pintado se clasifica como 'presente'", lvl_p == "presente")
check("N26b color cercano (dE entre 1x y 2x la tolerancia) + zona cambiada -> 'debil' (se acepta)", near_shift is not None and lvl_w == "debil", f"dE={de0:.1f}, nivel {lvl_w}")

# N27 volcado de diagnóstico al fallar un lote (captura + log con la causa)
fs = FakeScreen(); fs.draw_enabled = False; drv = make_drv(fs)
ck = ckm.Checkpoint(os.path.join(tmp, "run11")); ck.start_new(sub, "t", "sha", "cfg")
ex = eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: "")
ok = ex.run(0)
logtxt = open(os.path.join(tmp, "run11", "run_log_v6.txt"), encoding="utf-8").read()
check("N27 verificación fallida: parada + captura verif_fallo_*.png + causa en el log", (not ok) and any(f.startswith("verif_fallo_") for f in os.listdir(os.path.join(tmp, "run11"))) and "DIAGNÓSTICO" in logtxt and ck.data["confirmed"] == 0)

# N28 sonda al reanudar: ¿se ven las últimas pinceladas ya confirmadas?
fs_full = FakeScreen(); eng.Executor(make_drv(fs_full), sub, ckm.Checkpoint(os.path.join(tmp, "run12a")), say=lambda *a: None, ask=lambda *a: "").run(0) if False else None
fs_full = FakeScreen(); drv = make_drv(fs_full); ck = ckm.Checkpoint(os.path.join(tmp, "run12a")); ck.start_new(sub, "t", "sha", "cfg")
eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: "").run(0)
START = 40
# (a) lienzo en blanco (documento/capa distintos) -> no se pinta nada
fs = FakeScreen(); drv = make_drv(fs); ck = ckm.Checkpoint(os.path.join(tmp, "run12b")); ck.start_new(sub, "t", "sha", "cfg"); ck.confirm(START, "fuerte", 1.0)
ex = eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: ""); ok_a = ex.run(START)
# (b) lienzo con lo ya pintado -> reanuda y termina
fs2 = FakeScreen(); fs2.img = fs_full.img.copy(); drv = make_drv(fs2); ck = ckm.Checkpoint(os.path.join(tmp, "run12c")); ck.start_new(sub, "t", "sha", "cfg"); ck.confirm(START, "fuerte", 1.0)
ex2 = eng.Executor(drv, sub, ck, say=lambda *a: None, ask=lambda *a: ""); ok_b = ex2.run(START)
check("N28 reanudar con lienzo que NO contiene lo confirmado: se detiene sin pintar; con lo confirmado visible: continúa", (not ok_a) and fs.strokes_drawn == 0 and "reanudar" in str(ex.stopped) and ok_b and ck.data["confirmed"] == NSUB, str(ex.stopped)[:60])

# N29 cuenta atrás configurable (tu comportamiento de 10 s se conserva por defecto)
import v6_driver as dmod
lines = []; fsx = FakeScreen(); dx = make_drv(fsx, MANUAL_RETURN_S=3); dx.say = lambda m: lines.append(m); dx.ask = lambda *a: ""
real_sleep = dmod.time.sleep; dmod.time.sleep = lambda s: None
try:
    dx.v6["BRUSH_SIZE_MODE"] = "manual"; dx.set_brush_size(5); n3 = sum("Volviendo" in l for l in lines)
    lines.clear(); dx.v6["MANUAL_RETURN_S"] = 0; dx.set_brush_size(5); n0 = sum("Volviendo" in l for l in lines)
finally:
    dmod.time.sleep = real_sleep
check("N29 cuenta atrás tras la pausa manual configurable (MANUAL_RETURN_S; por defecto 10)", n3 == 3 and n0 == 0 and dmod.V6_DEFAULTS["MANUAL_RETURN_S"] == 10, f"{n3} y {n0} líneas")

# N30 compatibilidad con el progreso guardado por la versión anterior (sin la clave 'presente')
d30 = os.path.join(tmp, "run13"); ck30 = ckm.Checkpoint(d30); ck30.start_new(sub, "t", "sha", "cfg")
ck30.data["verification"] = dict(fuerte=345, debil=2, ninguno=0, omitida=0); ck30._write()
ck_b = ckm.Checkpoint(d30); pl30 = ck_b.load(); ck_b.confirm(80, "presente", 5.0)
check("N30 un progress_v6.json de la versión anterior se carga y acepta el nivel nuevo 'presente'", ck_b.data["verification"]["presente"] == 1 and ck_b.resume_index == 80 and ck_b.data["verification"]["fuerte"] == 345)

# N18 la prueba de región completa (menú 2) con MediBang falso: mide tiempos y escribe timing_v6.json
import builtins, run_painter_strokes_v6 as r6
cwd0 = os.getcwd(); os.chdir(tempfile.mkdtemp(prefix="v6reg_"))
fs = FakeScreen(); drv = make_drv(fs)
cfg6 = dict(r6.CFG6); cfg6["REGION_FRAC"] = [0.22, 0.24]
old_input, old_cd = builtins.input, r6.countdown
builtins.input = lambda *a: ""; r6.countdown = lambda n=5: None
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    tim = r6.run_region_test(drv, planA, scene, cfg6, geo, eng.load_timing())
builtins.input, r6.countdown = old_input, old_cd
wrote = os.path.exists(eng.TIMING_FILE) and "medido" in json.load(open(eng.TIMING_FILE))["source"]
os.chdir(cwd0)
check("N18 prueba de región: dibuja, mide y guarda timing_v6.json con tiempos 'medido'", tim is not None and wrote and fs.strokes_drawn > 0, f"{fs.strokes_drawn} trazos")

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} pruebas correctas")
sys.exit(0 if all(results) else 1)
