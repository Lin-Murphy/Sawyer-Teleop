"""Pure read-only display geometry for the local web interface."""
import time
import numpy as np
from scipy.spatial.transform import Rotation
from .kinematics_model import model_digest
from .protocol import JOINTS, vector


def feedback_message(operator):
    """Read the existing client cache. Never open a connection or send a command."""
    if operator is None:
        return {'session': None, 'status': 'Not connected'}
    robot = operator.robot
    hello = robot.hello or {}
    result = dict(session=hello.get('session'), model=hello.get('kinematics'),
                  backend=hello.get('backend', ''), status='Waiting for feedback')
    try:
        state = robot.snapshot()
        result.update(q=state['sample']['q'], source_seq=state['sample']['seq'],
                      expires_ns=state['a_received_ns'] +
                      int((robot.feedback_timeout-state['age_s'])*1e9),
                      status='Live feedback')
    except (ConnectionError, ValueError, KeyError) as exc:
        result['status'] = 'Feedback unavailable · ' + str(exc)
    return result


class DisplayChain:
    """Only forward transforms of the model supplied by this B session."""
    def __init__(self, model):
        if (not isinstance(model, dict) or model.get('available') is not True
                or model.get('schema') != 1 or model.get('joints') != JOINTS
                or model.get('base_frame') != 'base' or model.get('sha256') != model_digest(model)
                or not isinstance(model.get('chain'), list) or not 7 <= len(model['chain']) <= 32):
            raise ValueError('B model unavailable or invalid')
        self.chain, names = [], []
        for item in model['chain']:
            origin = np.eye(4)
            origin[:3, 3] = vector(item.get('xyz'), 3)
            origin[:3, :3] = Rotation.from_euler('xyz', vector(item.get('rpy'), 3)).as_matrix()
            axis = None
            if item.get('type') == 'revolute':
                axis = np.array(vector(item.get('axis'), 3))
                if abs(np.linalg.norm(axis)-1) > 1e-9:
                    raise ValueError('Invalid model axis')
                names.append(item.get('name'))
            elif item.get('type') != 'fixed':
                raise ValueError('Unsupported display joint')
            self.chain.append((origin, axis))
        if names != JOINTS:
            raise ValueError('B joint order mismatch')
        self.tip_frame = model.get('tip_frame', '')

    def frames(self, q):
        q = vector(q, 7)
        transform, index = np.eye(4), 0
        frames = [transform.copy()]
        for origin, axis in self.chain:
            transform = transform @ origin
            if axis is not None:
                transform[:3, :3] = transform[:3, :3] @ Rotation.from_rotvec(axis*q[index]).as_matrix()
                index += 1
            frames.append(transform.copy())
        return np.asarray(frames)


class Posture:
    def __init__(self):
        self.session = self.model_key = self.chain = self.frames = self.q = None
        self.expires_ns = 0
        self.status = 'Waiting for B feedback'
        self.backend = ''

    def accept(self, packet, now=None):
        now = time.monotonic_ns() if now is None else now
        session = packet.get('session')
        if session != self.session:
            self.__init__()
            self.session = session
        self.backend = packet.get('backend', '')
        self.status = packet.get('status', 'Waiting for feedback')
        self.expires_ns = 0
        if not session:
            self.frames = self.q = None
            return
        model = packet.get('model') or {}
        key = model.get('sha256')
        try:
            if key != self.model_key or self.chain is None:
                self.frames = self.q = None
                self.chain = DisplayChain(model)
                self.model_key = key
            deadline = packet.get('expires_ns', 0)
            # Never let a delayed IPC packet revive an expired observation.
            if packet.get('q') is not None and now < deadline:
                frames = self.chain.frames(packet['q'])
                self.q, self.frames = list(packet['q']), frames
                self.expires_ns = deadline
            elif packet.get('q') is not None:
                self.status = 'Feedback expired'
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self.frames = self.q = None
            self.chain = None
            self.status = str(exc)

    def fresh(self, now=None):
        return self.frames is not None and (time.monotonic_ns() if now is None else now) < self.expires_ns


