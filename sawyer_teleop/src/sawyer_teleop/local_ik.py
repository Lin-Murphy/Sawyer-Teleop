"""A-only bounded, warm-started IK. No ROS, devices, random restarts or angle wrapping."""
import math
import time

import numpy as np
from scipy.spatial.transform import Rotation

from .kinematics_model import model_digest
from .protocol import JOINTS, pose, vector
from .a_timing import LOCAL_IK_BUDGET_S
from .joint_protection import MAX_STEP_RAD


class LocalIK:
    position_tolerance = 1e-5
    rotation_tolerance = 1e-4
    max_seed_distance = MAX_STEP_RAD

    def __init__(self, model):
        if (not isinstance(model, dict) or model.get('available') is not True
                or model.get('schema') != 1 or model.get('joints') != JOINTS
                or model.get('base_frame') != 'base'
                or model.get('sha256') != model_digest(model)):
            raise ValueError('local_ik_model_unavailable_or_invalid')
        self.model = model
        self.chain, self.lower, self.upper = [], [], []
        moving = []
        if not isinstance(model.get('chain'), list) or not 7 <= len(model['chain']) <= 32:
            raise ValueError('local_ik_model_chain_invalid')
        for item in model['chain']:
            origin = np.eye(4)
            origin[:3, 3] = vector(item.get('xyz'), 3)
            origin[:3, :3] = Rotation.from_euler('xyz', vector(item.get('rpy'), 3)).as_matrix()
            kind = item.get('type')
            if kind == 'fixed':
                self.chain.append((origin, None, None, None))
                continue
            if kind != 'revolute':
                raise ValueError('local_ik_model_joint_invalid')
            moving.append(item.get('name'))
            lo, hi = vector([item.get('lower'), item.get('upper')], 2)
            if lo >= hi:
                raise ValueError('local_ik_model_limits_invalid')
            self.lower.append(lo); self.upper.append(hi)
            axis = np.array(vector(item.get('axis'), 3))
            if abs(np.linalg.norm(axis)-1) > 1e-9:
                raise ValueError('local_ik_model_axis_invalid')
            x, y, z = axis
            skew = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
            self.chain.append((origin, axis, skew, skew@skew))
        if moving != JOINTS:
            raise ValueError('local_ik_model_joint_order_invalid')
        self.lower, self.upper = np.array(self.lower), np.array(self.upper)

    def forward(self, q, jacobian=False):
        q = np.asarray(vector(list(q), 7))
        transform = np.eye(4)
        axes, origins, index = [], [], 0
        for origin, axis, skew, square in self.chain:
            transform = transform @ origin
            if axis is not None:
                if jacobian:
                    axes.append(transform[:3, :3]@axis)
                    origins.append(transform[:3, 3].copy())
                rotation = np.eye(3)+math.sin(q[index])*skew+(1-math.cos(q[index]))*square
                transform[:3, :3] = transform[:3, :3]@rotation
                index += 1
        if not jacobian:
            return transform
        axes, origins = np.array(axes), np.array(origins)
        return transform, np.vstack((np.cross(axes, transform[:3, 3]-origins).T, axes.T))

    def solve(self, target, seed, timeout=LOCAL_IK_BUDGET_S, cancelled=None):
        target = pose(target)
        q0 = np.array(vector(seed, 7, 'IK seed'))
        if np.any(q0 < self.lower) or np.any(q0 > self.upper):
            raise ValueError('local_ik_seed_outside_limits')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('local_ik_timeout_invalid')
        deadline = time.perf_counter()+timeout
        goal_p = np.array(target['p'])
        goal_r = Rotation.from_quat(target['q']).as_matrix()
        lower = np.maximum(self.lower, q0-self.max_seed_distance)
        upper = np.minimum(self.upper, q0+self.max_seed_distance)
        q = q0.copy()

        def error(transform):
            return np.r_[goal_p-transform[:3, 3],
                         Rotation.from_matrix(goal_r@transform[:3, :3].T).as_rotvec()]

        for _ in range(60):
            if time.perf_counter() >= deadline or (cancelled is not None and cancelled()):
                return None
            transform, jac = self.forward(q.tolist(), True)
            residual = error(transform)
            if (np.linalg.norm(residual[:3]) <= self.position_tolerance
                    and np.linalg.norm(residual[3:]) <= self.rotation_tolerance):
                return q.tolist()
            # Damped minimum-norm step preserves the nearby redundant posture.
            # Lock axes that would leave their bounds, then solve the remaining axes.
            active = np.ones(7, dtype=bool)
            step = np.zeros(7)
            for _active in range(8):
                j = jac[:, active]
                if j.shape[1] == 0:
                    return None
                step[:] = 0
                step[active] = j.T @ np.linalg.solve(j@j.T+np.eye(6)*1e-8, residual)
                blocked = ((q <= lower+1e-9) & (step < 0)) | ((q >= upper-1e-9) & (step > 0))
                if not np.any(blocked & active):
                    break
                active &= ~blocked
            step *= min(1., .08/max(1e-12, np.max(abs(step))))
            scale, improved = 1., False
            for _line in range(10):
                if time.perf_counter() >= deadline or (cancelled is not None and cancelled()):
                    return None
                candidate = np.clip(q+scale*step, lower, upper)
                candidate_error = error(self.forward(candidate.tolist()))
                if np.dot(candidate_error, candidate_error) < np.dot(residual, residual):
                    q, improved = candidate, True
                    break
                scale *= .5
            if not improved:
                return None
        return None
