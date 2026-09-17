#!/usr/bin/env python3
"""Offline unit tests for the pure security guards of the Open WebUI bridge.

Covers two untested functions of scripts/bridge/open-webui-bridge.py:

  * ``_safe_join(base, name)`` — the path-traversal defense used by
    ``_handle_file`` (HERMES_HOME) and the capabilities endpoints.
  * ``_bw_gpu_parse_smi(smi, data)`` — parses the ``nvidia-smi`` CSV line
    for the ml-host GPU probe.

The bridge module is imported by file path (same technique as
tests/test_bridge_file_endpoint.py) with spec_from_file_location. Verified
during authoring: importing the module has NO side effects — it neither
starts the HTTP server (guarded by ``__main__``) nor touches SSH/network;
all subprocess/urllib calls live inside functions. No mocking is needed
before exec_module.

These tests pin the REAL behavior of the module, including two quirks
reported to the card author (module intentionally NOT modified):

  1. ``_safe_join`` allows ``name`` that resolves to the base itself
     (``''``, ``'.'``, or base repeated) because the rejection condition is
     ``p != base and not p.startswith(base + os.sep)``.
  2. ``_bw_gpu_parse_smi`` receives the FULL nvidia-smi stdout; on a host
     with 2+ GPUs that stdout is multi-line, the comma split yields
     ``"5\\n62"``-style fragments for the util field and the int() call
     raises mid-assignment: the three first-GPU values are kept (partial
     mutation) and ``util_pct`` is silently dropped. util_pct is only
     ever present when stdout is a single CSV line. A latent limitation,
     documented by test, not fixed here.
"""
import importlib.util
import os

import pytest

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                      "scripts", "bridge", "open-webui-bridge.py")

_spec = importlib.util.spec_from_file_location("bridge_guards_under_test",
                                               BRIDGE)
assert _spec is not None and _spec.loader is not None  # noqa: E501 (static narrowing)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


# ----------------------------------------------------------- _safe_join

def test_safe_join_normal_path_resolved_inside_base(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("x", encoding="utf-8")
    got = bridge._safe_join(str(tmp_path), "config.yaml")
    assert got == str(target.resolve())


def test_safe_join_nested_subdir_inside_base(tmp_path):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    got = bridge._safe_join(str(tmp_path), os.path.join("a", "b", "f.txt"))
    assert got == str((sub / "f.txt").resolve())


def test_safe_join_dotdot_escape_returns_none(tmp_path):
    got = bridge._safe_join(str(tmp_path), os.path.join("..", "secret.txt"))
    assert got is None


def test_safe_join_deep_dotdot_escape_returns_none(tmp_path):
    got = bridge._safe_join(str(tmp_path),
                            os.path.join("a", "..", "..", "etc", "passwd"))
    assert got is None


def test_safe_join_absolute_path_outside_base_returns_none(tmp_path):
    got = bridge._safe_join(str(tmp_path), "/etc/passwd")
    assert got is None


def test_safe_join_empty_name_allowed_returns_base(tmp_path):
    # REAL behavior: p == base passes the guard (documented quirk #1).
    got = bridge._safe_join(str(tmp_path), "")
    assert got == str(tmp_path.resolve())


def test_safe_join_base_as_name_allowed_returns_base(tmp_path):
    got = bridge._safe_join(str(tmp_path), str(tmp_path))
    assert got == str(tmp_path.resolve())


def test_safe_join_dot_allowed_returns_base(tmp_path):
    got = bridge._safe_join(str(tmp_path), ".")
    assert got == str(tmp_path.resolve())


def test_safe_join_symlink_inside_base_pointing_outside_returns_none(tmp_path):
    outside = tmp_path.parent / "t32aa5d2c_outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    os.symlink(str(outside), str(link))
    try:
        # realpath resolves the symlink, so the resolved path escapes base.
        assert bridge._safe_join(str(tmp_path), "link.txt") is None
    finally:
        os.unlink(str(link))
        outside.unlink()


def test_safe_join_nonexistent_path_inside_base_allowed(tmp_path):
    # The guard is purely lexical: a path that does not exist (yet) but
    # resolves inside base is accepted.
    got = bridge._safe_join(str(tmp_path), os.path.join("no", "such", "f.txt"))
    assert got == str((tmp_path / "no" / "such" / "f.txt").resolve())


def test_safe_join_nul_byte_raises_value_error(tmp_path):
    # REAL behavior: os.path.realpath raises on embedded NUL; the guard
    # does not catch it. Callers must not pass unfiltered names.
    with pytest.raises(ValueError):
        bridge._safe_join(str(tmp_path), "bad\x00name")


def test_safe_join_base_symlink_resolved_to_realpath(tmp_path):
    # base itself is realpath'ed first, so a symlinked base still works.
    real = tmp_path / "real_dir"
    real.mkdir()
    link_base = tmp_path / "link_dir"
    os.symlink(str(real), str(link_base))
    try:
        got = bridge._safe_join(str(link_base), "f.txt")
        assert got == str((real / "f.txt").resolve())
    finally:
        os.unlink(str(link_base))


# -------------------------------------------------- _bw_gpu_parse_smi

def test_parse_smi_realistic_single_gpu_line():
    data = {}
    bridge._bw_gpu_parse_smi("61, 2345, 24564, 37", data)
    assert data == {
        "temp_c": 61,
        "vram_used_mib": 2345,
        "vram_total_mib": 24564,
        "util_pct": 37,
    }


def test_parse_smi_whitespace_and_extra_fields():
    data = {}
    bridge._bw_gpu_parse_smi(" 62 , 100 , 24564 , 5 , extra , junk ", data)
    assert data == {
        "temp_c": 62,
        "vram_used_mib": 100,
        "vram_total_mib": 24564,
        "util_pct": 5,
    }


def test_parse_smi_multiline_two_gpus_partial_mutation_quirk():
    # REAL behavior on a 2-GPU host: stdout is multi-line, the comma split
    # yields "5\n62" for the util field, int() raises mid-assignment and
    # the ValueError is swallowed: the three GPU-0 values stay in data and
    # util_pct is silently dropped (documented quirk #2, not fixed here).
    data = {}
    smi = "61, 100, 24564, 5\n62, 300, 24564, 12"
    bridge._bw_gpu_parse_smi(smi, data)
    assert data == {
        "temp_c": 61,
        "vram_used_mib": 100,
        "vram_total_mib": 24564,
    }


def test_parse_smi_empty_smi_noop():
    data = {}
    bridge._bw_gpu_parse_smi("", data)
    assert data == {}


def test_parse_smi_none_smi_noop():
    data = {}
    bridge._bw_gpu_parse_smi(None, data)
    assert data == {}


def test_parse_smi_truncated_line_noop():
    data = {}
    bridge._bw_gpu_parse_smi("61, 2345", data)
    assert data == {}


def test_parse_smi_garbage_never_raises():
    data = {}
    bridge._bw_gpu_parse_smi("not,a,number,at,all", data)
    assert data == {}


def test_parse_smi_bad_first_field_leaves_data_untouched():
    data = {}
    bridge._bw_gpu_parse_smi("NaN, 1, 2, 3", data)
    assert data == {}


def test_parse_smi_partial_mutation_when_later_field_bad():
    # REAL behavior: fields are assigned in order; if a later field fails
    # to parse, earlier ones are already mutated into data.
    data = {}
    bridge._bw_gpu_parse_smi("61, 2345, oops, 37", data)
    assert data == {"temp_c": 61, "vram_used_mib": 2345}


def test_parse_smi_preserves_existing_keys_on_garbage():
    data = {"temp_c": 99}
    bridge._bw_gpu_parse_smi("garbage line without commas", data)
    assert data == {"temp_c": 99}
