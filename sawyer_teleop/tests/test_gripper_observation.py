import pytest
from sawyer_teleop.client import RobotClient
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.protocol import ProtocolError

@pytest.mark.parametrize('trigger,expected', [(0.,-1),(.299999,-1),(.3,1),(1.,1),(None,None)])
def test_gripper_display_threshold_without_arm_motion(trigger,expected):
    backend=FakeBackend()
    core=BridgeCore(backend)
    hello=core.connect()
    client=RobotClient.__new__(RobotClient)
    client.hello=hello
    client.session=hello['session']
    class Link:
        def send(self, msg, latest):
            assert latest=='gripper_observation'
            core.request(msg)
    client.connection=Link()
    client.gripper_observation(trigger)
    assert core.diagnostics()['gripper_observation']['value']==expected
    assert not core.armed
    assert backend.publish_count==0
    core.disconnect(core.session)
    assert core.diagnostics()['gripper_observation'] is None

def test_invalid_gripper_value_rejected():
    core=BridgeCore(FakeBackend())
    hello=core.connect()
    with pytest.raises(ProtocolError):
        core.request({'type':'gripper_observation','session':hello['session'],'value':0})
