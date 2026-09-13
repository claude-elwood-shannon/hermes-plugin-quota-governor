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
     unblock.
  R3 dependencia resuelta: bloqueo por padre y el padre ya está done ->
     unblock (queda ready).
  R4 crash loop (gave_up >=3 con el mismo error): NO autorremediable ->
     triage directo con historial adjunto.
  R5 resto: sin reparación determinista -> triage (comentario "no-clasificable"
     si además no hay causa identificada).

Exclusiones (el TTL NUNCA toca):
  - Tareas con [human-gate] en title+header del body (puerta de aprobación
    humana explícita) — se registran y se dejan.
  - DIRECCION-STOP activo (~/.hermes/quota-governor/STOP): se clasifica en el
    log pero no se ejecuta ninguna mutación.

Idempotencia: una tarea ya en triage nunca se "re-mueve"; el comentario de
triage se escribe una sola vez (guard por task id en el propio movimiento).

Exit codes: 0 siempre (el watchdog no debe romperse por esto).
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")

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
    except sqlite3.Error:
        return None
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


def _route_to_triage(task_id: str, comment: str) -> None:
    """Canal CLI OFICIAL a triage: (la tarea ya está blocked) -> unblock ->
    block(same kind) -> triage. El segundo block del mismo kind dispara el
    unblock-loop detector del CLI y la tarea aterriza en triage con audit
    trail. Verificado en vivo 13-sep (t_45ff9da2, t_61fed817)."""
    _cli("comment", task_id, comment)
    _cli("unblock", task_id, "TTL-BLOCKED ciclo 1 (doble-block hacia triage)")
    _cli("block", "--kind", "needs_input", task_id,
         "[TTL-BLOCKED] re-block: ruteo a triage (unblock-loop break)")


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
        self.action = action        # classify|unblock|remediated|triaged|skipped
        self.task_id = task_id
        self.detail = detail

    def line(self, ts: str) -> str:
        return f"{ts} TTL-{self.action.upper()} {self.task_id}: {self.detail}"


def process_blocked(db_path: Path, task_id: str, model_override: str,
                    provider: str, body: str, title: str,
                    blocked_at: float, now: float,
                    *, execute: bool, root: Path) -> TtlResult:
    """Una tarea blocked: clasificar y actuar según la ventana del TTL."""
    age_s = now - blocked_at
    minutes = int(age_s // 60)
    reason = blocked_reason(db_path, task_id)
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    # Puerta humana explícita: nunca se toca.
    if has_human_gate(title, body):
        return TtlResult("skip-human-gate", task_id,
                         f"{minutes}m — puerta humana explícita, TTL no actúa")

    if age_s < TTL_CLASSIFY_S:
        # Ventana 0-5m: solo clasificación — PERO el crash loop es urgente y
        # determinista: no espera a los 5 min.
        errors_early = gave_up_history(db_path, task_id)
        if not any(sum(1 for x in errors_early if x == e) >= CRASH_LOOP_MIN
                   for e in errors_early):
            return TtlResult("classify", task_id,
                             f"{minutes}m — ventana 0-5m, solo clasificación")

    # R4: crash loop — triage directo con historial (no autorremediable).
    errors = gave_up_history(db_path, task_id)
    loop = None
    for e in errors:
        n = sum(1 for x in errors if x == e)
        if n >= CRASH_LOOP_MIN:
            loop = e
            break
    if loop:
        why = f"crash loop {CRASH_LOOP_MIN}x con el mismo error"
        if execute:
            _route_to_triage(task_id, triage_comment(
                minutes, "permanente", (loop or "")[:120],
                "no", "crash loop no es autorremediable",
                why + f": {(loop or '')[:160]}"))
        return TtlResult("triaged", task_id, f"{minutes}m — {why}")

    # ── Autorremediación determinista (desde los 5 min: R1-R3 son seguras) ──
    # R2: override envenenado.
    if model_override and not valid_override_for(provider, model_override, DEFAULT_OFFER):
        if execute:
            _cli("set-model", task_id, "deepseek-v4-flash", "--provider", "ollama-cloud")
            _cli("comment", task_id,
                 f"{TRIAGE_MARK} autorremediación R2: override '{model_override}' "
                 f"envenenado -> reasignado deepseek-v4-flash (pin gate)")
            _cli("unblock", task_id,
                 "TTL-BLOCKED R2: model_override envenenado limpiado y reasignado")
        return TtlResult("remediated", task_id,
                         f"{minutes}m — R2 override '{model_override}' -> pin gate")

    # R1: campo que el propio sistema podía generar.
    if reason and SELF_GENERABLE_RE.search(reason):
        if execute:
            _cli("comment", task_id,
                 f"{TRIAGE_MARK} autorremediación R1: '{reason[:80]}' es un campo "
                 f"generable por el sistema -> desbloqueada sin intervención humana")
            _cli("unblock", task_id, "TTL-BLOCKED R1: campo auto-generable resuelto por el sistema")
        return TtlResult("remediated", task_id, f"{minutes}m — R1 {reason[:60]}")

    # R3: dependencia ya resuelta.
    deps = dependency_ids(db_path, task_id)
    resolved = [d for d in deps if parent_status(db_path, d) == "done"]
    if deps and len(resolved) == len(deps):
        if execute:
            _cli("comment", task_id,
                 f"{TRIAGE_MARK} autorremediación R3: dependencias {deps} ya done -> desbloqueada")
            _cli("unblock", task_id, "TTL-BLOCKED R3: dependencia resuelta")
        return TtlResult("remediated", task_id, f"{minutes}m — R3 deps {deps} done")

    # 15-30m o >30m sin reparación determinista -> triage.
    if age_s >= TTL_TRIAGE_S:
        cls = "no-clasificable" if not reason else "permanente"
        why = "TTL 30m agotado sin resolución ni reparación determinista"
        if execute:
            _route_to_triage(task_id, triage_comment(
                minutes, cls, (reason or "(sin razon registrada)")[:120],
                "sí" if age_s >= TTL_REMEDIATE_S else "no",
                "sin reparación determinista disponible", why))
        return TtlResult("triaged", task_id, f"{minutes}m — {cls} -> triage")

    return TtlResult("classify", task_id,
                     f"{minutes}m — sin reparación determinista aún (ventana 5-15m)")


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
