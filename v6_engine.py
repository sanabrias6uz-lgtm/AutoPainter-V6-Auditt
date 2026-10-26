# -*- coding: utf-8 -*-
"""
AutoPainter V6 - ejecución del plan (por lotes verificados con checkpoint), tiempos y prueba de región.
"""
import math, time, json
import run_painter_strokes_v5 as v5
from v6_driver import UnsafeCondition
from v6_planner import Plan6, StagePlan, Group

TIMING_FILE = "timing_v6.json"
ASSUMED = dict(color_s=1.00, stroke_a=0.060, stroke_b=0.00050, verify_s=0.08, layer_s=1.5, size_s=0.8, save_s=1.5,
               source="SUPUESTO (sin medir): haz la prueba de región")


def load_timing(path=TIMING_FILE):
    try:
        with open(path, "r", encoding="utf-8") as f:
            t = json.load(f)
        if all(k in t for k in ("color_s", "stroke_a", "stroke_b")):
            return {**ASSUMED, **t}
    except (OSError, ValueError):
        pass
    return dict(ASSUMED)


def stroke_screen_len(pts, geo):
    return sum(math.hypot((pts[k + 1][0] - pts[k][0]) * geo.sx, (pts[k + 1][1] - pts[k][1]) * geo.sy) for k in range(len(pts) - 1))


def plan_metrics(plan, geo):
    lens, size_changes, prev = [], 0, None
    for i, si, size, col, pts in plan.flat():
        lens.append(stroke_screen_len(pts, geo))
        if size != prev:
            size_changes += 1; prev = size
    return lens, size_changes


def estimate_seconds6(plan, timing, geo, batch=40, manual_size_s=0.0, metrics=None):
    lens, size_changes = metrics or plan_metrics(plan, geo)
    t = sum(timing["stroke_a"] + timing["stroke_b"] * L for L in lens)
    t += plan.n_color_changes * timing["color_s"]
    t += len(plan.stages) * (timing["layer_s"] + timing["save_s"])
    t += size_changes * (timing["size_s"] if manual_size_s == 0 else manual_size_s)
    t += math.ceil(plan.n_strokes / batch) * timing["verify_s"]
    return t


def fmt_t(sec):
    return v5.fmt_t(sec)


def region_plan(plan, rect):
    """Subplan con solo los trazos cuyos puntos están TODOS dentro de rect (x0,y0,x1,y1 en px de lienzo)."""
    x0, y0, x1, y1 = rect
    stages = []
    for st in plan.stages:
        groups = []
        for g in st.groups:
            keep = [p for p in g.strokes if all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in p)]
            if keep:
                groups.append(Group(g.size, g.color, keep))
        stages.append(StagePlan(st.key, st.layer, groups))
    meta = dict(plan.meta); meta["region"] = list(rect)
    return Plan6(stages, meta)


class NullCheckpoint:
    """Para la prueba de región: misma interfaz, no escribe nada."""
    class _D(dict):
        pass
    def __init__(self):
        self.data = dict(layers_ready=[], stages_done=[], errors=[], verification=dict(fuerte=0, debil=0, ninguno=0, omitida=0))
    def attempted(self, i): pass
    def confirm(self, i, level, elapsed): self.data["verification"][level] = self.data["verification"].get(level, 0) + 1
    def error(self, msg, index): self.data["errors"].append((index, str(msg)))
    def stage_started(self, si, r): pass
    def stage_done(self, si, saved): pass
    def log(self, m): pass
    def finish(self, e): pass


class Executor:
    def __init__(self, drv, plan, ckpt, photo_rect=None, say=print, ask=input, clock=time.time):
        self.drv, self.plan, self.ckpt, self.photo_rect = drv, plan, ckpt, photo_rect
        self.say, self.ask, self.clock = say, ask, clock
        self.samples, self.color_times, self.verify_times, self.layer_times, self.save_times, self.size_times = [], [], [], [], [], []
        self.done_session = 0
        self.stopped = None

    # ------------------------------------------------------------------
    def _verify(self, batch, prev_img):
        drv = self.drv
        for k in range(1 + int(drv.v6["RECHECKS"])):
            t = time.perf_counter()
            level, img, detail = drv.verify_batch(batch, prev_img)
            self.verify_times.append(time.perf_counter() - t)
            if level != "ninguno":
                return level, img, detail
            time.sleep(0.4)                                  # la captura puede ir un poco retrasada
        return "ninguno", img, detail

    def _dump_failure(self, img, first_idx, last_idx):
        """Guarda una captura con las muestras (rojo = no coincide) y deja en el log lo esperado frente a lo visto."""
        try:
            import os
            from PIL import ImageDraw
            folder = getattr(self.ckpt, "dir", None)
            if not folder or img is None:
                return
            os.makedirs(folder, exist_ok=True)
            im = img.copy(); d = ImageDraw.Draw(im)
            for r in getattr(self.drv, "last_debug", []):
                x, y = r["pos"]
                d.ellipse([x - 9, y - 9, x + 9, y + 9], outline=(0, 200, 0) if r["ok"] else (255, 0, 0), width=2)
                self.ckpt.log(f"  muestra trazo#{r['stroke']} pos={r['pos']} esperado={r['color']} ancho={r['width_screen']}px dE_despues={r['de_after']} dE_antes={r['de_before']} {'OK' if r['ok'] else 'NO'}")
            diag = getattr(self.drv, "last_diagnosis", None)
            if diag:
                self.ckpt.log("  " + diag["texto"])
            path = os.path.join(folder, f"verif_fallo_{first_idx}.png")
            im.save(path)
            self.say(f"  Captura de diagnóstico: {path} (círculos rojos = muestras que no coinciden)")
        except Exception as ex_:
            self.say(f"  (no se pudo guardar el diagnóstico: {ex_!r})")

    def resume_probe(self, flat, start, n=12):
        """Antes de repintar nada al reanudar: ¿se ven en pantalla las últimas pinceladas YA confirmadas?
        Devuelve (ok|None, detalle). Distingue 'el documento/capa no es el del checkpoint' de 'fallan los trazos nuevos'."""
        g = self.drv.geo
        prev = [f for f in flat if f[0] < start][-n:]
        if not prev:
            return None, "no hay trazos previos que comprobar"
        img = self.drv.grab_canvas()
        checked = ok = 0
        for (i, si, size, color, pts) in prev:
            w = size * g.sx
            if w < 1.5:
                continue
            thin = w < 3
            x, y = pts[len(pts) // 2]
            da = self.drv._min_de(img, round(x * g.sx), round(y * g.sy), color, thin)
            if da is None:
                continue
            checked += 1
            ok += da <= (self.drv.v6["VERIFY_TOL"] if not thin else self.drv.v6["VERIFY_TOL_THIN"])
        if not checked:
            return None, "los trazos previos son demasiado finos para comprobarlos"
        return ok >= max(1, math.ceil(checked * 0.5)), f"{ok}/{checked} de las últimas pinceladas confirmadas se ven en pantalla"

    def _flush(self, batch, first_idx, last_idx, t0, base_elapsed, prev_img):
        """Verifica el lote y, solo si se verifica, confirma sus trazos en el checkpoint."""
        if not batch:
            return prev_img
        level, img, detail = self._verify(batch, prev_img)
        if level == "ninguno":
            self.ckpt.error(f"Lote {first_idx}-{last_idx} NO verificado: {detail}", last_idx)
            self._dump_failure(img, first_idx, last_idx)
            raise UnsafeCondition(f"No se pudo verificar el lote de trazos {first_idx}-{last_idx}: {detail}")
        self.ckpt.confirm(last_idx + 1, level, base_elapsed + (self.clock() - t0))
        batch.clear()
        st = self.drv.check_window()
        return img

    def run(self, start=0):
        drv, plan, ck = self.drv, self.plan, self.ckpt
        flat = list(plan.flat())
        total = len(flat)
        stage_first = {}
        for i, si, *_ in flat:
            stage_first.setdefault(si, i)
        base_elapsed = float(ck.data.get("elapsed_s", 0.0)) if hasattr(ck, "data") else 0.0
        t0 = self.clock()
        cur_stage = cur_size = cur_color = None
        batch, batch_first = [], None
        prev_img = None
        last_print = 0.0
        B = int(drv.v6["BATCH"])
        try:
            drv.check_window()
            if self.photo_rect:
                ok, det = drv.canvas_guard(self.photo_rect)
                if ok is False:
                    raise UnsafeCondition(f"Los márgenes del lienzo no están en blanco ({det}). ¿Zoom/documento distinto?")
                self.say(f"  comprobación de lienzo: {det}")
            if start > 0 and drv.v6["VERIFY"]:
                pok, pdet = self.resume_probe(flat, start)
                self.say(f"  comprobación al reanudar: {pdet}")
                if pok is False:
                    self.say("  Las últimas pinceladas confirmadas NO se ven donde deberían (documento o capa distintos, capa oculta, lienzo movido).")
                    if str(self.ask("  ¿Continuar igualmente? (s/N): ")).strip().lower() != "s":
                        raise UnsafeCondition("Al reanudar, las últimas pinceladas confirmadas no se ven en pantalla; no se pinta nada. " + pdet)
            prev_img = drv.grab_canvas() if drv.v6["VERIFY"] else None
            drv.select_tool()
            for (i, si, size, color, pts) in flat:
                if i < start:
                    continue
                if si != cur_stage:
                    prev_img = self._flush(batch, batch_first, i - 1, t0, base_elapsed, prev_img) if batch else prev_img
                    if cur_stage is not None:
                        t = time.perf_counter(); saved = drv.save_document(); self.save_times.append(time.perf_counter() - t)
                        ck.stage_done(cur_stage, saved)
                    st = plan.stages[si]
                    fresh = (i == stage_first[si]) or (si not in ck.data.get("layers_ready", []))
                    self.say(f"\n=== ETAPA {si + 1}/{len(plan.stages)}: {st.key} ({st.n_strokes} trazos, capa '{st.layer}') ===")
                    t = time.perf_counter()
                    if fresh:
                        res = drv.new_layer(st.layer)
                    else:
                        self.say(f">>> Reanudando a mitad de la etapa: selecciona la capa '{st.layer}' en MediBang.")
                        self.ask("    Pulsa ENTER cuando esté seleccionada... "); res = "reanudada-manual"
                    self.layer_times.append(time.perf_counter() - t)
                    ck.stage_started(si, res)
                    cur_stage, cur_size, cur_color = si, None, None
                    prev_img = drv.grab_canvas() if drv.v6["VERIFY"] else None
                if size != cur_size:
                    prev_img = self._flush(batch, batch_first, i - 1, t0, base_elapsed, prev_img) if batch else prev_img
                    t = time.perf_counter(); drv.set_brush_size(size); self.size_times.append(time.perf_counter() - t)
                    cur_size = size
                    prev_img = drv.grab_canvas() if drv.v6["VERIFY"] else None
                if color != cur_color:
                    t = time.perf_counter(); drv.select_color(color); self.color_times.append(time.perf_counter() - t)
                    cur_color = color
                if not batch:
                    batch_first = i
                t = time.perf_counter()
                drv.stroke_path(pts, size)
                self.samples.append((stroke_screen_len(pts, drv.geo), time.perf_counter() - t))
                ck.attempted(i + 1)
                batch.append((pts, color, size))
                self.done_session += 1
                if len(batch) >= B:
                    prev_img = self._flush(batch, batch_first, i, t0, base_elapsed, prev_img)
                    now = self.clock()
                    if now - last_print >= 20:
                        last_print = now
                        el = now - t0
                        left = el / max(1, self.done_session) * (total - i - 1)
                        self.say(f"  {i + 1}/{total} trazos | sesión {fmt_t(el)} | restante ~ {fmt_t(left)} | verif. {ck.data['verification']}")
            prev_img = self._flush(batch, batch_first, total - 1, t0, base_elapsed, prev_img) if batch else prev_img
            if cur_stage is not None:
                t = time.perf_counter(); saved = drv.save_document(); self.save_times.append(time.perf_counter() - t)
                ck.stage_done(cur_stage, saved)
            ck.finish(base_elapsed + (self.clock() - t0))
            self.stopped = None
            return True
        except KeyboardInterrupt:
            self.stopped = "ESC"
            ck.log("Detenido con ESC")
            self.say("\nDetenido con ESC. Botón del ratón liberado. Se puede reanudar desde el último lote confirmado.")
            return False
        except UnsafeCondition as e:
            self.stopped = f"seguridad: {e}"
            ck.log("PARADA DE SEGURIDAD: " + str(e))
            self.say(f"\nPARADA DE SEGURIDAD: {e}")
            return False
        except Exception as e:                                          # p. ej. FailSafe de pyautogui
            self.stopped = f"error: {e!r}"
            try:
                ck.error(repr(e), self.done_session)
            except Exception:
                pass
            self.say(f"\nProceso detenido por un error: {e!r}")
            return False
        finally:
            drv.release()
            try:
                if hasattr(ck, "_write") and ck.data:
                    ck._write()
            except Exception:
                pass


def timing_from_executor(ex, previous=None):
    """Convierte las mediciones de un Executor en un diccionario de tiempos."""
    a, b = v5.fit_stroke_model(ex.samples) if ex.samples else (ASSUMED["stroke_a"], ASSUMED["stroke_b"])
    mean = lambda v, d: (sum(v) / len(v)) if v else d
    t = dict(previous or ASSUMED)
    t.update(color_s=mean(ex.color_times, t["color_s"]), stroke_a=a, stroke_b=b, verify_s=mean(ex.verify_times, t["verify_s"]),
             layer_s=mean(ex.layer_times, t["layer_s"]) if ex.drv.v6["LAYER_MODE"] == "auto" else t["layer_s"],
             save_s=mean(ex.save_times, t["save_s"]),
             size_s=mean(ex.size_times, t["size_s"]) if ex.drv.v6["BRUSH_SIZE_MODE"] == "input" else t["size_s"],
             source=f"medido ({len(ex.samples)} trazos, {len(ex.color_times)} cambios de color, {time.strftime('%Y-%m-%d %H:%M')})")
    return t
