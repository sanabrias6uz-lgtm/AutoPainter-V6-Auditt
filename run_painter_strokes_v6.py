# -*- coding: utf-8 -*-
"""
AutoPainter V6 para MediBang Paint  (evolución de V5; V5 se conserva sin cambios y es el respaldo estable).

Uso:   python run_painter_strokes_v6.py [--imagen FOTO] [--semilla N] [--preset N] [--solo-plan] [--comparar]
                                         [--region] [--reanudar] [--calibrar] [--diagnostico] [--motor v5]
Todo lo que no necesita ratón (plan, vista previa, comparación) funciona sin pyautogui ni keyboard.
"""
import json, os, sys, time, math, argparse
from PIL import Image
import run_painter_strokes_v5 as v5
import v6_planner as p6
import v6_engine as eng
import v6_checkpoint as ckm
import v6_compare as cmp_
from v6_driver import Driver6, V6_DEFAULTS, UnsafeCondition

CFG6 = dict(V6_DEFAULTS)
CFG6.update({
    "SEED": 1,                  # semilla reproducible del planificador
    "WORK_SCALE": 0.5,          # resolución interna del análisis (0.5 = mitad del lienzo)
    "MAX_MINUTES": 120,
    "MIN_MINUTES": 60,          # objetivo inferior: solo informativo
    "SAFETY_FACTOR": 1.25,
    "PRESET": None,             # None = el más fiel que quepa en MAX_MINUTES (con timing medido o SUPUESTO)
    "FOCUS_BOXES": None,        # [[x0,y0,x1,y1],...] fracciones 0..1 de la foto para forzar zonas importantes
    "PROGRESS_DIR": "v6_progress",
    "REGION_FRAC": [0.24, 0.26],
})
SETTINGS_V6 = "settings_v6.json"      # puntos calibrados SOLO de V6 (settings.txt no se toca)


# ------------------------------------------------------------------------------------------
def load_all_settings(path):
    settings = v5.load_settings(path)
    cfg = dict(CFG6)
    for k, v in settings.get("v6", {}).items():
        if k in cfg:
            cfg[k] = v
    spots = {}
    if os.path.exists(SETTINGS_V6):
        with open(SETTINGS_V6, "r", encoding="utf-8") as f:
            extra = json.load(f)
        spots = {k: (tuple(v) if isinstance(v, list) and k != "layer_panel_region" else v) for k, v in extra.get("spots", {}).items()}
        for k, v in extra.get("config", {}).items():
            if k in cfg:
                cfg[k] = v
    return settings, cfg, spots


def build(args, settings, cfg):
    a = settings["canvas_area"]
    area = (a["left"], a["top"], a["width"], a["height"])
    cres = (int(settings.get("canvas_width_resolution", 1800)), int(settings.get("canvas_height_resolution", 1200)))
    name, image = v5.pick_image(args.imagen)
    if image is None:
        sys.exit("No se encontró ninguna imagen compatible en esta carpeta.")
    geo = v5.Geometry(area, cres, 320, round(320 * cres[1] / cres[0]))     # misma geometría pantalla<->lienzo que V5
    scene = p6.Scene(image, cres, cfg["WORK_SCALE"], cfg["FOCUS_BOXES"], cfg["SEED"])
    return name, image, area, cres, geo, scene


def make_cfg_hash(cfg, area, cres, preset):
    return ckm.config_hash(dict(seed=cfg["SEED"], ws=cfg["WORK_SCALE"], area=area, cres=cres, preset=preset["name"],
                                focus=cfg["FOCUS_BOXES"], stages=p6.STAGES))


def choose_plan6(scene, geo, timing, cfg, say=print):
    idx = cfg["PRESET"]
    order = [idx] if idx is not None else range(len(p6.PRESETS6))
    limit = cfg["MAX_MINUTES"] * 60
    best = None
    for i in order:
        pr = p6.PRESETS6[i]
        t = time.time()
        plan, _ = p6.make_plan6(scene, pr, seed=cfg["SEED"])
        est = eng.estimate_seconds6(plan, timing, geo, cfg["BATCH"]) * cfg["SAFETY_FACTOR"]
        say(f"  preset {pr['name']:<14} {plan.n_strokes:>6} trazos, {plan.n_color_changes:>4} cambios de color -> ~ {eng.fmt_t(est)} (con margen)  [planificado en {time.time() - t:.0f}s]")
        best = (plan, est, est <= limit)
        if est <= limit:
            return best
    return best


def v5_plan_for_compare(image, area, cres, v5cfg, timing5):
    f = v5.Factory(image, area, cres, 256, v5cfg)
    plan, est, fits = v5.choose_plan(f, timing5, v5cfg, verbose=False)
    return plan, est, fits


def make_driver(geo, cfg, settings, spots, say=print):
    cfg5 = dict(v5.CFG)
    cfg5.update(settings.get("v5", {}))
    picker, cinput, confirm = (tuple(settings[k]) for k in ("color_picker_spot", "color_input_spot", "color_confirmation_spot"))
    v6cfg = {k: cfg[k] for k in V6_DEFAULTS}
    return Driver6(geo, cfg5, picker, cinput, confirm, v6cfg, spots, say=say)


def countdown(n=5):
    v5.countdown(n)


# ------------------------------------------------------------------------------------------
def report_plan(plan, est, fits, timing, cfg, geo):
    print(f"\nPlan V6 '{plan.meta['preset']}' (semilla {plan.meta['seed']}): {plan.n_strokes} trazos, {plan.n_color_changes} cambios de color, huella {plan.digest()}")
    for st in plan.stages:
        print(f"  {st.key:<12} capa '{st.layer}': {st.n_strokes:>6} trazos, {st.n_color_changes:>3} colores, pinceles {[f'{s:g}' for s in st.sizes]} px")
    print(f"  estimación: {eng.fmt_t(est)} con margen  [{timing['source']}]" + ("" if fits else f"   ¡SUPERA {cfg['MAX_MINUTES']} min!"))
    if "SUPUESTO" in timing["source"]:
        print("  AVISO: tiempos SUPUESTOS. Ejecuta la prueba de región (opción 2) para medir los reales antes de fiarte de la estimación.")
    elif est / cfg["SAFETY_FACTOR"] < cfg["MIN_MINUTES"] * 60:
        print(f"  Nota: la estimación está por debajo de {cfg['MIN_MINUTES']} min; puedes elegir un preset más fiel (--preset 0).")


def region_rect_for(scene, cfg):
    r = cmp_.focus_rects(scene, *cfg["REGION_FRAC"])["ojos"]
    return r


def run_region_test(drv, plan, scene, cfg, geo, timing):
    rect = region_rect_for(scene, cfg)
    sub = eng.region_plan(plan, rect)
    est_region = sub.n_strokes
    print(f"\nPRUEBA DE REGIÓN: rectángulo {rect} (px de lienzo) alrededor de los ojos")
    print(f"  {sub.n_strokes} trazos, {sub.n_color_changes} cambios de color, {len(sub.stages)} etapas ({sum(1 for s in sub.stages if s.n_strokes)} con trazos)")
    print("  Haz esto en un documento de PRUEBA (mismo tamaño de lienzo) o prepárate para deshacer / borrar las capas creadas.")
    input("  Pon el pincel de MediBang listo (redondo, dureza 100 %, opacidad 100 %) y pulsa ENTER... ")
    countdown(5)
    ck = eng.NullCheckpoint()
    ex = eng.Executor(drv, sub, ck, photo_rect=None)
    ok = ex.run(0)
    if not ex.samples:
        print("No se midió nada.")
        return None
    t = eng.timing_from_executor(ex, timing)
    with open(eng.TIMING_FILE, "w", encoding="utf-8") as f:
        json.dump(t, f, indent=1)
    print(f"\nMedido: color {t['color_s']:.2f}s | trazo {t['stroke_a'] * 1000:.1f} ms + {t['stroke_b'] * 1000:.3f} ms/px | verificación {t['verify_s'] * 1000:.0f} ms/lote | capa {t['layer_s']:.2f}s")
    print(f"        ({len(ex.samples)} trazos medidos; terminó {'bien' if ok else 'detenida: ' + str(ex.stopped)})")
    print(f"        verificaciones: {ck.data['verification']}")
    return t


def run_full(drv, plan, scene, cfg, geo, settings_name, image_sha, cfg_hash, resume=False):
    ck = ckm.Checkpoint(cfg["PROGRESS_DIR"])
    start = 0
    if resume:
        plan = ck.load()
        d = ck.data
        print(f"\nREANUDAR: {d['confirmed']}/{d['n_strokes']} trazos confirmados, etapa {d['stage'] + 1}, tiempo previo {eng.fmt_t(d['elapsed_s'])}")
        if not ck.compatible(image_sha, cfg_hash):
            if input("AVISO: la imagen o la configuración actuales no coinciden con las del plan guardado. ¿Reanudar con el plan GUARDADO igualmente? (s/N): ").strip().lower() != "s":
                return
        if d["finished"]:
            print("Ese plan ya estaba terminado."); return
        start = ck.resume_index
        print(f"  Se repintarán los trazos enviados pero no confirmados ({d['attempted'] - d['confirmed']}).")
        print("  El documento de MediBang debe ser el MISMO que se estaba pintando (con sus capas).")
    else:
        if ck.exists() and input("Hay un progreso guardado. ¿Sobrescribirlo con un plan nuevo? (s/N): ").strip().lower() != "s":
            print("Usa 'Reanudar' para continuar el anterior."); return
        ck.start_new(plan, settings_name, image_sha, cfg_hash)
    print("\nEn MediBang: pincel redondo, dureza 100 %, opacidad 100 %, estabilizador al mínimo, lienzo visible entero")
    print("y en la MISMA posición/zoom que en el setup. ESC detiene (el botón del ratón se libera siempre).")
    print(f"Capas: modo '{drv.v6['LAYER_MODE']}'. Tamaño de pincel: modo '{drv.v6['BRUSH_SIZE_MODE']}'.")
    input("Pulsa ENTER cuando MediBang esté listo y en primer plano... ")
    countdown(5)
    ex = eng.Executor(drv, plan, ck, photo_rect=plan.meta["photo_rect"])
    ok = ex.run(start)
    if ok:
        try:
            from PIL import ImageGrab
            img = drv.grab_canvas(); img.save("preview_resultado_v6.png")
            print("Captura final del lienzo guardada en preview_resultado_v6.png")
        except Exception as e:
            print("No se pudo guardar la captura final:", e)
        print(f"TERMINADO en {eng.fmt_t(ck.data['elapsed_s'])}. Verificaciones: {ck.data['verification']}")
    else:
        print(f"Progreso guardado: {ck.data['confirmed']}/{plan.n_strokes} confirmados. Reanuda con la opción 4.")


# ------------------------------------------------------------------------------------------
def calibrate(settings):
    import pyautogui
    print("CALIBRACIÓN V6 (settings.txt NO se modifica; se guarda settings_v6.json)")
    def point(msg):
        input(f"\n{msg}\n  Coloca el ratón y pulsa ENTER aquí; tienes 5 s para volver a MediBang... ")
        for s in range(5, 0, -1):
            print(s, end=" ", flush=True); time.sleep(1)
        p = pyautogui.position(); print("->", tuple(p)); return [int(p[0]), int(p[1])]
    out = {"spots": {}, "config": {}}
    if input("¿Calibrar el botón 'capa nueva' y el panel de capas (modo auto de capas)? (s/N): ").strip().lower() == "s":
        out["spots"]["new_layer_button"] = point("Botón de CAPA NUEVA")
        a = point("Esquina SUPERIOR IZQUIERDA del panel de capas")
        b = point("Esquina INFERIOR DERECHA del panel de capas")
        out["spots"]["layer_panel_region"] = [a[0], a[1], b[0] - a[0], b[1] - a[1]]
        out["config"]["LAYER_MODE"] = "auto"
    if input("¿Calibrar la casilla numérica del tamaño de pincel (modo input)? (s/N): ").strip().lower() == "s":
        out["spots"]["brush_size_input"] = point("Casilla donde se escribe el TAMAÑO del pincel")
        out["config"]["BRUSH_SIZE_MODE"] = "input"
    k = input("¿Atajo de teclado que selecciona el pincel en TU MediBang (ej. b)? ENTER = ninguno: ").strip()
    if k:
        out["config"]["TOOL_HOTKEY"] = k
    with open(SETTINGS_V6, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print("Guardado", SETTINGS_V6, "- ejecuta el diagnóstico (--diagnostico) para comprobar que funciona.")


def diagnostic(drv, geo):
    print("\nDIAGNÓSTICO (usa un documento de PRUEBA vacío; se dibujan 2 líneas y se deshacen con el atajo configurado)")
    st, title = drv.window_state()
    print(f"  ventana en primer plano: {st} ({title!r})  [en este momento es la consola; vuelve a MediBang en la cuenta atrás]")
    input("  Pulsa ENTER, y pon MediBang en primer plano... ")
    countdown(5)
    print("  ventana:", drv.window_state())
    try:
        img = drv.grab_canvas(); print(f"  captura del lienzo: {img.size} (esperado {geo.width}x{geo.height})")
    except Exception as e:
        print("  FALLO captura de pantalla:", e); return
    for size in (4, 12):
        drv.set_brush_size(size)
        measured, expected = drv.probe_brush_width(size, 300 + size * 20)
        print(f"  pincel {size} px: grosor medido {measured} px de pantalla, esperado ~{expected:.1f} -> {'OK' if abs(measured - expected) <= max(2, expected * 0.4) else 'REVISAR tamaño/zoom'}")
    k = drv.v6["UNDO_HOTKEY"]
    for _ in range(2):
        drv.pg.hotkey(*k.split("+")); time.sleep(0.3)
    if drv.v6["LAYER_MODE"] == "auto":
        print("  capa:", drv.new_layer("diagnostico"), "(borra la capa creada)")
    print("  Diagnóstico terminado.")


# ------------------------------------------------------------------------------------------
def print_manifest():
    """Huella corta de cada archivo para comprobar que se ejecuta la versión entregada."""
    import hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    parts = []
    for f in ("run_painter_strokes_v6.py", "v6_analysis.py", "v6_planner.py", "v6_driver.py", "v6_engine.py", "v6_checkpoint.py", "v6_compare.py", "run_painter_strokes_v5.py"):
        p = os.path.join(here, f)
        parts.append(f"{f[:-3].replace('run_painter_strokes_', 'run_')}:{hashlib.sha256(open(p, 'rb').read()).hexdigest()[:6]}" if os.path.exists(p) else f"{f}:FALTA")
    print("Huellas: " + "  ".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--imagen"); ap.add_argument("--settings", default="settings.txt")
    ap.add_argument("--semilla", type=int); ap.add_argument("--preset", type=int)
    ap.add_argument("--solo-plan", action="store_true", help="planifica, guarda la vista previa y sale (sin ratón)")
    ap.add_argument("--comparar", action="store_true", help="genera preview_comparacion_v5_v6.png y sale")
    ap.add_argument("--reanudar", action="store_true"); ap.add_argument("--region", action="store_true")
    ap.add_argument("--calibrar", action="store_true"); ap.add_argument("--diagnostico", action="store_true")
    ap.add_argument("--motor", choices=["v5", "v6"], default="v6")
    args = ap.parse_args()
    if args.motor == "v5":
        sys.argv = [sys.argv[0]]
        return v5.main()
    print_manifest()
    settings, cfg, spots = load_all_settings(args.settings)
    if args.semilla is not None: cfg["SEED"] = args.semilla
    if args.preset is not None: cfg["PRESET"] = args.preset
    if args.calibrar:
        return calibrate(settings)
    name, image, area, cres, geo, scene = build(args, settings, cfg)
    image_sha = ckm.file_sha(name) if os.path.exists(name) else "?"
    print(f"Imagen: {name} {image.size} | lienzo {cres[0]}x{cres[1]} | pantalla {area[2]}x{area[3]} | semilla {cfg['SEED']}")
    timing = eng.load_timing()
    print("Planificando (puede tardar unos segundos por preset)...")
    plan, est, fits = choose_plan6(scene, geo, timing, cfg)
    report_plan(plan, est, fits, timing, cfg, geo)
    p6.render_plan6(plan, ss=2).save("preview_v6.png")
    print("  vista previa simulada guardada en preview_v6.png")
    if args.solo_plan:
        return
    if args.comparar:
        return do_compare(scene, image, area, cres, plan, est, geo, cfg, settings)
    cfg_hash = make_cfg_hash(cfg, area, cres, p6.PRESETS6[[p["name"] for p in p6.PRESETS6].index(plan.meta["preset"])])
    if args.region:
        return run_region_test(make_driver(geo, cfg, settings, spots), plan, scene, cfg, geo, timing)
    if args.reanudar:
        return run_full(make_driver(geo, cfg, settings, spots), plan, scene, cfg, geo, name, image_sha, cfg_hash, resume=True)
    if args.diagnostico:
        return diagnostic(make_driver(geo, cfg, settings, spots), geo)
    drv = None
    while True:
        print("\n1) Planificar otro preset / ver tabla (sin ratón)\n2) Prueba de región pequeña (mide tiempos reales)\n3) Pintura completa (nueva)"
              "\n4) Reanudar pintura interrumpida\n5) Comparar V5 vs V6 (simulación, sin ratón)\n6) Diagnóstico de interacción\n7) Calibrar capas/tamaño\n8) Ejecutar V5 (respaldo)\n0) Salir")
        op = input("Opción: ").strip()
        if op == "0": return
        if op == "1":
            cfg["PRESET"] = None; plan, est, fits = choose_plan6(scene, geo, timing, cfg); report_plan(plan, est, fits, timing, cfg, geo)
            p6.render_plan6(plan, ss=2).save("preview_v6.png")
        elif op == "5":
            do_compare(scene, image, area, cres, plan, est, geo, cfg, settings)
        elif op == "7":
            calibrate(settings); settings, cfg2, spots = load_all_settings(args.settings); cfg.update({k: cfg2[k] for k in V6_DEFAULTS})
        elif op == "8":
            sys.argv = [sys.argv[0]]; return v5.main()
        elif op in ("2", "3", "4", "6"):
            drv = make_driver(geo, cfg, settings, spots)
            if op == "2":
                t = run_region_test(drv, plan, scene, cfg, geo, timing)
                if t:
                    timing = t; plan, est, fits = choose_plan6(scene, geo, timing, cfg); report_plan(plan, est, fits, timing, cfg, geo)
            elif op == "6":
                diagnostic(drv, geo)
            else:
                if op == "3" and not fits and input(f"La estimación supera {cfg['MAX_MINUTES']} min. ¿Continuar igualmente? (s/N): ").strip().lower() != "s":
                    continue
                return run_full(drv, plan, scene, cfg, geo, name, image_sha, cfg_hash, resume=(op == "4"))


def do_compare(scene, image, area, cres, plan6, est6, geo, cfg, settings):
    cfg5 = dict(v5.CFG); cfg5.update(settings.get("v5", {})); cfg5["GRID_W"] = int(settings.get("paint_grid_width", cfg5["GRID_W"]))
    p5, est5, fits5 = v5_plan_for_compare(image, area, cres, cfg5, v5.load_timing())
    r = cmp_.compare(scene, p5, plan6, "preview_comparacion_v5_v6.png", est5=eng.fmt_t(est5), est6=eng.fmt_t(est6))
    print("Comparación guardada en preview_comparacion_v5_v6.png")
    for k in ("v5", "v6"):
        print(f"  {k.upper()}: dE medio {r[k]['mean']:.2f}  p95 {r[k]['p95']:.1f}  >10: {r[k]['over10']:.1f}%  PSNR {r[k]['psnr']:.1f}")


if __name__ == "__main__":
    main()
