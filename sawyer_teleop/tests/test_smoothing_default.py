"""New A installs start with Smooth teleop on at the DROID-like 150 ms strength (user choice 2026-11-07)."""
import json
from pathlib import Path

from sawyer_teleop.motion import PoseSmoother

ROOT = Path(__file__).resolve().parents[1]


def test_template_defaults_to_droid_like_smoothing():
    motion = json.loads((ROOT/'config/operator.json').read_text(encoding='utf-8'))['motion']
    assert motion == {'enabled': True, 'time_constant_ms': 150.0}
    assert PoseSmoother(motion).description()['time_constant_ms'] == 150.0


def test_web_strength_menu_offers_the_default():
    html = (ROOT/'src/sawyer_teleop/web/index.html').read_text(encoding='utf-8')
    assert '<option value="150">DROID-like · 150 ms</option>' in html
