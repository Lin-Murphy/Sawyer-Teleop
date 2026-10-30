from sawyer_teleop.input_status import describe_input, expire_status
from sawyer_teleop.protocol import ns


def sample(tracked=True, active=True):
    return {'valid': tracked, 'received_ns': ns(), 'reason': 'ready', 'packet': {
        'focused': True, 'mounted': True, 'head_status': 15,
        'hands': {'r': {'input_ok': active, 'tracking_ok': tracked, 'status': 15,
                        'buttons': 1, 'grip': .8, 'trigger': .4,
                        'pose': {'p': [1., 2., 3.]}}}}}


def test_tracking_loss_keeps_valid_buttons_but_not_pose():
    state = describe_input(sample(tracked=False))
    assert not state['hands']['r']['tracked']
    assert state['hands']['r']['position'] is None
    assert state['hands']['r']['buttons'] == 1


def test_inactive_input_never_displays_stale_button():
    state = describe_input(sample(active=False))
    assert state['hands']['r']['buttons'] is None
    assert state['hands']['r']['grip'] is None


def test_stalled_gui_expires_buttons_even_without_new_operator_tick():
    state = describe_input(sample())
    assert state['hands']['r']['buttons'] == 1
    state['received_ns'] -= 1_000_000_000
    expired = expire_status(state)
    assert not expired['fresh']
    assert expired['hands']['r']['buttons'] is None
    assert expired['hands']['r']['position'] is None


def test_bad_raw_packet_does_not_crash_status_display():
    for packet in ([], {'head_status': 'bad', 'hands': []},
                   {'hands': {'r': {'input_ok': True, 'status': 'bad', 'buttons': [],
                                    'grip': 'bad', 'pose': {'p': ['bad']}}}}):
        state = describe_input({'valid': False, 'reason': 'invalid_packet',
                                'received_ns': ns(), 'packet': packet})
        assert state['hands']['r']['buttons'] is None
        assert state['hands']['r']['position'] is None
