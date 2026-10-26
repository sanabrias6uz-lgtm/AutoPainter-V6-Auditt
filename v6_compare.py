# -*- coding: utf-8 -*-
"""Comparación visual y numérica V5 vs V6 (solo simulación; no toca el ratón)."""
import numpy as np
from PIL import Image, ImageDraw
import run_painter_strokes_v5 as v5
import v6_planner as p6
import v6_analysis as an


def focus_rects(scene, frac_w=0.24, frac_h=0.26):
    """Rectángulos (px de lienzo) de ojos y pelo, deducidos de los mapas de análisis."""
    ey = scene.maps["eye_like"]
    big = an.gaussian(ey, 6.0)
    ys, xs = np.unravel_index(int(np.argmax(big)), big.shape)
    # centroide de las celdas más "ojo" para caer entre los dos ojos
    thr = np.percentile(ey[scene.mask], 99)
    yy, xx = np.nonzero((ey >= thr) & scene.mask)
    if len(yy):
        xs, ys = float(xx.mean()), float(yy.mean())
    cx, cy = xs / scene.ws, ys / scene.ws
    w, h = scene.rw * frac_w, scene.rh * frac_h
    eyes = _clamp((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), scene)
    hair = an.gaussian(scene.maps["dark"] * scene.maps["coherence"], 8.0)
    hy, hx = np.unravel_index(int(np.argmax(hair)), hair.shape)
    cx, cy = hx / scene.ws, hy / scene.ws
    hairr = _clamp((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), scene)
    return dict(ojos=eyes, pelo=hairr)


def _clamp(r, scene):
    x0, y0, x1, y1 = r
    w, h = x1 - x0, y1 - y0
    x0 = min(max(x0, scene.ox), scene.ox + scene.rw - w); y0 = min(max(y0, scene.oy), scene.oy + scene.rh - h)
    return (int(x0), int(y0), int(x0 + w), int(y0 + h))


def compare(scene, plan5, plan6, out_path, timing5=None, timing6=None, est5=None, est6=None, geo6=None):
    ref = p6.reference_canvas(scene)
    sim5 = v5.render_preview(plan5)
    sim6 = p6.render_plan6(plan6, ss=2)
    rect = [scene.ox, scene.oy, scene.rw, scene.rh]
    f5, f6 = p6.fidelity(sim5, ref, rect), p6.fidelity(sim6, ref, rect)
    crops = focus_rects(scene)
    cw = 560
    def fit(im, box=None):
        im = im.crop(box) if box else im.crop((scene.ox, scene.oy, scene.ox + scene.rw, scene.oy + scene.rh))
        s = cw / im.width
        return im.resize((cw, max(1, round(im.height * s))), Image.Resampling.LANCZOS)
    rows = [("Foto original", ref), ("V5 (simulación)", sim5), ("V6 (simulación)", sim6)]
    top = [fit(i) for _, i in rows]
    row2 = [fit(i, crops["ojos"]) for _, i in rows]
    row3 = [fit(i, crops["pelo"]) for _, i in rows]
    H1, H2, H3 = max(i.height for i in top), max(i.height for i in row2), max(i.height for i in row3)
    pad, head, foot = 8, 26, 120
    W = 3 * cw + 4 * pad
    H = head + H1 + head + H2 + head + H3 + foot + 5 * pad
    sheet = Image.new("RGB", (W, H), (245, 245, 245))
    d = ImageDraw.Draw(sheet)
    y = pad
    for title, row, hh in (("Imagen completa", top, H1), ("Zoom: ojos / nariz", row2, H2), ("Zoom: pelo", row3, H3)):
        d.text((pad, y), title, fill=(0, 0, 0)); y += head - 8
        for k, im in enumerate(row):
            sheet.paste(im, (pad + k * (cw + pad), y))
            d.text((pad + k * (cw + pad) + 4, y + 4), rows[k][0], fill=(255, 0, 0))
        y += hh + pad + 8
    n5 = plan5.strokes; n6 = plan6.n_strokes
    lines = [
        f"V5 preset '{plan5.preset['name']}': {n5} trazos, {plan5.color_changes} colores | dE medio {f5['mean']:.2f} | p95 {f5['p95']:.1f} | >10: {f5['over10']:.1f}% | PSNR {f5['psnr']:.1f} dB" + (f" | tiempo est. {est5}" if est5 else ""),
        f"V6 preset '{plan6.meta['preset']}': {n6} trazos, {plan6.n_color_changes} colores | dE medio {f6['mean']:.2f} | p95 {f6['p95']:.1f} | >10: {f6['over10']:.1f}% | PSNR {f6['psnr']:.1f} dB" + (f" | tiempo est. {est6}" if est6 else ""),
        "Metrica: dE76 entre cada simulacion y la foto colocada en el lienzo (misma metrica para ambas). Menor = mas fiel.",
        "Las dos son SIMULACIONES en Pillow; el resultado real en MediBang no se ha medido aqui.",
    ]
    for k, ln in enumerate(lines):
        d.text((pad, y + k * 16), ln, fill=(0, 0, 0))
    sheet.save(out_path)
    return dict(v5=f5, v6=f6, crops=crops)
