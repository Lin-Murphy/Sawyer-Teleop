"""Bounded online joint references; no motion I/O.

Each new goal replaces one cubic Hermite segment, retaining its current
position and velocity. Duration is increased until the *whole* polynomial
satisfies velocity and acceleration limits (including interior extrema).
Acceleration is bounded but may change at a replan; this is not jerk control.
"""
import math

from .protocol import vector


def trajectory_options(options=None):
    options = {} if options is None else dict(options)
    profile = options.get("profile", "cubic")
    if profile not in ("cubic", "responsive", "coordinated"):
        raise ValueError("Unknown joint trajectory profile")
    if profile in ("responsive", "coordinated") and "min_duration_ms" in options:
        raise ValueError("min_duration_ms applies only to the cubic profile")
    defaults = {
        "enabled": True,
        "max_velocity_rad_s": [0.6] * 7,
        "max_acceleration_rad_s2": [1.5] * 7,
        "min_duration_ms": 20.0,
        "max_following_error_rad": 0.5,
    }
    if profile in ("responsive", "coordinated"):
        defaults.pop("min_duration_ms")
    if "profile" in options:
        defaults["profile"] = profile
    if set(options) - set(defaults):
        raise ValueError("Unknown joint trajectory option")
    result = dict(defaults, **options)
    if type(result["enabled"]) is not bool:
        raise ValueError("Joint trajectory enabled must be a boolean")
    for key in ("max_velocity_rad_s", "max_acceleration_rad_s2"):
        values = vector(result[key], 7, key)
        if any(v <= 0 for v in values):
            raise ValueError("Joint velocity and acceleration limits must be greater than zero")
        result[key] = values
    for key, low, high in (("min_duration_ms", 10, 200),
                           ("max_following_error_rad", 0.01, 1.0)):
        if key not in result:
            continue
        value = result[key]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not low <= value <= high):
            raise ValueError("invalid trajectory " + key)
        result[key] = float(value)
    return result


class JointTrajectory:
    """One replaceable, velocity-continuous segment, shared duration for 7 joints."""
    algorithm = "online_cubic_hermite_v2"

    def __init__(self, options=None):
        self.options = trajectory_options(options)
        self.velocity = self.options["max_velocity_rad_s"]
        self.acceleration = self.options["max_acceleration_rad_s2"]
        self.reset()

    def reset(self, q=None, stamp_ns=None):
        self.origin = vector(q, 7) if q is not None else None
        self.goal = list(self.origin) if self.origin is not None else None
        self.start_ns = stamp_ns
        self.last_ns = stamp_ns
        self.duration = 0.0
        self.coefficients = None
        self.end_velocity = [0.0] * 7
        self.goal_stamp_ns = None

    def sample(self, stamp_ns):
        if self.origin is None or self.start_ns is None:
            raise RuntimeError("trajectory_requires_reengage")
        if self.last_ns is not None and stamp_ns < self.last_ns:
            raise RuntimeError("trajectory_clock_regressed")
        self.last_ns = stamp_ns
        elapsed = (stamp_ns - self.start_ns) / 1e9
        if self.coefficients is None:
            return list(self.goal), [0.0] * 7, [0.0] * 7
        if elapsed >= self.duration:
            # A moving endpoint cannot be snapped to zero velocity. If the next
            # goal has not arrived, brake within the original goal's short lease.
            # Hold/disconnect bypass this tail and cancel immediately in BridgeCore.
            q, dq, ddq = [], [], []
            for goal, velocity, limit in zip(self.goal, self.end_velocity, self.acceleration):
                stop_time = abs(velocity)/limit
                t = min(elapsed-self.duration, stop_time)
                acceleration = -math.copysign(limit, velocity)
                q.append(goal + velocity*t + .5*acceleration*t*t)
                dq.append(velocity + acceleration*t if t < stop_time else 0.0)
                ddq.append(acceleration if t < stop_time else 0.0)
            return q, dq, ddq
        u = elapsed / self.duration
        q, dq, ddq = [], [], []
        for origin, (c1, c2, c3) in zip(self.origin, self.coefficients):
            q.append(origin + u * (c1 + u * (c2 + u * c3)))
            dq.append((c1 + u * (2*c2 + 3*u*c3)) / self.duration)
            ddq.append((2*c2 + 6*u*c3) / self.duration**2)
        return q, dq, ddq

    def retarget(self, goal, stamp_ns, goal_stamp_ns=None):
        goal = vector(goal, 7)
        q, dq, _ = self.sample(stamp_ns)
        goal_stamp_ns = stamp_ns if goal_stamp_ns is None else goal_stamp_ns
        interval = ((goal_stamp_ns-self.goal_stamp_ns)/1e9
                    if self.goal_stamp_ns is not None else 0)
        # Estimate endpoint velocity from source sample spacing, not network
        # arrival spacing. This avoids braking to rest at every VR frame.
        end_velocity = ([max(-v, min(v, (new-old)/interval))
                         for new, old, v in zip(goal, self.goal, self.velocity)]
                        if 0 < interval <= .15 else [0.0]*7)
        self.goal_stamp_ns = goal_stamp_ns
        # Repeated goals renew the lease in BridgeCore, not this polynomial.
        # Restarting a stationary goal every packet would never finish it.
        if goal == self.goal and not any(self.end_velocity):
            return
        delta = [g - p for g, p in zip(goal, q)]
        duration = max(
            self.options["min_duration_ms"] / 1000,
            max(abs(d)/v for d, v in zip(delta, self.velocity)),
        )
        if not math.isfinite(duration) or duration > 60:
            raise RuntimeError("trajectory_unfeasible")
        coefficients = self._bounded_curve(delta, dq, end_velocity, duration)
        if coefficients is None:
            # Feasible durations can form several disconnected intervals. A 15%
            # growth search can jump over a short feasible curve and repeatedly
            # choose a slow one, even for a constant goal velocity below vmax.
            # Check the analytic constraint boundaries in increasing time order.
            lower = duration
            for upper in self._duration_boundaries(delta, dq, end_velocity, duration):
                candidate = self._bounded_curve(delta, dq, end_velocity, upper)
                if candidate is not None:
                    duration, coefficients = upper, candidate
                    break
                # Rounding at a boundary must not hide an otherwise feasible
                # interval. Keep a checked feasible bound during this fallback.
                middle = (lower + upper) / 2
                candidate = self._bounded_curve(delta, dq, end_velocity, middle)
                if candidate is not None:
                    upper, coefficients = middle, candidate
                    for _ in range(18):
                        middle = (lower + upper) / 2
                        candidate = self._bounded_curve(delta, dq, end_velocity, middle)
                        if candidate is None:
                            lower = middle
                        else:
                            upper, coefficients = middle, candidate
                    duration = upper
                    break
                lower = upper
        if coefficients is None:
            raise RuntimeError("trajectory_unfeasible")
        self.origin, self.goal = q, goal
        self.coefficients, self.duration = coefficients, duration
        self.end_velocity = end_velocity
        self.start_ns = stamp_ns

    def _duration_boundaries(self, delta, velocity, end_velocity, minimum):
        """Finite candidates where an acceleration or velocity bound is touched."""
        boundaries = {60.0}
        for d, v, end, vmax, amax in zip(
                delta, velocity, end_velocity, self.velocity, self.acceleration):
            equations = []
            # Acceleration extrema are at u=0 and u=1; multiply by T**2.
            for bound in (-amax, amax):
                equations.extend(((bound, 4*v + 2*end, -6*d),
                                  (bound, -2*v - 4*end, 6*d)))
            # An interior velocity extremum touches +/-vmax when the quadratic
            # v(u)-bound has a double root. Its discriminant times T**2 is
            # another quadratic in T. Extraneous roots are harmless: each
            # candidate still undergoes the independent whole-curve check.
            k, l = 4*v + 2*end, 3*(v + end)
            for bound in (-vmax, vmax):
                m = v - bound
                equations.append((k*k - 4*l*m, -12*d*k + 24*d*m, 36*d*d))
            for equation in equations:
                boundaries.update(t for t in _quadratic_roots(*equation)
                                  if minimum < t <= 60.0)
        return sorted(boundaries)

    def _bounded_curve(self, delta, velocity, end_velocity, duration):
        """Return coefficients only if the entire curve respects both limits."""
        coefficients = [(v*duration, 3*d - (2*v+end)*duration,
                         -2*d + (v+end)*duration)
                        for d, v, end in zip(delta, velocity, end_velocity)]
        for (c1, c2, c3), vmax, amax in zip(coefficients, self.velocity, self.acceleration):
            velocities = [c1/duration, (c1 + 2*c2 + 3*c3)/duration]
            if c3 != 0:
                root = -c2/(3*c3)
                if 0 < root < 1:
                    velocities.append((c1 + 2*c2*root + 3*c3*root**2)/duration)
            accelerations = [2*c2/duration**2, (2*c2 + 6*c3)/duration**2]
            if (max(map(abs, velocities)) > vmax*(1 + 1e-12)
                    or max(map(abs, accelerations)) > amax*(1 + 1e-12)):
                return None
        return coefficients


def _quadratic_roots(a, b, c):
    """Real roots without subtracting two nearly equal numbers."""
    scale = max(abs(a), abs(b), abs(c))
    if not scale:
        return ()
    if not math.isfinite(scale):
        raise RuntimeError("trajectory_unfeasible")
    a, b, c = a/scale, b/scale, c/scale
    if not a:
        return (-c/b,) if b else ()
    discriminant = b*b - 4*a*c
    if discriminant < -1e-14*(b*b + abs(4*a*c)):
        return ()
    q = -.5*(b + math.copysign(math.sqrt(max(0., discriminant)), b))
    return (q/a, c/q) if q else (0.,)


class ResponsiveJointTrajectory:
    """Independent minimum-time references with bounded velocity/acceleration.

    Each joint accelerates, cruises if needed, and decelerates to the latest
    goal's estimated velocity. No common arrival time slows unrelated joints.
    Acceleration can change at a phase boundary; there is deliberately no jerk
    smoothing or motion queue. BridgeCore retains authority over every sample.
    """
    algorithm = "online_bounded_acceleration_v1"

    def __init__(self, options=None):
        self.options = trajectory_options(dict(options or {}, profile="responsive"))
        self.velocity = self.options["max_velocity_rad_s"]
        self.acceleration = self.options["max_acceleration_rad_s2"]
        self.reset()

    def reset(self, q=None, stamp_ns=None):
        self.origin = vector(q, 7) if q is not None else None
        self.goal = list(self.origin) if self.origin is not None else None
        self.start_ns = self.last_ns = stamp_ns
        self.goal_stamp_ns = None
        self.end_velocity = [0.0] * 7
        self.initial_velocity = [0.0] * 7
        self.plans = None
        self.duration = 0.0

    def sample(self, stamp_ns):
        if self.origin is None or self.start_ns is None:
            raise RuntimeError("trajectory_requires_reengage")
        if self.last_ns is not None and stamp_ns < self.last_ns:
            raise RuntimeError("trajectory_clock_regressed")
        self.last_ns = stamp_ns
        if self.plans is None:
            return list(self.goal), [0.0] * 7, [0.0] * 7
        points = []
        for q, v, phases, goal, end, limit in zip(
                self.origin, self.initial_velocity, self.plans, self.goal,
                self.end_velocity, self.acceleration):
            remaining = (stamp_ns-self.start_ns)/1e9
            for duration, acceleration in phases:
                if remaining < duration:
                    points.append((q + v*remaining + .5*acceleration*remaining**2,
                                   v + acceleration*remaining, acceleration))
                    break
                q += v*duration + .5*acceleration*duration**2
                v += acceleration*duration
                remaining -= duration
            else:
                # A missing update is not permission to coast indefinitely.
                # Brake within the original source lease; hold bypasses this.
                stop_time = abs(end)/limit
                elapsed = min(remaining, stop_time)
                acceleration = -math.copysign(limit, end)
                points.append((goal + end*elapsed + .5*acceleration*elapsed**2,
                               end + acceleration*elapsed if elapsed < stop_time else 0.0,
                               acceleration if elapsed < stop_time else 0.0))
        return tuple([point[i] for point in points] for i in range(3))

    def retarget(self, goal, stamp_ns, goal_stamp_ns=None):
        goal = vector(goal, 7)
        q, dq, _ = self.sample(stamp_ns)
        goal_stamp_ns = stamp_ns if goal_stamp_ns is None else goal_stamp_ns
        if goal_stamp_ns == self.goal_stamp_ns and goal == self.goal:
            return  # Reusing one VR sample must not invent a zero-velocity endpoint.
        interval = ((goal_stamp_ns-self.goal_stamp_ns)/1e9
                    if self.goal_stamp_ns is not None else 0.0)
        end_velocity = ([max(-limit, min(limit, (new-old)/interval))
                         for new, old, limit in zip(goal, self.goal, self.velocity)]
                        if 0 < interval <= .15 else [0.0]*7)
        self.goal_stamp_ns = goal_stamp_ns
        if goal == self.goal and not any(self.end_velocity):
            return
        plans = [self._plan(g-p, v, end, vmax, amax)
                 for g, p, v, end, vmax, amax in zip(
                     goal, q, dq, end_velocity, self.velocity, self.acceleration)]
        duration = max(sum(t for t, _ in phases) for phases in plans)
        if not math.isfinite(duration) or duration > 60:
            raise RuntimeError("trajectory_unfeasible")
        self.origin, self.initial_velocity = q, dq
        self.goal, self.end_velocity = goal, end_velocity
        self.plans, self.duration, self.start_ns = plans, duration, stamp_ns

    @staticmethod
    def _plan(distance, initial, final, vmax, amax):
        # Enumerate both acceleration directions. A reversal may first need to
        # brake beyond the goal; selecting only sign(distance) misses that case.
        candidates = []
        for direction in (1, -1):
            acceleration = direction*amax
            square = direction*amax*distance + .5*(initial**2 + final**2)
            if square >= -1e-12:
                peak = direction*math.sqrt(max(0.0, square))
                up, down = (peak-initial)/acceleration, (peak-final)/acceleration
                if abs(peak) <= vmax+1e-12 and min(up, down) >= -1e-10:
                    candidates.append([(max(0.0, up), acceleration),
                                       (max(0.0, down), -acceleration)])
            peak = direction*vmax
            up, down = (peak-initial)/acceleration, (peak-final)/acceleration
            ramp_distance = .5*(initial+peak)*up + .5*(final+peak)*down
            cruise = (distance-ramp_distance)/peak
            if min(up, cruise, down) >= -1e-10:
                candidates.append([(max(0.0, up), acceleration),
                                   (max(0.0, cruise), 0.0),
                                   (max(0.0, down), -acceleration)])
        checked = []
        for phases in candidates:
            position, velocity = 0.0, initial
            valid = abs(velocity) <= vmax+1e-10
            for duration, acceleration in phases:
                position += velocity*duration + .5*acceleration*duration**2
                velocity += acceleration*duration
                # Velocity is linear in each phase, so its extrema are at ends.
                valid = valid and all(map(math.isfinite, (duration, position, velocity)))
                valid = valid and abs(velocity) <= vmax+1e-10 and abs(acceleration) <= amax
            if (valid and abs(position-distance) <= 1e-9*(1+abs(distance))
                    and abs(velocity-final) <= 1e-9):
                checked.append(phases)
        if not checked:
            raise RuntimeError("trajectory_unfeasible")
        return min(checked, key=lambda phases: sum(t for t, _ in phases))


class CoordinatedJointTrajectory:
    """One latest-goal, synchronized seven-axis acceleration-limited trajectory.

    Ruckig performs local state-to-state planning (no waypoints/cloud service).
    Phase synchronization preserves a joint-space line when the boundary states
    permit it, otherwise time synchronization keeps a common arrival time.
    This is NOT an exact Cartesian interpolator; recorded FK/path tests remain
    mandatory. No queue, fixed smoothing window, jerk limit or future-position
    prediction is added. The bridge owns freshness and immediate cancellation.
    """
    algorithm = "online_coordinated_acceleration_v1"

    def __init__(self, options=None):
        try:
            from ruckig import Ruckig, InputParameter, Trajectory, Synchronization, __version__
        except ImportError as exc:
            raise ValueError("coordinated_planner_unavailable_install_ruckig_0_12_2") from exc
        if __version__ != "0.12.2":
            raise ValueError("coordinated_planner_version_mismatch_requires_0_12_2")
        self.options = trajectory_options(dict(options or {}, profile="coordinated"))
        self.velocity = self.options["max_velocity_rad_s"]
        self.acceleration = self.options["max_acceleration_rad_s2"]
        self._solver = Ruckig(7)
        self._input_type, self._trajectory_type = InputParameter, Trajectory
        self._synchronization = Synchronization.Phase
        self.reset()

    def reset(self, q=None, stamp_ns=None):
        self.origin = vector(q, 7) if q is not None else None
        self.goal = list(self.origin) if self.origin is not None else None
        self.start_ns = self.last_ns = stamp_ns
        self.goal_stamp_ns = None
        self.end_velocity = [0.] * 7
        self.duration = 0.
        self.curve = None

    def sample(self, stamp_ns):
        if self.origin is None or self.start_ns is None:
            raise RuntimeError("trajectory_requires_reengage")
        if self.last_ns is not None and stamp_ns < self.last_ns:
            raise RuntimeError("trajectory_clock_regressed")
        self.last_ns = stamp_ns
        if self.curve is None:
            return list(self.goal), [0.] * 7, [0.] * 7
        elapsed = (stamp_ns-self.start_ns)/1e9
        if elapsed <= self.duration:
            return self.curve.at_time(elapsed)
        # Missing updates cannot authorize endless extrapolation. All axes
        # brake together; the original source lease still applies every tick.
        duration = max(abs(v)/a for v, a in zip(self.end_velocity, self.acceleration))
        t = min(elapsed-self.duration, duration)
        if not duration:
            return list(self.goal), [0.] * 7, [0.] * 7
        q = [g+v*(t-.5*t*t/duration) for g, v in zip(self.goal, self.end_velocity)]
        dq = [v*(1-t/duration) for v in self.end_velocity]
        ddq = [-v/duration if t < duration else 0. for v in self.end_velocity]
        return q, dq, ddq

    def retarget(self, goal, stamp_ns, goal_stamp_ns=None):
        goal = vector(goal, 7)
        q, dq, ddq = self.sample(stamp_ns)
        goal_stamp_ns = stamp_ns if goal_stamp_ns is None else goal_stamp_ns
        if goal_stamp_ns == self.goal_stamp_ns and goal == self.goal:
            return
        dt = ((goal_stamp_ns-self.goal_stamp_ns)/1e9
              if self.goal_stamp_ns is not None else 0.)
        end = ([(new-old)/dt for new, old in zip(goal, self.goal)]
               if 0 < dt <= .15 else [0.] * 7)
        # Apply common scales to source velocity and its change (using the
        # configured acceleration, no additional smoothing time constant).
        # Also limit terminal kinetic energy to what is reachable over the
        # remaining distance. Without this bound a noisy velocity estimate can
        # make a solver deliberately go past/away from a nearby endpoint.
        # Tiny inward margins avoid binary roundoff at exact solver bounds.
        scale = (1.-1e-12)/max(1., max(abs(v)/limit for v, limit in zip(end, self.velocity)))
        end = [scale*v for v in end]
        if 0 < dt <= .15:
            change = [v-old for v, old in zip(end, self.end_velocity)]
            ratio = 1./max(1., max(abs(v)/(a*dt) for v, a in zip(change, self.acceleration)))
            end = [old+ratio*v for old, v in zip(self.end_velocity, change)]
        scale = 1.
        for v, current, target, position, a in zip(end, dq, goal, q, self.acceleration):
            if abs(v) > 1e-8:
                scale = min(scale, (1.-1e-9)*math.sqrt(current*current+2*a*abs(target-position))/abs(v))
        end = [scale*v for v in end]
        self.goal_stamp_ns = goal_stamp_ns
        if goal == self.goal and not any(self.end_velocity):
            return
        parameters = self._input_type(7)
        parameters.current_position, parameters.current_velocity = q, dq
        parameters.current_acceleration = ddq
        parameters.target_position, parameters.target_velocity = goal, end
        parameters.max_velocity, parameters.max_acceleration = self.velocity, self.acceleration
        parameters.synchronization = self._synchronization
        delta = [g-p for g, p in zip(goal, q)]
        if not any(dq) and not any(end) and any(delta):
            speed = min(v/abs(d) for v, d in zip(self.velocity, delta) if d)
            acceleration = min(a/abs(d) for a, d in zip(self.acceleration, delta) if d)
            parameters.max_velocity = [abs(d)*speed if d else v for d, v in zip(delta, self.velocity)]
            parameters.max_acceleration = [abs(d)*acceleration if d else a for d, a in zip(delta, self.acceleration)]
        curve = self._trajectory_type(7)
        try:
            result = self._solver.calculate(parameters, curve)
        except Exception as exc:
            raise RuntimeError("trajectory_unfeasible") from exc
        if int(result) < 0 or not math.isfinite(curve.duration) or curve.duration > 60:
            raise RuntimeError("trajectory_unfeasible")
        self.origin, self.goal, self.end_velocity = q, goal, end
        self.curve, self.duration, self.start_ns = curve, curve.duration, stamp_ns


def make_trajectory(options=None):
    options = trajectory_options(options)
    if options.get("profile") == "coordinated":
        return CoordinatedJointTrajectory(options)
    if options.get("profile", "cubic") == "responsive":
        return ResponsiveJointTrajectory(options)
    return JointTrajectory(options)
