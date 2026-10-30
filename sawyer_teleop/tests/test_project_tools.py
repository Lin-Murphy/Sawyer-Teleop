"""Retained developer entry points must include their actual dependencies."""
from pathlib import Path
import runpy
import sys
import types

ROOT = Path(__file__).resolve().parents[1]


def test_windows_scene_entry_uses_common_initializer(monkeypatch):
    calls = []
    common = types.ModuleType('initialize_gazebo_scene')
    common.main = lambda **kwargs: calls.append(kwargs)
    monkeypatch.setitem(sys.modules, 'initialize_gazebo_scene', common)
    runpy.run_path(str(ROOT/'scripts/initialize_windows_sim.py'), run_name='__main__')
    assert calls == [{'node_name': 'windows_sim_initialize'}]
