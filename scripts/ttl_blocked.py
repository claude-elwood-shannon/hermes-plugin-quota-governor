#!/usr/bin/python3.12
"""ttl_blocked.py — P1: TTL agresivo de tareas blocked con autorremediación.

Promesa (13-sep, user-approved): ninguna tarea permanece en blocked más de
30 minutos sin estar resolviéndose activamente o haber pasado a triage.

Ventanas (medidas desde el último evento 'blocked' de la tarea):
  0-5 min    solo clasificación (el watchdog v3 ya lo hace; aquí no actuamos)
  5-15 min   transitoria -> auto-unblock (rate-limit 1/h existente respetado);
             permanente -> autorremediación determinista
  15-30 min  no resuelta -> move a triage con comentario estructurado
  >30 min    no debe existir (cualquier blocked mayor se mueve a triage SIEMPRE)

Autorremediaciones deterministas (sin intervención humana, en orden):
  R1 campo faltante generable ("user needed to set task id" y variantes):
     el campo lo generó el propio sistema -> re-spawn limpio: unblock.
  R2 model_override inválido/envenenado (no existe en la oferta del perfil
     o pin muerto conocido): limpiar override, reasignar el pin del gate y
     unblock. La remediación SE VERIFICA releyendo el override en la DB:
     si sigue igual, la acción no se produce (p.ej. binario ausente) y la
     tarea se registra como fallida, nunca como remediada.
  R3 dependencia resuelta: bloqueo por padre y el padre ya está done ->
     unblock (queda ready).
  R4 crash loop (gave_up >=3 con el mismo error): NO autorremediable ->
     triage directo con historial adjunto.
  R5 resto: sin reparación determinista -> triage (comentario "no-clasificable"
     si además no hay causa identificada).
  R6 retry-loop (14-sep, mandato del mediador): NO reintentar la misma
     autorremediación en bucle. Dos detectores, ambos leen task_comments
     (sobreviven unblocks; los eventos blocked se re-crean en cada ciclo):
     (a) huella del motivo: cada intento graba fp=sha1(reason)[:8] y su
         resultado. Si una remediación VERIFICADA ok va seguida de un
         re-bloqueo con la MISMA causa (misma fp) -> triage inmediato: la
         causa no era la remediable. Si la misma R falla >= 2 veces con la
         misma fp -> triage (sin tercera espera).
     (b) contador por remedio: la misma R1-R3 intentada >= 2 veces y la
         tarea sigue blocked con remedio pendiente -> triage.

Exclusiones (el TTL NUNCA toca):
  - Tareas con [human-gate] en title+header del body (puerta de aprobación
    humana explícita) — se registran y se dejan.
  - DIRECCION-STOP activo (~/.hermes/quota-governor/STOP): se clasifica en el
    log pero no se ejecuta ninguna mutación.

Idempotencia: una tarea ya en triage nunca se "re-mueve"; el comentario de
triage se escribe una sola vez (guard por task id en el propio movimiento).

Veracidad del log: el resultado refleja lo VERIFICADO, no lo intentado.
Si la CLI mutadora muere (binario ausente, PATH, permisos), la acción se
registra como "action-failed" — jamás como remediated/triaged; en dry-run
los veredictos son "dry-*" y ninguna mutación ocurre. Lección del bucle
t_32a71a49 (14-sep): 34 ciclos de "REMEDIATED" sin una sola mutación real
porque HERMES_BIN no resolvía en el PATH de cron y el except tragaba todo.

Nota de enrutado a triage: el canal oficial es unblock -> block SIN kind;
los blocks sin kind comparan iguales entre sí y unblock_task PRESERVA
block_kind/block_recurrences, así que la segunda vuelta alcanza
BLOCK_RECURRENCE_LIMIT (=2) y el core aterriza la tarea en triage con el
evento 'block_loop_detected'. El kind='needs_input' usado el 13-sep NO
servía para eso (el recuento solo suma cuando el kind entrante == previo).

Exit codes: 0 siempre (el watchdog no debe romperse por esto).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── Constantes ──────────────────────────────────────────────────────────────
TTL_CLASSIFY_S = 5 * 60       # 0-5 min: solo clasificar
TTL_REMEDIATE_S = 15 * 60     # 5-15 min: autorremediación
TTL_TRIAGE_S = 30 * 60        # >30 min: no debe existir -> triage forzado
CRASH_LOOP_MIN = 3            # gave_up >= 3 con mismo error -> triage directo
TRIAGE_MARK = "[TTL-BLOCKED]"
R6_MARK = "R6-RETRY-LOOP"     # prefijo del contador de reintentos en comments
R6_MAX_ATTEMPTS = 2           # misma R fallida >= 2 veces con misma fp -> triage
_HERMES_DEFAULT = shutil.which("hermes") or "hermes"
HERMES_BIN = os.environ.get("HERMES_BIN") or _HERMES_DEFAULT
PLUGIN_DIR = os.environ.get("PLUGIN_DIR", "/data/git/hermes-plugin-quota-governor")

# Overrides muertos conocidos (historia 12-sep): modelo no existe en el host
DEAD_OVERRIDES = {"gpt-oss:20b", "glm-5.2"}  # glm-5.2 prohibido para workers (caro)

# Motivos que el SISTEMA puede generar él solo (R1): la frase "user needed"
# para un campo auto-generado es una contradicción — se repara re-spawneando.
SELF_GENERABLE_RE = re.compile(
    r"(user needed to set task id|need(?:s)? .*task id|missing task id|"
    r"requires? user[- ]set id)", re.I)


def _cli(*args, timeout=60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([HERMES_BIN, "kanban", *args],
                              capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — el watchdog nunca debe romper
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def stop_signal_active(root: Path | None = None) -> bool:
    base = Path(root) if root else Path.home() / ".hermes"
    return (base / "quota-governor" / "STOP").exists()


def _connect(db_path: Path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def header_of(body: str) -> str:
    lines = []
    for line in (body or "").splitlines():
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def has_human_gate(title: str, body: str) -> bool:
    return "[human-gate]" in (title or "") or "[human-gate]" in header_of(body or "")


def blocked_since_map(db_path: Path) -> dict[str, float]:
    """task_id -> epoch del ÚLTIMO evento blocked (la ventana se mide desde
    el bloqueo vigente, no desde el primero de la historia)."""
    out: dict[str, float] = {}
    if not db_path.exists():
        return out
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT task_id, created_at FROM task_events "
            "WHERE kind='blocked' ORDER BY id ASC").fetchall()
    except sqlite3.Error:
        return out
    finally:
        con.close()
    for r in rows:
        out[r["task_id"]] = float(r["created_at"])
    return out


def blocked_reason(db_path: Path, task_id: str) -> str:
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
            "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        if row:
            try:
                return (json.loads(row["payload"]).get("reason") or "").strip()
            except (ValueError, AttributeError):
                pass
        run = con.execute(
            "SELECT error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,)).fetchone()
        return (run["error"] or "").strip() if run and run["error"] else ""
    except sqlite3.Error:
        return ""
    finally:
        con.close()


def gave_up_history(db_path: Path, task_id: str) -> list[str]:
    """Últimos errores de runs fallidos (para detectar crash loop)."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT error FROM task_runs WHERE task_id=? AND error IS NOT NULL "
            "AND error != '' ORDER BY id DESC LIMIT 10", (task_id,)).fetchall()
        return [r["error"] for r in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def dependency_ids(db_path: Path, task_id: str) -> list[str]:
    """Padres ligados vía kanban_link (parent->child)."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT parent_id FROM task_links WHERE child_id=?",
            (task_id,)).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def parent_status(db_path: Path, parent_id: str) -> str | None:
    con = _connect(db_path)
    try:
        row = con.execute("SELECT status FROM tasks WHERE id=?",
                          (parent_id,)).fetchone()
        return row["status"] if row else None
    finally:
        con.close()


def valid_override_for(provider: str | None, model: str | None,
                      offer: dict[str, set[str]]) -> bool:
    """True si (provider, model) existe en la oferta conocida del perfil."""
    if not model:
        return False
    m = model.split("/")[-1]
    if m in DEAD_OVERRIDES:
        return False
    prov = (provider or "").strip() or "ollama-cloud"
    return m in offer.get(prov, set())


# Oferta mínima verificada por /api/tags + gate (13-sep). No es un catálogo:
# es la lista de pines que SABEMOS vivos; el resto se considera envenenado.
DEFAULT_OFFER: dict[str, set[str]] = {
    "ollama-cloud": {"deepseek-v4-flash", "deepseek-v4-flash:cloud",
                     "gpt-oss:120b-cloud", "qwen3.8-flash", "kimi-k2.7-code"},
    "custom": {"liodon-ai/Qwen2.5-7B-Instruct-FP8"},
    "nanogpt": {"z-ai/glm-5.3-flash"},
}


# ── R6: memoria de reintentos (task_comments sobreviven unblocks) ──────────
def reason_fp(reason: str) -> str:
    """Huella corta del motivo de bloqueo (estable entre ciclos)."""
    return hashlib.sha1((reason or "").encode("utf-8", "replace")).hexdigest()[:8]


def record_remediation_attempt(db_path: Path, task_id: str, remedy: str,
                               reason: str, ok: bool) -> None:
    """Deja huella duradera del intento: remedio + fp del motivo + resultado.
    Llamar SOLO con execute=True (un dry-run no debe inflar el contador)."""
    outcome = "ok" if ok else "failed"
    _cli("comment", task_id,
         f"{R6_MARK} {remedy} attempt fp={reason_fp(reason)} {outcome}")


def r6_escalation(db_path: Path, task_id: str, current_reason: str) -> str | None:
    """Detecta bucle: None = seguir el flujo normal; str = motivo de triage.
    (a) fp actual con intento 'ok' previo: la remediación se verificó y la
        tarea volvió a bloquearse con la MISMA causa -> no es remediable.
    (b) fp actual con >= R6_MAX_ATTEMPTS intentos 'failed': no insistir."""
    fp = reason_fp(current_reason)
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT body FROM task_comments WHERE task_id=? AND body LIKE ?",
            (task_id, f"{R6_MARK} %attempt fp={fp} %")).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    n_ok = n_failed = 0
    last_remedy = "R?"
    for r in rows:
        parts = (r["body"] or "").split()
        # "R6-RETRY-LOOP <remedy> attempt fp=<fp> <ok|failed>"
        if len(parts) >= 5:
            last_remedy = parts[1]
            if parts[-1] == "ok":
                n_ok += 1
            elif parts[-1] == "failed":
                n_failed += 1
    if n_ok >= 1:
        return (f"re-bloqueo con la misma causa tras remediación {last_remedy} "
                f"verificada ok — la causa no era la remediable (fp={fp})")
    if n_failed >= R6_MAX_ATTEMPTS:
        return (f"remediación {last_remedy} falló {n_failed}x con la misma causa "
                f"(fp={fp}) — no reintentar en bucle")
    return None


def remediation_attempt_count(db_path: Path, task_id: str, remedy: str) -> int:
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body LIKE ?",
            (task_id, f"{R6_MARK} {remedy} attempt%")).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def _route_to_triage(task_id: str, comment: str, db_path: Path) -> tuple[bool, str]:
    """Canal CLI OFICIAL a triage: (la tarea ya está blocked) -> unblock ->
    block SIN kind -> la misma causa genérica cuenta recurrencia y al llegar
    a BLOCK_RECURRENCE_LIMIT (=2) el propio core aterriza la tarea en triage
    con el evento 'block_loop_detected'. Un task con contador a 0 necesita
    DOS vueltas del watchdog (recurrences 1 y 2): la primera devuelve False
    y el veredicto es honesto ('route incomplete'), la segunda completa.
    Verifica el aterrizaje REAL en la DB antes de decir triaged."""
    c1 = _cli("comment", task_id, comment)
    c2 = _cli("unblock", task_id,
              "--reason", "TTL-BLOCKED ciclo (doble-block hacia triage)")
    c3 = _cli("block", task_id,
              "[TTL-BLOCKED] re-block: ruteo a triage (unblock-loop break)")
    if _cli_failed(c2) or _cli_failed(c3):
        return False, _first_err(c2, c3)
    con = _connect(db_path)
    try:
        row = con.execute("SELECT status FROM tasks WHERE id=?",
                          (task_id,)).fetchone()
        status = row["status"] if row else "?"
    except sqlite3.Error:
        status = "?"
    finally:
        con.close()
    if status == "triage":
        return True, ""
    return False, f"ruta incompleta: status={status} (recurrences=1; la 2a vuelta completa)"


def triage_comment(minutes: int, cls: str, root_cause: str,
                   attempted: str, result: str, why: str) -> str:
    return (
        f"{TRIAGE_MARK} Movida a triage automático.\n"
        f"Tiempo en blocked: {minutes}m\n"
        f"Clasificación: {cls}\n"
        f"Causa raíz identificada: {root_cause}\n"
        f"Autorremediación intentada: {attempted} — {result}\n"
        f"Motivo triage: {why}")


class TtlResult:
    __slots__ = ("action", "task_id", "detail")

    def __init__(self, action: str, task_id: str, detail: str = ""):
        self.action = action        # classify|remediated|failed|triaged|dry-*|skipped
        self.task_id = task_id
        self.detail = detail

    def line(self, ts: str) -> str:
        return f"{ts} TTL-{self.action.upper()} {self.task_id}: {self.detail}"


def _cli_failed(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode != 0


def _first_err(*procs: subprocess.CompletedProcess) -> str:
    for p in procs:
        if p.returncode != 0:
            return (p.stderr or p.stdout or f"rc={p.returncode}").strip()[:140]
    return ""


def _override_in_db(db_path: Path, task_id: str) -> str:
    con = _connect(db_path)
    try:
        row = con.execute("SELECT model_override FROM tasks WHERE id=?",
                          (task_id,)).fetchone()
        return (row["model_override"] or "") if row else ""
    finally:
        con.close()


def _apply_override_and_unblock(db_path: Path, task_id: str, old: str) -> tuple[bool, str]:
    """R2: set-model al pin del gate + unblock. VERIFICA la persistencia
    releyendo la DB. Devuelve (ok, err)."""
    setr = _cli("set-model", task_id, "deepseek-v4-flash", "--provider", "ollama-cloud")
    if _cli_failed(setr):
        return False, f"set-model rc={setr.returncode}: {_first_err(setr)}"
    unp = _cli("unblock", task_id, "--reason",
               "TTL-BLOCKED R2: model_override envenenado limpiado y reasignado")
    if _cli_failed(unp):
        return False, f"unblock rc={unp.returncode}: {_first_err(unp)}"
    now = _override_in_db(db_path, task_id)
    if now == old:
        # La DB sigue igual pese a rc=0: persistencia no verificada.
        return False, "set-model rc=0 pero el override sigue igual en la DB"
    _cli("comment", task_id,
         f"{TRIAGE_MARK} autorremediación R2 verificada: override '{old}' -> "
         f"deepseek-v4-flash (persistencia comprobada en DB)")
    return True, ""


def _skip_human_gate(title: str, body: str, task_id: str,
                     minutes: int) -> TtlResult | None:
    """Exclusión [human-gate]: el TTL nunca actúa sobre esa tarea."""
    if not has_human_gate(title, body):
        return None
    return TtlResult("skip-human-gate", task_id,
                     f"{minutes}m — puerta humana explícita, TTL no actúa")


def _classify_early(db_path: Path, task_id: str, age_s: float,
                    minutes: int) -> TtlResult | None:
    """Ventana 0-5m: solo clasificación — salvo crash loop (no espera a 5m)."""
    if age_s >= TTL_CLASSIFY_S:
        return None
    errors = gave_up_history(db_path, task_id)
    if not any(sum(1 for x in errors if x == e) >= CRASH_LOOP_MIN
               for e in errors):
        return TtlResult("classify", task_id,
                         f"{minutes}m — ventana 0-5m, solo clasificación")
    return None


def _crash_loop_triage(db_path: Path, task_id: str, minutes: int,
                       execute: bool, dry) -> TtlResult | None:
    """R4: crash loop (gave_up >=3 con el mismo error) -> triage directo."""
    errors = gave_up_history(db_path, task_id)
    loop = None
    for e in errors:
        if sum(1 for x in errors if x == e) >= CRASH_LOOP_MIN:
            loop = e
            break
    if not loop:
        return None
    why = f"crash loop {CRASH_LOOP_MIN}x con el mismo error"
    if execute:
        done, err = _route_to_triage(task_id, triage_comment(
            minutes, "permanente", (loop or "")[:120],
            "no", "crash loop no es autorremediable",
            why + f": {(loop or '')[:160]}"), db_path)
        if done:
            return TtlResult("triaged", task_id, f"{minutes}m — {why}")
        return TtlResult("retrying", task_id,
                         f"{minutes}m — {why} | ruta a triage: {err}")
    return dry("triage", why)


def _r6_escalation_triage(db_path: Path, task_id: str, reason: str,
                          model_override: str, minutes: int, execute: bool,
                          dry) -> TtlResult | None:
    """R6(a): re-bloqueo con la MISMA causa tras remediación ok -> triage."""
    esc = r6_escalation(db_path, task_id, reason or model_override)
    if not esc:
        return None
    if execute:
        done, err = _route_to_triage(task_id, triage_comment(
            minutes, "permanente", (reason or model_override or "")[:120],
            "R1-R3", "sin efecto persistente sobre la causa",
            f"retry loop: {esc}"), db_path)
        if done:
            return TtlResult("triaged", task_id, f"{minutes}m — retry loop: {esc}")
        return TtlResult("retrying", task_id,
                         f"{minutes}m — retry loop: {esc} | ruta a triage: {err}")
    return dry("triage", f"retry loop: {esc}")


def _pending_remedies(db_path: Path, task_id: str, reason: str,
                      model_override: str, provider: str) -> tuple[list[str], list[str]]:
    """Remedios deterministas pendientes (R1-R3) en orden R2->R1->R3."""
    pending: list[str] = []
    if model_override and not valid_override_for(provider, model_override,
                                                 DEFAULT_OFFER):
        pending.append("R2")
    if reason and SELF_GENERABLE_RE.search(reason):
        pending.append("R1")
    deps = dependency_ids(db_path, task_id)
    resolved = [d for d in deps if parent_status(db_path, d) == "done"]
    if deps and len(resolved) == len(deps):
        pending.append("R3")
    return pending, deps


def _r6_counter_triage(db_path: Path, task_id: str, pending: list[str],
                       reason: str, model_override: str, minutes: int,
                       execute: bool, dry) -> TtlResult | None:
    """R6(b): contador por remedio — la misma R >= 2x pendiente -> triage."""
    for remedy in pending:
        attempts = remediation_attempt_count(db_path, task_id, remedy)
        if attempts < R6_MAX_ATTEMPTS:
            continue
        why = (f"retry loop: {remedy} intentada {attempts}x y la tarea sigue "
               f"blocked con remedio pendiente — escalado a triage")
        if execute:
            done, err = _route_to_triage(task_id, triage_comment(
                minutes, "permanente", (reason or model_override or "")[:120],
                f"{remedy} x{attempts}", "sin efecto persistente", why), db_path)
            if done:
                return TtlResult("triaged", task_id, f"{minutes}m — {why}")
            return TtlResult("retrying", task_id,
                             f"{minutes}m — {why} | ruta a triage: {err}")
        return dry("triage", why)
    return None


def _apply_remediation(db_path: Path, task_id: str, pending: list[str],
                       reason: str, model_override: str, deps: list[str],
                       minutes: int, execute: bool, dry) -> TtlResult | None:
    """Aplica el primer remedio pendiente en orden R2 -> R1 -> R3."""
    if "R2" in pending:
        return _remediate_r2(db_path, task_id, reason, model_override,
                             minutes, execute, dry)
    if "R1" in pending:
        return _remediate_r1(db_path, task_id, reason, minutes, execute, dry)
    if "R3" in pending:
        return _remediate_r3(db_path, task_id, reason, deps, minutes,
                             execute, dry)
    return None


def _remediate_r2(db_path: Path, task_id: str, reason: str,
                  model_override: str, minutes: int, execute: bool,
                  dry) -> TtlResult:
    """R2: override envenenado -> set-model al pin del gate + unblock + verify."""
    if not execute:
        return dry("remediate",
                   f"R2 pendiente: set-model deepseek-v4-flash + unblock + verify")
    ok, err = _apply_override_and_unblock(db_path, task_id, model_override)
    record_remediation_attempt(db_path, task_id, "R2", reason, ok=ok)
    if not ok:
        return TtlResult("failed", task_id,
                         f"{minutes}m — R2 action-failed: {err}")
    return TtlResult("remediated", task_id,
                     f"{minutes}m — R2 override '{model_override}' -> pin gate (verificado)")


def _remediate_r1(db_path: Path, task_id: str, reason: str, minutes: int,
                  execute: bool, dry) -> TtlResult:
    """R1: campo que el propio sistema podía generar -> unblock limpio."""
    if not execute:
        return dry("remediate", f"R1 pendiente: unblock ({reason[:50]})")
    unp = _cli("unblock", task_id, "--reason",
               "TTL-BLOCKED R1: campo auto-generable resuelto por el sistema")
    ok = not _cli_failed(unp)
    record_remediation_attempt(db_path, task_id, "R1", reason, ok=ok)
    if ok:
        _cli("comment", task_id,
             f"{TRIAGE_MARK} autorremediación R1: '{reason[:80]}' es un campo "
             f"generable por el sistema -> desbloqueada sin intervención humana")
    else:
        return TtlResult("failed", task_id,
                         f"{minutes}m — R1 action-failed: {_first_err(unp)}")
    return TtlResult("remediated", task_id, f"{minutes}m — R1 {reason[:60]}")


def _remediate_r3(db_path: Path, task_id: str, reason: str, deps: list[str],
                  minutes: int, execute: bool, dry) -> TtlResult:
    """R3: dependencia ya resuelta -> unblock como ready."""
    if not execute:
        return dry("remediate", f"R3 pendiente: unblock (deps {deps} done)")
    unp = _cli("unblock", task_id, "--reason", "TTL-BLOCKED R3: dependencia resuelta")
    ok = not _cli_failed(unp)
    record_remediation_attempt(db_path, task_id, "R3", reason, ok=ok)
    if ok:
        _cli("comment", task_id,
             f"{TRIAGE_MARK} autorremediación R3: dependencias {deps} ya done -> desbloqueada")
    else:
        return TtlResult("failed", task_id,
                         f"{minutes}m — R3 action-failed: {_first_err(unp)}")
    return TtlResult("remediated", task_id, f"{minutes}m — R3 deps {deps} done")


def _triage_or_classify(db_path: Path, task_id: str, reason: str,
                        age_s: float, minutes: int, execute: bool,
                        dry) -> TtlResult:
    """15-30m+ sin remedio -> triage; resto: clasificada hasta la ventana."""
    if age_s < TTL_TRIAGE_S:
        return TtlResult("classify", task_id,
                         f"{minutes}m — sin reparación determinista aún (ventana 5-15m)")
    cls = "no-clasificable" if not reason else "permanente"
    why = "TTL 30m agotado sin resolución ni reparación determinista"
    if execute:
        done, err = _route_to_triage(task_id, triage_comment(
            minutes, cls, (reason or "(sin razon registrada)")[:120],
            "sí" if age_s >= TTL_REMEDIATE_S else "no",
            "sin reparación determinista disponible", why), db_path)
        if done:
            return TtlResult("triaged", task_id, f"{minutes}m — {cls} -> triage")
        return TtlResult("retrying", task_id,
                         f"{minutes}m — {cls} -> triage | ruta: {err}")
    return dry("triage", f"{cls} -> triage")


def process_blocked(db_path: Path, task_id: str, model_override: str,
                    provider: str, body: str, title: str,
                    blocked_at: float, now: float,
                    *, execute: bool, root: Path) -> TtlResult:
    """Orquestador: mide la ventana TTL y despacha a helpers de triage/remedio."""
    age_s = now - blocked_at
    minutes = int(age_s // 60)
    reason = blocked_reason(db_path, task_id)
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    def dry(action: str, detail: str) -> TtlResult:
        return TtlResult(f"dry-{action}", task_id, f"{minutes}m — {detail}")

    res = _skip_human_gate(title, body, task_id, minutes)
    if res:
        return res
    res = _classify_early(db_path, task_id, age_s, minutes)
    if res:
        return res
    res = _crash_loop_triage(db_path, task_id, minutes, execute, dry)
    if res:
        return res
    res = _r6_escalation_triage(db_path, task_id, reason, model_override,
                                minutes, execute, dry)
    if res:
        return res
    pending, deps = _pending_remedies(db_path, task_id, reason,
                                      model_override, provider)
    res = _r6_counter_triage(db_path, task_id, pending, reason,
                             model_override, minutes, execute, dry)
    if res:
        return res
    res = _apply_remediation(db_path, task_id, pending, reason,
                             model_override, deps, minutes, execute, dry)
    if res:
        return res
    return _triage_or_classify(db_path, task_id, reason, age_s, minutes,
                              execute, dry)


def run(execute: bool = False, now: float | None = None,
        db_path: Path | None = None, root: Path | None = None) -> list[TtlResult]:
    now = time.time() if now is None else float(now)
    root = root or Path.home() / ".hermes"
    db_path = db_path or root / "kanban.db"
    if not db_path.exists():
        return []
    stopped = stop_signal_active(root)
    results: list[TtlResult] = []
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, model_override, provider_override, body, title "
            "FROM tasks WHERE status='blocked'").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    since = blocked_since_map(db_path)
    for r in rows:
        tid = r["id"]
        blocked_at = since.get(tid)
        if blocked_at is None:
            # Sin evento: asume created_at (tarea bloqueada de fábrica).
            try:
                con = _connect(db_path)
                blocked_at = con.execute(
                    "SELECT created_at FROM tasks WHERE id=?", (tid,)).fetchone()[0]
                con.close()
            except sqlite3.Error:
                blocked_at = now
        if stopped:
            results.append(TtlResult("skipped-stop", tid,
                                     "DIRECCION-STOP activo — clasifica pero no actúa"))
            continue
        results.append(process_blocked(
            db_path, tid, r["model_override"] or "", r["provider_override"] or "",
            r["body"] or "", r["title"] or "", float(blocked_at), now,
            execute=execute, root=root))
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--execute", action="store_true",
                   help="aplicar mutaciones (sin esto: dry-run)")
    args = p.parse_args(argv)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    for res in run(execute=args.execute):
        print(res.line(ts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
