# -*- coding: utf-8 -*-
"""
AutoPainter V6 - checkpoints y recuperación.

Carpeta de progreso (por defecto v6_progress/):
  plan_v6.json.gz   plan completo de pinceladas (se escribe una vez)
  progress_v6.json  progreso. Se escribe de forma ATÓMICA (archivo temporal + os.replace): un corte de
                    luz a mitad de escritura nunca deja un JSON a medias.
  run_log_v6.txt    registro de eventos y errores (solo se añade)

Regla de recuperación:  'confirmed' = nº de trazos CONFIRMADOS por verificación (lote verificado en la
captura de pantalla). Al reanudar se empieza en 'confirmed'; los trazos enviados pero no confirmados
('attempted' > 'confirmed') se REPINTAN con el mismo color (con opacidad 100 % repintar el mismo color
encima no cambia el resultado). Enviar el movimiento del ratón NO cuenta como completado.
LIMITACIÓN: si el documento de MediBang se cerró sin guardar, el progreso del plan no puede recrear el
lienzo; por eso se guarda el documento nativo al final de cada etapa (SAVE_HOTKEY) y se avisa.
"""
import os, json, time, hashlib
from v6_planner import Plan6

VERSION = 1


def file_sha(path, n=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(n * 16))
    return h.hexdigest()[:16]


def config_hash(d):
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Checkpoint:
    def __init__(self, folder="v6_progress"):
        self.dir = folder
        self.plan_path = os.path.join(folder, "plan_v6.json.gz")
        self.prog_path = os.path.join(folder, "progress_v6.json")
        self.log_path = os.path.join(folder, "run_log_v6.txt")
        self.data = None

    # ---------------------------------------------------------------- ciclo de vida
    def exists(self):
        return os.path.exists(self.prog_path) and os.path.exists(self.plan_path)

    def start_new(self, plan, image_name, image_sha, cfg_hash):
        os.makedirs(self.dir, exist_ok=True)
        plan.save(self.plan_path)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self.data = dict(version=VERSION, image=image_name, image_sha=image_sha, config_hash=cfg_hash,
                         plan_digest=plan.digest(), n_strokes=plan.n_strokes, created=now, updated=now,
                         confirmed=0, attempted=0, stage=0, stages_done=[], layers_ready=[],
                         elapsed_s=0.0, errors=[], verification=dict(fuerte=0, debil=0, ninguno=0, omitida=0),
                         last_safe=dict(index=0, time=now), doc_saved=[], finished=False)
        self._write()
        self.log("INICIO nuevo plan: %d trazos, digest %s" % (plan.n_strokes, self.data["plan_digest"]))

    def load(self):
        with open(self.prog_path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        plan = Plan6.load(self.plan_path)
        if plan.digest() != self.data["plan_digest"]:
            raise ValueError("El plan guardado no coincide con su huella (archivo dañado). No se reanuda.")
        return plan

    def compatible(self, image_sha, cfg_hash):
        d = self.data
        return d["image_sha"] == image_sha and d["config_hash"] == cfg_hash

    # ---------------------------------------------------------------- escritura
    def _write(self):
        self.data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = self.prog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.prog_path)

    def log(self, msg):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")

    def attempted(self, index_next):
        """Se han ENVIADO trazos hasta index_next (exclusive) - sin confirmar."""
        self.data["attempted"] = max(self.data["attempted"], index_next)

    def confirm(self, index_next, level, elapsed):
        """Confirma trazos hasta index_next (exclusive) tras verificar el lote."""
        d = self.data
        d["confirmed"] = max(d["confirmed"], index_next)
        d["attempted"] = max(d["attempted"], index_next)
        d["verification"][level] = d["verification"].get(level, 0) + 1
        d["elapsed_s"] = elapsed
        d["last_safe"] = dict(index=d["confirmed"], time=time.strftime("%Y-%m-%d %H:%M:%S"))
        self._write()

    def error(self, msg, index):
        self.data["errors"].append(dict(t=time.strftime("%H:%M:%S"), index=index, msg=str(msg)[:300]))
        self.data["errors"] = self.data["errors"][-200:]
        self.log(f"ERROR en trazo {index}: {msg}")
        self._write()

    def stage_started(self, si, layer_result):
        d = self.data
        d["stage"] = si
        if si not in d["layers_ready"]:
            d["layers_ready"].append(si)
        self.log(f"ETAPA {si} iniciada (capa: {layer_result})")
        self._write()

    def stage_done(self, si, saved):
        d = self.data
        if si not in d["stages_done"]:
            d["stages_done"].append(si)
        if saved:
            d["doc_saved"].append(dict(stage=si, t=time.strftime("%H:%M:%S")))
        self.log(f"ETAPA {si} terminada (documento guardado: {saved})")
        self._write()

    def finish(self, elapsed):
        self.data["finished"] = True
        self.data["elapsed_s"] = elapsed
        self.log("FIN")
        self._write()

    @property
    def resume_index(self):
        return self.data["confirmed"]
