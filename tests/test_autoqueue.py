import os
import tempfile
from pathlib import Path

# Import the script module
import sys
sys.path.append('/data/git/hermes-plugin-quota-governor/scripts')
import autoqueue


def test_parse_valid_line(tmp_path):
    file = tmp_path/'autoqueue.md'
    file.write_text(' - [ ] test task | pr-ollama | small\n')
    os.environ['AUTOQUEUE_FILE']=str(file)
    semillas = autoqueue.parsear_semillas(Path(file))
    assert len(semillas)==1
    assert semillas[0]['desc']=='test task'
    assert semillas[0]['consumible']


def test_parse_non_consumible(tmp_path):
    file = tmp_path/'autoqueue.md'
    file.write_text(' - [ ] test task (t_123) | pr-ollama | small\n')
    os.environ['AUTOQUEUE_FILE']=str(file)
    semillas = autoqueue.parsear_semillas(Path(file))
    assert not any(s['consumible'] for s in semillas)


def test_dry_run(tmp_path, capsys):
    file = tmp_path/'autoqueue.md'
    file.write_text(' - [ ] dry task | pr-ollama | small\n')
    os.environ['AUTOQUEUE_FILE']=str(file)
    autoqueue.consumir_semilla(execute=False)
    captured = capsys.readouterr()
    assert 'dry-run:' in captured.out

@tempfile
def test_execute_and_mark(tmp_path, capsys):
    file = tmp_path/'autoqueue.md'
    file.write_text(' - [ ] exec task | pr-ollama | small\n')
    os.environ['AUTOQUEUE_FILE']=str(file)
    def dummy():
        return '999'
    autoqueue.consumir_semilla(execute=True, crear_tarea=dummy)
    new_content = file.read_text()
    assert '(t_999)' in new_content
    captured = capsys.readouterr()
    assert 'marcado' in captured.out
