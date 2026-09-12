# Configuración de la sección GPU

La página **GPU ml-host** del portal de observabilidad (hiperespacio) es
generada por `scripts/obs/portal-build.py` (`page_gpu` → `read_gpu_health`).
Muestra: temperatura, VRAM, utilidad, modelo vLLM activo, rondas del día,
estado de servicios (`vllm.service`, `vllm-rounds.timer`), sparkline térmico
y las últimas 10 rondas.

> **Importante:** hoy **no existe una superficie de configuración dinámica**
> (ni variables de entorno, ni claves de config, ni toggles). Todas las
> opciones de esta sección son **constantes de módulo** hardcodeadas en
> `scripts/obs/portal-build.py` (ver líneas 437–440). Este documento lista
> cada una con su nombre exacto en el código y su valor por defecto, para que
> cualquier cambio futuro parta de un inventario verificado y no se invente
> una API que no existe.

## Qué activa la sección

Nada, en el sentido de *toggle*. La página `gpu` está siempre registrada:

- `PAGES = ("index", "consumo", "board", "providers", "alarms", "gpu", "docs")`
- `_PAGE_FN` asocia `"gpu" → page_gpu`
- `read_gpu_health(...)` se llama de forma incondicional con el resto del
  portal (`build_data`, línea ~1463).

Por tanto la sección se **renderiza siempre** que se construye el portal
(`python3 portal-build.py` o el servidor `obs-serve.py`). Lo único que cambia
con el estado real del host GPU es el **contenido** (datos presentes o estados
vacíos), nunca la presencia de la página.

## Opciones de configuración (constantes de módulo)

Ubicadas en `scripts/obs/portal-build.py`, líneas 437–440, con el bloque que
seguido por las funciones `_ssh_gpu` / `_http_json` / `_gpu_cache_path`.

| Nombre exacto | Valor por defecto | Qué hace |
|---|---|---|
| `_GPU_HOST` | cadena de acceso SSH, p. ej. `user@<gpu-host>` | Destino SSH usado por `_ssh_gpu(...)` para: `nvidia-smi` (temp/VRAM/util, línea 512), `cat .../rounds.jsonl` (historial, línea 535), `systemctl --user is-active vllm` (línea 568) y `systemctl --user is-active vllm-rounds.timer` (línea 572). |
| `_GPU_API` | URL base HTTP del vLLM, p. ej. `http://<gpu-host>:8000` | Se le concatena `/v1/models` (línea 525) vía `_http_json(...)` para leer el modelo activo (`model_id`, `max_model_len` → `model_ctx_len`) y el flag `vllm_active`. |
| `_GPU_CACHE` | `"gpu-health.json"` | Nombre del fichero de caché; vive en `<hermes-home>/quota-governor/obs/` (véase `_gpu_cache_path`). Contiene el último snapshot de salud. |
| `_GPU_CACHE_TTL` | `120` (segundos) | Tiempo de vida de la caché. Dado que el SSH es caro, una lectura más reciente que el TTL devuelve la caché con `source: "cache"` (línea 504) y no vuelve a contactar el host. Pasado el TTL se rehace la lectura viva (`source: "live"`). |

### Tiempos de espera hardcodeados (comportamiento, no configurables)

No son constantes con nombre propio — van como parámetros por defecto dentro de
cada llamada. Forman parte del comportamiento observable:

- `ssh -o ConnectTimeout=5` y `timeout=10` para `nvidia-smi` (`_ssh_gpu`, línea 443).
- `timeout=15` para `cat .../rounds.jsonl` (línea 535).
- `timeout=8` para cada chequeo `systemctl` (líneas 568, 572).
- `_http_json(...)`: `timeout=5` por defecto (línea 455).

### Rutas de estado remotas (hardcodeadas en los comandos SSH)

- Historial de rondas en el host GPU: `.../logs/rounds.jsonl` (línea 535); las
  rondas "de hoy" se cuentan frente al eje de media noche en `CEST`.

## Comportamiento cuando el host GPU es inalcanzable

`_ssh_gpu` devuelve `""` ante **cualquier** fallo (timeout, conexión rechazada,
clave ausente, comando con código de salida ≠ 0). `_http_json` devuelve `{}`
ante cualquier excepción. Consecuencia en la página (estados vacíos, nunca
ceros falsos — patrón watchdog del portal):

- **Temp GPU** → `n/d · sin lectura` (ausentes `temp_c`).
- **VRAM** → `n/d · sin lectura` (ausentes `mem_used_mib` / `mem_total_mib`).
- **Utilización** → `n/d` (ausente `util_pct`).
- **Modelo activo** → `?` con `vLLM ✗` (`vllm_active: False`, no llega `/v1/models`).
- **Rondas hoy** → `0 ok · 0 err · 0 total`; la tabla de rondas recientes se
  sustituye por estado vacío `"sin rondas registradas (rounds.jsonl vacío o inaccesible)"`.
- **Estado de servicios** → `vllm: unknown` y `vllm-rounds.timer: unknown`.
- El pie de fuente muestra `source` (`live` o `cache`) y el timestamp.

Además, incluso cuando el host está inalcanzable, `read_gpu_health` **escribe**
la caché (con `ts` y los campos a vacío), de modo que el siguiente render
dentro del TTL servirá esa copia sin reintentar el SSH.

## Cómo se despliega el cambio (si algún día se parametriza)

Como no hay capa de configuración, "configurar la sección GPU" hoy significa
**editar las constantes en `scripts/obs/portal-build.py`** y reconstruir el
portal (`portal-build.py` o reiniciar `obs-serve.py`). Antes de añadir variables
de entorno o un fichero de config, conviene acordarlo con el mantenedor (OBJ-27)
para no divergir del patrón "solo stdlib, sin rutas absolutas del host" que
impone la prueba de portabilidad del módulo.
