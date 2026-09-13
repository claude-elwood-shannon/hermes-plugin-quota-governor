#!/usr/bin/env python3
"""autoqueue script."""

import argparse
import json
import os
from pathlib import Path

DEFAULT_FILE = Path(os.getenv('AUTOQUEUE_FILE', '~/.hermes/data/autoqueue.md')).expanduser()
DEFAULT_LOG = Path('~/.hermes/quota-governor/autoqueue-consumes.jsonl').expanduser()

def parsear_semillas(ruta: Path):
    semillas = []
    for line in ruta.read_text().splitlines():
        if not (line.startswith('- [ ]') or line.startswith('- [ ]')):
            continue
        parts = line.split('|')
        if len(parts) < 3:
            continue
        desc = parts[0].replace('- [ ]', '').strip()
        perfil = parts[1].strip()
        coste = parts[2].strip()
        consumible = True
        for note in ('duplicada', 'rota', 'tarea existente'):
            if note in desc.lower():
                consumible = False
                break
        if '(t_' in desc:
            consumible = False
        semillas.append({'desc': desc, 'perfil': perfil, 'coste': coste, 'consumible': consumible, 'line': line})
    return semillas

def generar_semillas():
    return []

def consumir_semilla(execute=False, crear_tarea=None):
    semillas = parsear_semillas(DEFAULT_FILE)
    consumibles = [s for s in semillas if s['consumible']]
    if not consumibles:
        print('NO hay semillas consumibles')
        return
    semilla = consumibles[0]
    with DEFAULT_LOG.open('a') as f:
        f.write(json.dumps({'semilla': semilla['desc'], 'timestamp': os.getenv('TIMESTAMP')}) + '\n')
    if not execute:
        print('dry-run:', semilla['desc'])
        return
    if crear_tarea:
        tarea_id = crear_tarea()
        new_line = semilla['line'] + f' (t_{tarea_id})'
        content = DEFAULT_FILE.read_text().replace(semilla['line'], new_line)
        DEFAULT_FILE.write_text(content)
        print('marcado y marcada semilla')
    else:
        print('execute sin crear')

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--execute', action='store_true')
    p.add_argument('--profile', default=None)
    args = p.parse_args()
    if args.execute:
        def dummy_tarea():
            return '123456'
        consumir_semilla(execute=True, crear_tarea=dummy_tarea)
    else:
        consumir_semilla()
