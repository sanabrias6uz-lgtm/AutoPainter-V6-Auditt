# -*- coding: utf-8 -*-
"""
AutoPainter V6 - análisis de imagen (solo numpy + Pillow, sin ratón).

Produce, sobre una rejilla de trabajo (celdas), los mapas que el planificador necesita:
  lum        luminancia 0..255
  edge       magnitud de borde 0..1
  theta      orientación local del TRAZO (rad). Es perpendicular al gradiente = sigue el borde/forma
  coherence  0..1 cuánto manda esa orientación (alta en pelo/pliegues, baja en zonas lisas)
  importance 0..1 importancia visual (ojos, rostro, contraste, centro)
  skin       0..1 probabilidad de piel (heurística de color, NO es un detector de caras)
  dark       0..1 zonas oscuras (pelo, pupilas)
Limitaciones documentadas: la importancia es heurística (piel + contraste + centralidad).
No hay detector de caras; si quieres forzar zonas usa "focus_boxes" en settings.txt.
"""
import numpy as np
from PIL import Image


def _gauss_kernel(sigma):
    r = max(1, int(round(sigma * 3)))
    x = np.arange(-r, r + 1, dtype=np.float32)
    k = np.exp(-(x * x) / (2 * sigma * sigma))
    return k / k.sum()


def gaussian(a, sigma):
    """Desenfoque gaussiano separable con numpy (bordes por reflejo)."""
    if sigma <= 0:
        return a.astype(np.float32)
    k = _gauss_kernel(sigma)
    r = len(k) // 2
    a = a.astype(np.float32)
    p = np.pad(a, ((0, 0), (r, r)), mode="reflect")
    out = np.zeros_like(a)
    for i, w in enumerate(k):
        out += w * p[:, i:i + a.shape[1]]
    p = np.pad(out, ((r, r), (0, 0)), mode="reflect")
    out2 = np.zeros_like(a)
    for i, w in enumerate(k):
        out2 += w * p[i:i + a.shape[0], :]
    return out2


def to_array(img):
    return np.asarray(img.convert("RGB"), dtype=np.float32)


def analyze(photo, focus_boxes=None, rng_seed=0):
    """photo: PIL RGB ya al tamaño de la rejilla de trabajo (pw x ph)."""
    rgb = to_array(photo)
    h, w, _ = rgb.shape
    lum = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]

    # --- gradiente (Sobel) sobre luminancia suavizada
    sm = gaussian(lum, 1.0)
    p = np.pad(sm, 1, mode="edge")
    gx = (p[0:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[0:-2, 0:-2] + 2 * p[1:-1, 0:-2] + p[2:, 0:-2])
    gy = (p[2:, 0:-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[0:-2, 0:-2] + 2 * p[0:-2, 1:-1] + p[0:-2, 2:])
    mag = np.hypot(gx, gy)
    edge = np.clip(mag / (np.percentile(mag, 98) + 1e-6), 0, 1)

    # --- tensor de estructura -> orientación dominante y coherencia
    jxx, jyy, jxy = gaussian(gx * gx, 2.5), gaussian(gy * gy, 2.5), gaussian(gx * gy, 2.5)
    # ángulo del gradiente dominante; el trazo va a 90 grados
    ang_grad = 0.5 * np.arctan2(2 * jxy, jxx - jyy)
    theta = ang_grad + np.pi / 2
    tr = jxx + jyy
    disc = np.sqrt(np.maximum((jxx - jyy) ** 2 + 4 * jxy ** 2, 0))
    coherence = np.where(tr > 1e-6, disc / (tr + 1e-6), 0.0)
    # donde el borde es débil la orientación no es fiable
    coherence = coherence * np.clip(gaussian(edge, 2.0) * 3.0, 0, 1)

    # --- piel (regla clásica en RGB, tolerante: piel oscura y clara)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    skin = ((r > 60) & (r > g) & (g > b * 0.9) & ((r - b) > 15) & ((r - g) > 5) & (r < 250) & (lum > 50) & (lum < 215))
    skin = gaussian(skin.astype(np.float32), 2.0)

    dark = np.clip((95 - gaussian(lum, 1.0)) / 95, 0, 1)

    # --- importancia: contraste local alto dentro/junto a la piel, más peso al centro-alto
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w * 0.5, h * 0.52
    centre = np.exp(-(((xx - cx) / (0.42 * w)) ** 2 + ((yy - cy) / (0.45 * h)) ** 2))
    local_contrast = np.clip(gaussian(edge, 3.0) * 2.2, 0, 1)
    near_skin = gaussian(skin, 6.0)
    # "ojo": zona oscura pequeña rodeada de piel
    eye_like = np.clip(dark * 1.2 - gaussian(dark, 7.0) * 0.9, 0, 1) * np.clip(near_skin * 2.0, 0, 1)
    eye_like = gaussian(eye_like, 1.5)
    eye_like = eye_like / (eye_like.max() + 1e-6)
    importance = np.clip(0.30 * centre + 0.30 * near_skin + 0.25 * local_contrast + 0.55 * eye_like, 0, 1)
    importance = importance / (importance.max() + 1e-6)
    if focus_boxes:
        for bx in focus_boxes:                       # [x0, y0, x1, y1] en fracciones 0..1 de la foto
            x0, y0, x1, y1 = int(bx[0] * w), int(bx[1] * h), int(bx[2] * w), int(bx[3] * h)
            importance[y0:y1, x0:x1] = 1.0
    return dict(rgb=rgb, lum=lum, edge=edge, theta=theta, coherence=np.clip(coherence, 0, 1),
                skin=skin, dark=dark, importance=importance, eye_like=eye_like)


def to_image(arr01, mode="L"):
    return Image.fromarray(np.clip(arr01 * 255, 0, 255).astype(np.uint8), mode)
