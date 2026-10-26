# -*- coding: utf-8 -*-
"""
Estudio de la verificación SIN ratón ni MediBang: simula cómo vería la pantalla cada lote si todo se pintara bien y aplica el
mismo muestreo que v6_driver.verify_batch (trazos gruesos: media 3x3 en el punto medio; finos: mejor de 9 píxeles).

  python estudio_verificacion.py                       # usa v6_progress/plan_v6.json.gz y settings.txt
  python estudio_verificacion.py --indice 31954        # muestra el lote que contiene ese trazo y sus 8 muestras
  python estudio_verificacion.py --plan otro.json.gz

Si para un lote la simulación da 8/8 y en tu pantalla real fallan, el problema NO es el muestreo: es que los trazos no se ven
(capa, herramienta, color, foco) o que la pantalla difiere de lo planificado. Es una simulación: no sustituye a MediBang.
"""
import argparse, math, os, sys, collections
import numpy as np
from PIL import Image, ImageDraw
import run_painter_strokes_v5 as v5
import v6_planner as p6

ap = argparse.ArgumentParser()
ap.add_argument("--plan", default=os.path.join("v6_progress", "plan_v6.json.gz"))
ap.add_argument("--indice", type=int, default=None)
ap.add_argument("--batch", type=int, default=40)
args = ap.parse_args()

s = v5.load_settings(); a = s["canvas_area"]
area = (a["left"], a["top"], a["width"], a["height"])
geo = v5.Geometry(area, (int(s.get("canvas_width_resolution", 1800)), int(s.get("canvas_height_resolution", 1200))), 320, 213)
plan = p6.Plan6.load(args.plan)
SS = 2
canvas = Image.new("RGB", (geo.cw * SS, geo.ch * SS), "white"); d = ImageDraw.Draw(canvas)

def draw(pts, color, size):
    w = max(1, round(size * SS)); r = w / 2; P = [(x * SS, y * SS) for x, y in pts]
    d.line(P, fill=color, width=w, joint="curve")
    for (x, y) in (P[0], P[-1]): d.ellipse([x - r, y - r, x + r, y + r], fill=color)

def screen_mean(sx, sy, r):
    x0 = (sx - r) / geo.sx * SS; x1 = (sx + r + 1) / geo.sx * SS; y0 = (sy - r) / geo.sy * SS; y1 = (sy + r + 1) / geo.sy * SS
    box = (int(math.floor(x0)), int(math.floor(y0)), int(math.ceil(x1)), int(math.ceil(y1)))
    return tuple(round(c) for c in np.asarray(canvas.crop(box), np.float32).reshape(-1, 3).mean(0))

def sample(batch):
    out = []
    for pts, color, size in batch[-8:]:
        w = size * geo.sx
        if w < 1.5: continue
        x, y = pts[len(pts) // 2]; sx, sy = round(x * geo.sx), round(y * geo.sy)
        if w >= 3: de = v5.dE(screen_mean(sx, sy, 1), tuple(color)); tol = 14
        else: de = min(v5.dE(screen_mean(sx + dx, sy + dy, 0), tuple(color)) for dx in (-1, 0, 1) for dy in (-1, 0, 1)); tol = 22
        out.append((de <= tol, de, (sx, sy), color, round(w, 1)))
    return out

rows, batch, first, cs, csz = [], [], None, None, None
def flush(last):
    global batch
    if batch:
        smp = sample(batch); rows.append((first, last, batch[-1][2], sum(x[0] for x in smp), len(smp), smp))
    batch = []
for (i, si, size, color, pts) in plan.flat():
    if si != cs or size != csz: flush(i - 1); cs, csz = si, size
    if not batch: first = i
    draw(pts, color, size); batch.append((pts, color, size))
    if len(batch) >= args.batch: flush(i)
flush(plan.n_strokes - 1)
need = lambda n: max(1, math.ceil(n * 0.5))
print(f"{len(rows)} lotes simulados; con pintura perfecta no pasarían por color: {sum(1 for r in rows if r[4] and r[3] < need(r[4]))}")
by = collections.defaultdict(lambda: [0, 0])
for r in rows: by[r[2]][0] += 1; by[r[2]][1] += (r[4] and r[3] < need(r[4]))
for sz in sorted(by, reverse=True): print(f"  pincel {sz:>4g} px (pantalla {sz * geo.sx:.1f} px): {by[sz][0]:>4} lotes, {by[sz][1]} que no pasarían")
if args.indice is not None:
    for r in rows:
        if r[0] <= args.indice <= r[1]:
            print(f"\nLote {r[0]}-{r[1]} (pincel {r[2]:g}): simulación {r[3]}/{r[4]} muestras con el color esperado")
            for ok, de, pos, col, w in r[5]: print(f"   {'OK' if ok else 'NO'}  pantalla {pos}  esperado {col}  ancho {w}px  dE {de:.1f}")
