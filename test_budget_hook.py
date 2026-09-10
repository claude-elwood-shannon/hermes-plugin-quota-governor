#!/usr/bin/python3.12
"""test_budget_hook.py — tests del spawn de budget_check en el hook
kanban_task_claimed del plugin (F3, OBJ-24). Verifica que el hook invoca
_spawn_budget_check con el task_id, que es best-effort (script ausente o
spawn fallido no rompen el hook) y que sigue existiendo el wiring.

Importa el plugin como package (sys.path al padre + import real) porque
__init__.py usa imports relativos.
"""
import os
import sys
import unittest
from unittest import mock

# importar el plugin como package real: sys.path incluye el PADRE del plugin
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
import hermes_plugin_quota_governor as plug  # noqa: E402


class TestBudgetHook(unittest.TestCase):
    def _claim(self, **kwargs):
        # hook completo con quota mockeada (no llama APIs reales)
        with mock.patch.object(plug.gov, "query_quota",
                               side_effect=RuntimeError("no api in test")):
            plug._on_kanban_task_claimed(**kwargs)

    def test_hook_invoca_spawn_con_task_id(self):
        with mock.patch.object(plug, "_spawn_budget_check") as sp:
            self._claim(task_id="t_x", assignee="pr-ollama", run_id=1)
            sp.assert_called_once_with("t_x")

    def test_hook_sin_task_id_no_spawn(self):
        with mock.patch.object(plug, "_spawn_budget_check") as sp:
            self._claim(assignee="pr-ollama", run_id=1)
            sp.assert_not_called()

    def test_spawn_fallido_no_rompe(self):
        with mock.patch.object(plug, "_spawn_budget_check",
                               side_effect=OSError("boom")):
            self._claim(task_id="t_y", assignee="pr-ollama", run_id=1)  # no raise

    def test_spawn_script_ausente_silencioso(self):
        with mock.patch.object(plug.os.path, "exists", return_value=False):
            plug._spawn_budget_check("t_z")  # no raise, no spawn

    def test_spawn_usa_python312_y_detached(self):
        with mock.patch.object(plug.os.path, "exists", return_value=True), \
             mock.patch.object(plug.subprocess, "Popen") as popen:
            plug._spawn_budget_check("t_w")
            args, kwargs = popen.call_args
            self.assertEqual(args[0][0], "/usr/bin/python3.12")
            self.assertIn("budget_check.py", args[0][1])
            self.assertTrue(kwargs.get("start_new_session"))

    def test_wiring_del_hook_no_cambiado(self):
        import inspect
        src = inspect.getsource(plug.register)
        self.assertIn('"kanban_task_claimed"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)