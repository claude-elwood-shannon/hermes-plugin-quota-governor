#!/usr/bin/env python3
"""_miniyaml — parser del subset YAML de los manifests de capabilities/.

FUENTE UNICA compartida por:
  - capabilities/sysadmin-lan/guard.py (validacion fail-closed de operaciones)
  - scripts/bridge/open-webui-bridge.py (GET /capabilities*)

Zero dependencies (solo stdlib): la regla de la casa prohibe PyYAML en el
bridge y en el guard. El subset soportado es exactamente el que usan los
manifests: mapas y listas anidadas por indentacion, items '- ' escalares o de
un mapa, flow lists [a, b], claves escalares, comentarios '#' fuera de
comillas, comillas simples/dobles, numeros/bools/null. NO soporta (y falla si
aparece): block scalars (|, >), anclas, multi-linea. Ante entrada fuera del
subset lanza ValueError — los consumidores deben hacer fail-closed.

Test de conformance: tests/test_capability_guard.py compara su salida contra
PyYAML (en entornos de desarrollo donde existe) para cada manifest del repo.
"""
from __future__ import annotations

import re
from typing import Any


_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:")


def strip_comment(s: str) -> str:
    """Elimina ' #' de comentario fuera de comillas."""
    out = []
    quote = None
    i = 0
    while i < len(s):
        ch = s[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        else:
            if ch in ("'", '"'):
                quote = ch
                out.append(ch)
            elif ch == "#" and (not out or out[-1] in (" ", "\t")):
                break
            else:
                out.append(ch)
        i += 1
    return "".join(out).rstrip()


def scalar(tok: str) -> str | bool | int | float | None:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        return tok[1:-1]
    low = tok.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        pass
    return tok


def flow(tok: str) -> list[str | bool | int | float | None]:
    """'[a, b]' → ['a', 'b'] (sin comas anidadas: no se usa en estos manifests)."""
    inner = tok.strip()[1:-1].strip()
    if not inner:
        return []
    return [scalar(p) for p in inner.split(",")]


def _sig_lines(raw: str) -> list[tuple[int, str]]:
    out = []
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        content = strip_comment(line.strip())
        if not content:
            continue
        indent = len(line) - len(line.lstrip(" "))
        out.append((indent, content))
    return out


def _parse_block(sigs: list[tuple[int, str]], i: int, indent: int) -> tuple[Any, int]:
    """Parsea un bloque (mapa o lista) en sigs[i] con columna `indent`."""
    if sigs[i][1] == "-" or sigs[i][1].startswith("- "):
        return _parse_list(sigs, i, indent)
    out = {}
    while i < len(sigs) and sigs[i][0] == indent:
        content = sigs[i][1]
        if content == "-" or content.startswith("- "):
            break
        m = _KEY_RE.match(content)
        if not m:
            raise ValueError(f"línea no reconocida como clave: {content!r}")
        key = m.group(1)
        rest = content[m.end():].strip()
        i += 1
        if rest == "":
            if i < len(sigs) and sigs[i][0] > indent:
                val, i = _parse_block(sigs, i, sigs[i][0])
            else:
                val = None
            out[key] = val
        elif rest.startswith("[") and rest.endswith("]"):
            out[key] = flow(rest)
        else:
            out[key] = scalar(rest)
    return out, i


def _parse_list(sigs: list[tuple[int, str]], i: int, indent: int) -> tuple[list, int]:
    out = []
    while i < len(sigs) and sigs[i][0] == indent and (
            sigs[i][1] == "-" or sigs[i][1].startswith("- ")):
        content = sigs[i][1]
        if content == "-":
            i += 1
            if i < len(sigs) and sigs[i][0] > indent:
                val, i = _parse_block(sigs, i, sigs[i][0])
                out.append(val)
            else:
                out.append(None)
            continue
        inner = content[2:].strip()
        m = _KEY_RE.match(inner)
        if not m:
            out.append(scalar(inner))
            i += 1
            continue
        # item es un mapa: primer par en la línea del guion, pares siguientes
        # en líneas con indent mayor.
        key = m.group(1)
        rest = inner[m.end():].strip()
        i += 1
        item = {}
        if rest == "":
            if i < len(sigs) and sigs[i][0] > indent:
                val, i = _parse_block(sigs, i, sigs[i][0])
                item[key] = val
            else:
                item[key] = None
        elif rest.startswith("[") and rest.endswith("]"):
            item[key] = flow(rest)
        else:
            item[key] = scalar(rest)
        prev = i
        while i < len(sigs) and sigs[i][0] > indent:
            sub, i = _parse_block(sigs, i, sigs[i][0])
            item.update(sub)
            if i == prev:
                break
            prev = i
        out.append(item)
    return out, i


def load(path: str) -> dict:
    """Carga `path` (subset YAML) → dict. Lanza ValueError/OSError si el
    contenido está fuera del subset (los consumidores hacen fail-closed)."""
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    sigs = _sig_lines(raw)
    if not sigs:
        return {}
    data, i = _parse_block(sigs, 0, sigs[0][0])
    if i != len(sigs):
        raise ValueError(f"contenido no parseado completo (línea {i})")
    return data
