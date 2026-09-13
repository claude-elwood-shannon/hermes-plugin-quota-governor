"""Tests de scripts/autoqueue.py (OBJ-44).

Los caminos de la cola y del registro se inyectan (tmp_path); nunca se
toca la cola real ni se escribe en rutas de host.
"""
import json

import autoqueue as aq


# --- parseo ---------------------------------------------------------------

def parse_semillas(tmp_path, content):
    f = tmp_path / "autoqueue.md"
    f.write_text(content, encoding="utf-8")
    return aq.parsear_semillas(f), f


def test_parse_valida_e_ignora_hechas(tmp_path):
    content = (
        "- [ ] tarea a | pr-ollama | small\n"
        "- [x] tarea hecha | pr-ollama | micro\n"
        "- [ ] otra | pr-ollama | tiny\n"
    )
    semillas, _ = parse_semillas(tmp_path, content)
    assert len(semillas) == 2                     # solo las pendientes `- [ ]`
    assert semillas[0]["desc"] == "tarea a"
    assert semillas[0]["perfil"] == "pr-ollama"
    assert semillas[0]["coste"] == "small"
    assert all(s["consumible"] for s in semillas)


def test_parse_no_consumibles_por_anotaciones(tmp_path):
    content = (
        "- [ ] duplicada de la anterior | a | small\n"
        "- [ ] semilla rota | b | small\n"
        "- [ ] tarea existente ya hecha | c | small\n"
        "- [ ] con id encargado (t_abc) | d | small\n"
        "- [ ] sana | e | small\n"
    )
    semillas, _ = parse_semillas(tmp_path, content)
    anotadas = [s for s in semillas if not s["consumible"]]
    cons = [s for s in semillas if s["consumible"]]
    assert len(anotadas) == 4                     # 4 anotadas -> no consumibles
    assert {s["desc"] for s in anotadas} == {"duplicada de la anterior",
                                             "semilla rota",
                                             "tarea existente ya hecha",
                                             "con id encargado (t_abc)"}
    assert [s["desc"] for s in cons] == ["sana"]


def test_parse_fuera_de_formato_ignorado(tmp_path):
    content = (
        "- [ ] sin tuberias | pr-ollama\n"       # <3 partes -> ignorado
        "esto no es una semilla\n"
        "# titulo\n"
    )
    semillas, _ = parse_semillas(tmp_path, content)
    assert semillas == []


# --- generador -------------------------------------------------------------

def test_generar_semillas_reservorio_vacio():
    assert aq.generar_semillas() == []


# --- consumo ---------------------------------------------------------------

def test_dry_run_no_muta_y_registra(tmp_path):
    f = tmp_path / "autoqueue.md"
    f.write_text("- [ ] tarea a | pr-ollama | small\n", encoding="utf-8")
    log = tmp_path / "consumes.jsonl"

    msg = aq.consumir_semilla(execute=False, crear_tarea=lambda: "x",
                              ruta=f, log=log)

    # Dry-run: no muta la cola.
    assert f.read_text(encoding="utf-8") == "- [ ] tarea a | pr-ollama | small\n"
    assert len(f.read_text(encoding="utf-8").splitlines()) == 1  # wc -l intacto
    # Registra con dry_run=True.
    recs = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(recs) == 1 and recs[0]["dry_run"] is True
    # Devuelve (y main() imprime) la acción en dry-run.
    assert msg.startswith("dry-run:") and "tarea a" in msg


def test_execute_marca_inplace_y_conserva_wc_l(tmp_path):
    f = tmp_path / "autoqueue.md"
    original = ("- [ ] primera | pr-ollama | small\n"
                "- [ ] segunda anotada (t_zzz) | pr-ollama | micro\n"
                "- [ ] tercera | pr-ollama | small\n")
    f.write_text(original, encoding="utf-8")
    log = tmp_path / "consumes.jsonl"
    n_antes = len(original.splitlines())

    msg = aq.consumir_semilla(execute=True, crear_tarea=lambda: "999",
                              ruta=f, log=log)

    final = f.read_text(encoding="utf-8")
    lines = final.splitlines()
    # Marca la primera consumible en-place, con [x] y sufijo (t_999).
    assert lines[0].startswith("- [x] primera") and "(t_999)" in lines[0]
    # Las otras líneas intactas (la anotada y la tercera).
    assert lines[1] == original.splitlines()[1]
    assert lines[2] == original.splitlines()[2]
    # wc -l sin cambios.
    assert len(lines) == n_antes
    # Registro append-only con el id.
    recs = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(recs) == 1 and recs[0]["tarea"] == "999"
    assert recs[0]["execute"] is True
    # Devuelve (y main() imprime) la acción del consumo real.
    assert msg.startswith("consumida:") and "t_999" in msg


def test_execute_sin_consumibles_no_hace_nada(tmp_path):
    f = tmp_path / "autoqueue.md"
    f.write_text("- [ ] duplicada x | a | small\n", encoding="utf-8")
    log = tmp_path / "consumes.jsonl"

    msg = aq.consumir_semilla(execute=True, crear_tarea=lambda: "7",
                              ruta=f, log=log)

    assert msg == ""                              # sin acción -> sin stdout
    assert f.read_text(encoding="utf-8") == "- [ ] duplicada x | a | small\n"
    assert not log.exists()                        # no registra si no hay acción
