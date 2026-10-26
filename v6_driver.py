# -*- coding: utf-8 -*-
"""
AutoPainter V6 - motor de interacción con MediBang Paint (teclado + ratón).

Reutiliza de V5: Driver (selección de color por la interfaz, esc(), release()), Geometry.
Añade: trayectorias curvas, tamaño de pincel, capas, ventana en primer plano, verificación por captura.

QUÉ SE PUEDE Y QUÉ NO (limitaciones reales, no suposiciones):
  * Color: igual que V5 (clic en selector, escribir #RRGGBB, confirmar). Ya funcionaba en tu instalación.
  * Trayectorias: pyautogui.moveTo con puntos intermedios sobre una curva Catmull-Rom. El ratón no tiene
    presión: el grosor es constante dentro de un trazo (la variedad viene de varios tamaños de pincel).
  * Tamaño de pincel: modo "manual" (el programa se detiene y te pide ponerlo; fiable) o modo "input"
    (clic en la casilla numérica de tamaño, Ctrl+A, escribir, Enter; requiere calibrar el punto).
    No se asumen atajos de teclado de MediBang porque varían según versión/configuración.
  * Capas: modo "auto" (clic en el botón "capa nueva" que calibras; se verifica porque el PANEL DE CAPAS
    cambia en la captura), "manual" (el programa te pide crearla/seleccionarla) o "off" (una sola capa,
    sin fingir capas). El programa NO puede leer el nombre de la capa activa de MediBang.
  * Verificación: captura del lienzo cada lote de trazos y comparación del color en el centro de los
    últimos trazos. Es una verificación visual indirecta: confirma que "algo del color esperado apareció
    donde debía", no que esté en la capa correcta.
  * Ventana: en Windows se lee el título de la ventana en primer plano (ctypes); en otros sistemas no se
    puede y la comprobación queda como 'desconocida'.
"""
import math, time
import run_painter_strokes_v5 as v5

V6_DEFAULTS = {
    "WINDOW_TITLE": "MediBang",      # fragmento del título de la ventana
    "REQUIRE_WINDOW": True,          # parar si la ventana en primer plano no es MediBang (si se puede saber)
    "BATCH": 40,                     # trazos por lote de verificación/checkpoint
    "VERIFY": False,
    "VERIFY_TOL": 20.0,              # dE tolerado entre color esperado y capturado (trazos >= 3 px de pantalla)
    "VERIFY_TOL_THIN": 28.0,
    "RECHECKS": 2,                   # reintentos de la captura antes de dar un lote por NO verificado (-> parada)
    "WEAK_TOL_FACTOR": 2.0,          # nivel 'debil': la zona cambió Y >=50 % de muestras a menos de FACTOR x tolerancia del color esperado
    "MANUAL_RETURN_S": 10,           # segundos de cuenta atrás tras cada pausa manual (capa/tamaño) para volver a MediBang (0 = sin cuenta atrás)
    "LAYER_MODE": "manual",          # auto | manual | off
    "BRUSH_SIZE_MODE": "manual",     # manual | input
    "TOOL_HOTKEY": None,             # p. ej. "b" si en tu MediBang selecciona el pincel (opcional)
    "SAVE_HOTKEY": "ctrl+s",         # guardar el documento nativo (solo si ya lo guardaste una vez con nombre)
    "UNDO_HOTKEY": "ctrl+z",         # solo se usa en el diagnóstico
    "LAYER_DELAY": 0.6,
    "SIZE_DELAY": 0.25,
    "CURVE_STEP_PX": 6,              # separación de puntos del ratón sobre curvas (px de pantalla)
}


class UnsafeCondition(Exception):
    """Condición insegura: se detiene la pintura de forma controlada (el botón del ratón ya está suelto)."""


# ---------------------------------------------------------------------------------------------
def smooth_path(P, step):
    """Catmull-Rom sobre puntos de pantalla -> lista de puntos enteros separados <= step px."""
    if len(P) == 1:
        return [(round(P[0][0]), round(P[0][1]))]
    ext = [P[0]] + list(P) + [P[-1]]
    out = [P[0]]
    for i in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        seg = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        n = max(1, math.ceil(seg / step))
        for k in range(1, n + 1):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2 +
                                    (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3) for j in (0, 1)))
    res = []
    for x, y in out:
        q = (round(x), round(y))
        if not res or res[-1] != q:
            res.append(q)
    return res


def foreground_title():
    """Título de la ventana en primer plano (solo Windows). None = no se puede saber."""
    try:
        import ctypes
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return buf.value
    except Exception:
        return None


def mean_patch(img, x, y, r=1):
    px = img.load()
    w, h = img.size
    xs = range(max(0, x - r), min(w, x + r + 1))
    ys = range(max(0, y - r), min(h, y + r + 1))
    n = len(xs) * len(ys)
    if n == 0:
        return None
    s = [0, 0, 0]
    for yy in ys:
        for xx in xs:
            p = px[xx, yy]
            for c in range(3):
                s[c] += p[c]
    return tuple(c / n for c in s)


class Driver6(v5.Driver):
    def __init__(self, geo, cfg, picker, color_input, confirm, v6cfg=None, spots=None, grab=None, title_fn=None, say=print, ask=input):
        super().__init__(geo, cfg, picker, color_input, confirm)
        self.v6 = dict(V6_DEFAULTS)
        self.v6.update(v6cfg or {})
        self.spots = spots or {}                 # new_layer_button, layer_panel_region, brush_size_input
        self._grab = grab
        self._title = title_fn or foreground_title
        self.say, self.ask = say, ask
        self.layers_created = 0

    # ---------------------------------------------------------------- seguridad
    def window_state(self):
        """('ok'|'bad'|'unknown', título)"""
        t = self._title()
        if t is None:
            return "unknown", None
        return ("ok" if self.v6["WINDOW_TITLE"].lower() in t.lower() else "bad"), t

    def check_window(self):
        st, t = self.window_state()
        if st == "bad" and self.v6["REQUIRE_WINDOW"]:
            raise UnsafeCondition(f"La ventana en primer plano no es MediBang (es '{t}').")
        return st

    def grab_canvas(self):
        g = self.geo
        if self._grab is not None:
            return self._grab((g.left, g.top, g.left + g.width, g.top + g.height))
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=(g.left, g.top, g.left + g.width, g.top + g.height))

    def grab_region(self, region):
        l, t, w, h = region
        if self._grab is not None:
            return self._grab((l, t, l + w, t + h))
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=(l, t, l + w, t + h))

    def canvas_guard(self, photo_rect):
        """Los márgenes blancos (fuera de la foto) deben seguir blancos: si no, cambió el zoom, el documento
        o hay otra cosa encima. Devuelve (ok|None, detalle). None = no hay margen que comprobar."""
        ox, oy, rw, rh = photo_rect
        geo = self.geo
        strips = []
        if ox > 8:
            strips.append((0, 0, ox - 4, geo.ch))
            strips.append((ox + rw + 4, 0, geo.cw - (ox + rw + 4), geo.ch))
        if oy > 8:
            strips.append((0, 0, geo.cw, oy - 4))
            strips.append((0, oy + rh + 4, geo.cw, geo.ch - (oy + rh + 4)))
        strips = [s for s in strips if s[2] > 4 and s[3] > 4]
        if not strips:
            return None, "la foto ocupa todo el lienzo: no hay margen para comprobar"
        img = self.grab_canvas()
        bad = 0
        total = 0
        for (x, y, w, h) in strips:
            for fx in (0.25, 0.75):
                for fy in (0.25, 0.75):
                    sx, sy = round((x + w * fx) * geo.sx), round((y + h * fy) * geo.sy)
                    p = mean_patch(img, sx, sy)
                    total += 1
                    if p is None or min(p) < 235:
                        bad += 1
        return bad == 0, f"{total - bad}/{total} muestras de margen son blancas"

    # ---------------------------------------------------------------- herramientas, tamaño, capas
    def select_tool(self):
        k = self.v6["TOOL_HOTKEY"]
        if k:
            self.pg.hotkey(*k.split("+"))
            time.sleep(0.15)

    def set_brush_size(self, px):
        mode = self.v6["BRUSH_SIZE_MODE"]
        shown = f"{px:g}"
        if mode == "input" and self.spots.get("brush_size_input"):
            pg = self.pg
            pg.click(*self.spots["brush_size_input"]); time.sleep(self.v6["SIZE_DELAY"])
            pg.hotkey("ctrl", "a"); time.sleep(0.05)
            pg.write(shown, interval=.01)
            pg.press("enter")
            time.sleep(self.v6["SIZE_DELAY"])
            return "auto"
        self.say(f"\n>>> Pon el tamaño del pincel en {shown} px en MediBang.")
        self.ask("    Pulsa ENTER cuando esté puesto... ")
        self._return_countdown()
        return "manual"

    def _return_countdown(self):
        for s in range(int(self.v6.get("MANUAL_RETURN_S", 10)), 0, -1):
            self.say(f"    Volviendo a MediBang: {s} segundos...")
            time.sleep(1)

    def _panel_signature(self):
        reg = self.spots.get("layer_panel_region")
        if not reg:
            return None
        img = self.grab_region(reg)
        small = img.convert("L").resize((24, 24))
        return list(small.getdata())

    def new_layer(self, name):
        """Crea la capa de la etapa. Devuelve 'auto-verificada', 'auto-sin-verificar', 'manual' u 'off'."""
        mode = self.v6["LAYER_MODE"]
        if mode == "off":
            return "off"
        if mode == "auto" and self.spots.get("new_layer_button"):
            before = self._panel_signature()
            self.pg.click(*self.spots["new_layer_button"])
            time.sleep(self.v6["LAYER_DELAY"])
            after = self._panel_signature()
            self.layers_created += 1
            if before is None or after is None:
                return "auto-sin-verificar"
            diff = sum(abs(a - b) for a, b in zip(before, after)) / len(before)
            if diff < 1.0:
                raise UnsafeCondition(f"El panel de capas no cambió tras pulsar 'capa nueva' (diferencia {diff:.2f}).")
            return "auto-verificada"
        self.say(f"\n>>> Crea una capa NUEVA en MediBang y déjala seleccionada. Nombre sugerido: '{name}'.")
        self.ask("    Pulsa ENTER cuando la capa esté seleccionada... ")
        self._return_countdown()
        return "manual"

    def save_document(self):
        k = self.v6["SAVE_HOTKEY"]
        if not k:
            return False
        self.pg.hotkey(*k.split("+"))
        time.sleep(0.8)
        return True

    # ---------------------------------------------------------------- trazos
    def screen_path(self, pts):
        g = self.geo
        P = []
        for x, y in pts:
            sx, sy = g.screen(x, y)
            sx = min(max(sx, g.left), g.left + g.width)       # nunca fuera del lienzo
            sy = min(max(sy, g.top), g.top + g.height)
            P.append((sx, sy))
        return P

    def stroke_path(self, pts, size=None):
        pg, c = self.pg, self.cfg
        P = self.screen_path(pts)
        path = smooth_path(P, self.v6["CURVE_STEP_PX"]) if len(P) > 1 else [P[0]]
        if len(path) < 2:                                     # trazo de un punto: segmento corto, nunca se omite
            x, y = path[0]
            path = [(x - c["DOT_PX"] // 2, y), (x + (c["DOT_PX"] + 1) // 2, y)]
        if self.esc():
            raise KeyboardInterrupt
        pg.moveTo(*path[0])
        pg.mouseDown()
        try:
            time.sleep(c["DOWN_DELAY"])
            for i, (x, y) in enumerate(path[1:], 1):
                pg.moveTo(x, y)
                time.sleep(c["STEP_DELAY"])
                if i % 8 == 0 and self.esc():
                    raise KeyboardInterrupt
            time.sleep(c["END_HOLD"])
        finally:
            pg.mouseUp()

    def release(self):
        for b in ("left", "right", "middle"):
            try:
                self.pg.mouseUp(button=b)
            except Exception:
                pass

    # ---------------------------------------------------------------- verificación
    # ---- muestreo: SIN cambios para trazos gruesos (>= 3 px de pantalla): media 3x3 en el punto medio, igual que antes.
    #      Solo para trazos finos (< 3 px) se acepta el mejor de los 9 píxeles vecinos: un trazo de 1,9-2,7 px no cubre un
    #      píxel entero y MediBang suaviza sus bordes, así que un solo píxel depende del redondeo de +-1 px.
    def _sample_colors(self, img, sx, sy, thin):
        if img is None:
            return []
        w, h = img.size
        px = img.load()
        if not thin:
            m = mean_patch(img, sx, sy, r=1)
            return [] if m is None else [tuple(round(c) for c in m)]
        out = []
        for yy in range(sy - 1, sy + 2):
            for xx in range(sx - 1, sx + 2):
                if 0 <= xx < w and 0 <= yy < h:
                    out.append(tuple(px[xx, yy][:3]))
        return out

    def _min_de(self, img, sx, sy, color, thin):
        cs = self._sample_colors(img, sx, sy, thin)
        return min((v5.dE(c, tuple(color)) for c in cs), default=None)

    def _zone_diff(self, prev_img, img, xs, ys):
        g = self.geo
        if prev_img is None or not xs:
            return None
        box = (max(0, int(min(xs)) - 2), max(0, int(min(ys)) - 2), min(g.width, int(max(xs)) + 3), min(g.height, int(max(ys)) + 3))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        a, b = prev_img.crop(box).convert("L"), img.crop(box).convert("L")
        return sum(abs(p - q) for p, q in zip(a.getdata(), b.getdata())) / max(1, (box[2] - box[0]) * (box[3] - box[1]))

    def verify_batch(self, batch, prev_img):
        """batch: lista de (pts, color, size). Devuelve (nivel, imagen_actual, detalle).
        Se miran los últimos 8 trazos, un punto (el medio del recorrido) por trazo, y se compara con su color.
        Niveles:
          'fuerte'   >= 50 % de las muestras muestran el color esperado y ese color NO estaba ya ahí (el trazo cambió la pantalla)
          'presente' >= 50 % lo muestran, pero ya estaba (al reanudar se repintan trazos ya hechos / mismo color sobre mismo color):
                     no se puede distinguir 'pintado ahora' de 'ya estaba'
          'debil'    el color no llega a la tolerancia, pero la zona cambió Y >= 50 % de las muestras están a menos de
                     WEAK_TOL_FACTOR x tolerancia (un color claramente equivocado YA NO se acepta por el solo hecho de que algo cambie)
          'ninguno'  en otro caso: NO se confirma y se deja diagnóstico (self.last_diagnosis)"""
        self.last_debug, self.last_diagnosis = [], None
        if not self.v6["VERIFY"] or not batch:
            return "omitida", prev_img, "verificación desactivada"
        img = self.grab_canvas()
        g = self.geo
        checked = changed = present = near = 0
        xs, ys = [], []
        for k, (pts, color, size) in enumerate(batch[-8:]):
            wscreen = size * g.sx
            for (px_, py_) in pts:
                xs.append(px_ * g.sx); ys.append(py_ * g.sy)
            if wscreen < 1.5:
                continue
            thin = wscreen < 3
            tol = self.v6["VERIFY_TOL"] if not thin else self.v6["VERIFY_TOL_THIN"]
            x, y = pts[len(pts) // 2]
            sx, sy = round(x * g.sx), round(y * g.sy)
            da = self._min_de(img, sx, sy, color, thin)
            if da is None:
                continue
            db = self._min_de(prev_img, sx, sy, color, thin)
            checked += 1
            ok = da <= tol
            if da <= tol * float(self.v6["WEAK_TOL_FACTOR"]):
                near += 1
            if ok:
                if db is None or db > tol:
                    changed += 1
                else:
                    present += 1
            self.last_debug.append(dict(stroke=k, pos=(sx, sy), color=tuple(color), width_screen=round(wscreen, 1), thin=thin, tol=tol,
                                        de_after=round(da, 1), de_before=None if db is None else round(db, 1), ok=ok))
        need = max(1, math.ceil(checked * 0.5)) if checked else 1
        ok_total = changed + present
        if checked and ok_total >= need:
            level = "fuerte" if changed >= need else "presente"
            return level, img, f"{ok_total}/{checked} muestras con el color esperado ({changed} cambiaron la pantalla, {present} ya estaban)"
        dd = self._zone_diff(prev_img, img, xs, ys)
        if checked and dd is not None and dd > 0.8 and near >= need:
            return "debil", img, f"color no concluyente ({ok_total}/{checked}; {near} cerca) pero la zona cambió (dif {dd:.1f})"
        self.last_diagnosis = self.diagnose(img, prev_img, dd)
        dtxt = "sin comparación previa" if dd is None else f"cambio de zona {dd:.1f}"
        return "ninguno", img, f"{ok_total}/{checked} muestras con el color esperado (hacían falta {need}), {dtxt}. {self.last_diagnosis['texto']}"

    def diagnose(self, img, prev_img, zone_diff, window=12):
        """Clasifica POR QUÉ falló la verificación, para no tener que adivinar:
          sin_cambios      la zona no cambió: los trazos no se están pintando (capa oculta/bloqueada, herramienta o pincel distintos, ventana sin foco)
          desplazado       el color esperado aparece, de forma consistente en varias muestras, a (dx,dy) px de donde debía (lienzo movido o con otro zoom).
                           Solo se afirma si hay POCOS desplazamientos candidatos; con colores uniformes cualquier desplazamiento encaja y no se concluye.
          color_distinto   la zona cambió pero con otro color (¿color mal seleccionado?) -> se indica el color observado
          indeterminado    ninguna de las anteriores"""
        bad = [r for r in self.last_debug if not r["ok"]]
        obs = []
        votes = {}
        for r in bad:
            sx, sy = r["pos"]
            cs = self._sample_colors(img, sx, sy, r["thin"])
            if cs:
                obs.append(min(cs, key=lambda c: v5.dE(c, r["color"])))
            for dy in range(-window, window + 1, 2):
                for dx in range(-window, window + 1, 2):
                    if dx == 0 and dy == 0:
                        continue
                    cs2 = self._sample_colors(img, sx + dx, sy + dy, r["thin"])
                    if cs2 and min(v5.dE(c, r["color"]) for c in cs2) <= r["tol"]:
                        votes[(dx, dy)] = votes.get((dx, dy), 0) + 1
        need = max(2, math.ceil(len(bad) * 0.5))
        winners = sorted([o for o, v in votes.items() if v >= need], key=lambda o: (-votes[o], abs(o[0]) + abs(o[1])))
        comps, seen = 0, set()                            # componentes conexas (vecinos a <= 4 px): un solo bloque = un único desplazamiento coherente
        for o in winners:
            if o in seen:
                continue
            comps += 1
            stack = [o]; seen.add(o)
            while stack:
                cur = stack.pop()
                for q in winners:
                    if q not in seen and abs(q[0] - cur[0]) <= 4 and abs(q[1] - cur[1]) <= 4:
                        seen.add(q); stack.append(q)
        if comps == 1 and len(winners) <= 40:     # un único desplazamiento coherente y no un color uniforme
            dx, dy = winners[0]
            kind, txt = "desplazado", f"DIAGNÓSTICO: el color esperado aparece a ~({dx},{dy}) px de donde debía en {votes[winners[0]]}/{len(bad)} muestras: ¿lienzo movido o con otro zoom?"
        elif zone_diff is not None and zone_diff <= 0.8:
            kind, txt = "sin_cambios", "DIAGNÓSTICO: la zona no cambió; los trazos no se ven en pantalla (¿capa oculta o bloqueada, herramienta/pincel distintos, opacidad, MediBang sin foco?)"
        elif zone_diff is not None and zone_diff > 0.8 and obs:
            med = tuple(sorted(c[i] for c in obs)[len(obs) // 2] for i in range(3))
            kind, txt = "color_distinto", f"DIAGNÓSTICO: la zona cambió pero con otro color (mediana observada {med}, esperado {self.last_debug[0]['color'] if self.last_debug else '?'}): ¿color mal seleccionado?"
        else:
            kind, txt = "indeterminado", "DIAGNÓSTICO: indeterminado; revisa la captura guardada"
        return dict(kind=kind, texto=txt, zone_diff=zone_diff, observados=obs[:8], desplazamiento=winners[0] if kind == "desplazado" else None)

    def probe_brush_width(self, size_px, y_canvas):
        """Diagnóstico: dibuja una línea horizontal y mide su grosor en pantalla (compara con size*sy)."""
        g = self.geo
        x0, x1 = g.cw * 0.40, g.cw * 0.60
        self.stroke_path([(x0, y_canvas), (x1, y_canvas)], size_px)
        time.sleep(0.2)
        img = self.grab_canvas()
        sx, sy = round(g.cw * 0.5 * g.sx), round(y_canvas * g.sy)
        px = img.load()
        up = dn = 0
        base = px[sx, max(0, sy - int(size_px * g.sy) - 12)]
        def dark(p):
            return sum(abs(p[i] - base[i]) for i in range(3)) > 120
        while sy - up - 1 >= 0 and dark(px[sx, sy - up - 1]):
            up += 1
        while sy + dn + 1 < img.height and dark(px[sx, sy + dn + 1]):
            dn += 1
        return up + dn + 1, size_px * g.sy
