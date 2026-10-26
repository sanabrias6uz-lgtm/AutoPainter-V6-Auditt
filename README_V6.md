# AutoPainter V6 para MediBang Paint

V6 es una evolución de V5. **`run_painter_strokes_v5.py` no se modifica** (hay una prueba que comprueba su SHA-256) y sigue
siendo el respaldo estable: `python run_painter_strokes_v6.py --motor v5` o la opción 8 del menú lo ejecutan.
V6 importa de V5 el color (`lab`/`dE`), la geometría lienzo↔pantalla, el selector de color, el ajuste de tiempos y el control de ESC.
`settings.txt` tampoco se toca; lo nuevo va en `settings_v6.json` (opcional) o en una clave `"v6"` de `settings.txt`.

## Archivos
| Archivo | Para qué |
|---|---|
| `run_painter_strokes_v6.py`, `run_v6.bat` | programa principal y lanzador |
| `v6_analysis.py` | orientación de formas (tensor de estructura), bordes, importancia (piel/contraste/ojos) |
| `v6_planner.py` | 6 etapas, pinceladas curvas, presets, simulación |
| `v6_driver.py` | ratón, capas, tamaño, ventana, verificación por captura |
| `v6_engine.py` | ejecución por lotes verificados, tiempos, prueba de región |
| `v6_checkpoint.py` | progreso y reanudación |
| `v6_compare.py` | comparación V5 vs V6 |
| `test_v6.py`, `test_v6.bat` | regresión V5 (16 pruebas originales) + pruebas nuevas |
| `requirements.txt` | añade **numpy** (V5 sigue funcionando con él) |

## Instalación
1. Copia estos archivos a la carpeta de V5 (no sobrescribas nada de V5; solo `requirements.txt` cambia: se añade `numpy`).
2. Con tu entorno: `venv\Scripts\python.exe -m pip install -r requirements.txt` (o vuelve a ejecutar `setup.bat` si lo usas).
3. Comprueba: `test_v6.bat`. Debe terminar con `37/37 pruebas correctas` (incluye las 16 de V5).

## Configuración en MediBang (igual que V5)
Lienzo 1800x1200, visible entero y **en la misma posición/zoom del setup** (`settings.txt`). Pincel redondo, dureza 100 %, opacidad 100 %,
estabilizador al mínimo. Opacidad 100 % es importante: la recuperación repinta trazos y con opacidad menor se acumularían.

### Capas y tamaño de pincel (no se asumen atajos)
No sé qué atajos tiene tu MediBang, así que por defecto V6 funciona **como V5**: se detiene y te pide crear la capa / poner el tamaño
(`LAYER_MODE: manual`, `BRUSH_SIZE_MODE: manual`). Son 6 capas y ~10 cambios de tamaño en todo el cuadro.
Para automatizarlo, opción 7 del menú (o `--calibrar`): marcas el botón "capa nueva", las esquinas del panel de capas y la casilla numérica
del tamaño. Después usa la opción 6 (diagnóstico, en un documento de PRUEBA) para comprobarlo: mide el grosor real de una línea.
* Capas `auto`: se verifica que el **panel de capas cambia** tras el clic; si no cambia, V6 se detiene.
* Capas `off`: una sola capa, sin fingir capas.
* V6 **no puede leer el nombre de la capa activa**; la verificación de capas es indirecta.

## Uso recomendado: primero una región pequeña
1. `run_v6.bat` → opción **2 (Prueba de región)**. Pinta solo los trazos de un rectángulo alrededor de los ojos (todas las etapas) en un
   documento de prueba del mismo tamaño. Mide: segundos por cambio de color, ms por trazo, coste de verificación. Guarda `timing_v6.json`.
2. El programa **replanifica** con los tiempos medidos y elige el preset más fiel que quepa en `MAX_MINUTES` (120 por defecto, con margen 1,25).
3. Mira `preview_v6.png` y `preview_comparacion_v5_v6.png`.
4. Opción **3 (Pintura completa)**. ESC detiene en cualquier momento.

## Etapas (una capa por etapa)
1 Composición (pincel 72 px, 12 colores) · 2 Bloqueo (36/22) · 3 Volumen (20/14, sombras y luces, error de luminosidad pesa más) ·
4 Textura (12/8, sigue pelo y pliegues) · 5 Detalles (5/3,5, solo donde hay importancia o borde) · 6 Acabado (2,5, brillos y acentos, tope 450).
Cada pasada compara la foto con un lienzo simulado y pinta solo donde queda error visible; el trazo sigue el campo de orientación de la imagen
y se detiene cuando el color deja de coincidir. `--semilla N` fija el azar (mismo plan ⇒ misma huella).
Presets (del más fiel al más rápido): S densa, A+, A, B, C, D, E.

## Guardado y reanudación (`v6_progress/`)
`plan_v6.json.gz` (plan), `progress_v6.json` (progreso, escritura atómica), `run_log_v6.txt`.
Un trazo se da por **confirmado** solo cuando su lote (40 trazos) se verifica en una captura del lienzo (color esperado en el centro de los
últimos trazos; si son muy finos, que la zona haya cambiado). Si un lote no se puede verificar, V6 se **detiene** y no lo confirma.
Reanudar (opción 4 / `--reanudar`): continúa desde el último lote confirmado y repinta como mucho un lote. El MediBang debe ser **el mismo documento**.
Se pulsa Ctrl+S al acabar cada etapa (`SAVE_HOTKEY`), pero solo funciona si ya guardaste el documento una vez con nombre.
Si cierras MediBang sin guardar, el progreso del plan no puede recrear el lienzo.

## Solución de errores
* **"La ventana en primer plano no es MediBang"**: pon MediBang delante (solo Windows puede leer el título; `WINDOW_TITLE` ajustable).
* **"Los márgenes del lienzo no están en blanco"**: cambió el zoom/posición/documento. Restaura la vista del setup.
* **"No se pudo verificar el lote"**: los trazos no aparecen donde se esperaba (pincel/capa oculta, opacidad < 100 %, zoom). Revisa y reanuda.
* **"El panel de capas no cambió"**: recalibra el botón "capa nueva" y el panel (opción 7).
* **Muy lento o muy rápido**: repite la prueba de región; fija `"PRESET": n` o `"MAX_MINUTES"` en `settings_v6.json`/`settings.txt` (`"v6": {...}`).
* ESC detiene y suelta el botón; mover el ratón a una esquina activa el FailSafe de pyautogui, que también lo suelta.

## Límites conocidos
* El ratón no tiene presión: no hay trazos afilados en los extremos; la variedad viene de curvas, direcciones y varios tamaños.
* La importancia visual (ojos/cara) es una heurística de color y contraste, no un detector de caras. Se puede forzar con `"FOCUS_BOXES"`.
* El orden dentro de una etapa es por color (claro→oscuro) para limitar cambios de color; no es el orden de un pintor humano.
* V6 no aprueba nada por sí solo: las simulaciones son de Pillow, no de MediBang.
