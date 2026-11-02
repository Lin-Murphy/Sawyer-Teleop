"""Optional B-side Dynamixel driver. Only the worker owns the serial port.

Ticks and register settings come from gripper-integration (lab-tested hardware).
Pause holds position with torque enabled; shutdown also holds, then closes the
port without disabling torque. Neither action automatically releases an object.
"""
from collections import deque
import threading
import time

OPEN, CLOSED = 3766, 1024


class DynamixelTransport:
    def __init__(self, device):
        from dynamixel_sdk import PortHandler, PacketHandler
        self.port = PortHandler(device)
        self.packet = PacketHandler(2.0)
        self.port.getCurrentTime = lambda: time.perf_counter()*1000.
        # SDK computes a packet timeout for every transaction. Cap its wait;
        # this is not an end-to-end real-time guarantee for OS/USB operations.
        original = self.port.setPacketTimeout
        def bounded_timeout(length):
            original(length)
            self.port.setPacketTimeoutMillis(min(self.port.packet_timeout, 50.))
        self.port.setPacketTimeout = bounded_timeout

    def check(self, result, error):
        if result != 0:
            raise RuntimeError(self.packet.getTxRxResult(result))
        if error:
            raise RuntimeError(self.packet.getRxPacketError(error))

    def write(self, size, address, value):
        self.check(*getattr(self.packet, 'write%dByteTxRx' % size)(
            self.port, 1, address, value & ((1 << (8*size))-1)))

    def start(self):
        if not self.port.openPort() or not self.port.setBaudRate(57600):
            raise RuntimeError('Cannot open gripper serial port at 57600 baud')
        self.port.ser.write_timeout = .05
        self.write(1, 64, 0)
        self.write(1, 11, 4)
        self.write(2, 36, 300)
        self.write(4, 112, 80)
        # Replace any old goal before enabling torque; no startup open/close.
        self.goal(self.read())
        self.write(1, 64, 1)

    def read(self):
        value, result, error = self.packet.read4ByteTxRx(self.port, 1, 132)
        self.check(result, error)
        return value - (1 << 32) if value >= (1 << 31) else value

    def goal(self, value):
        self.write(4, 116, value)

    def close(self):
        self.port.closePort()


class DynamixelGripper:
    def __init__(self, device, command_timeout=.25, transport_factory=DynamixelTransport,
                 clock=time.perf_counter):
        self.device, self.clock = device, clock
        if not 0 < command_timeout <= 1:
            raise ValueError('invalid_gripper_command_timeout')
        self.command_timeout = command_timeout
        self.lock = threading.Lock()
        self.wake, self.stop, self.ready = threading.Event(), threading.Event(), threading.Event()
        self.intent, self.generation, self.lease = None, 0, 0.
        self.applied = -1
        self.transport_factory = transport_factory
        self.position = self.captured = self.target = None
        self.state, self.fault, self.submitted = 'initializing', None, 0
        self.active = False
        self.thread = threading.Thread(target=self._run, name='dynamixel-gripper', daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            self.stop.set()
            self.wake.set()
            raise RuntimeError('gripper_initialization_timeout')
        if self.fault:
            self.close()
            raise RuntimeError(self.fault)

    def snapshot(self):
        with self.lock:
            age = None if self.captured is None else self.clock()-self.captured
            fault = self.fault or ('gripper_feedback_stale' if age is None or not 0 <= age <= .15 else None)
            return dict(supported=True, valid=fault is None, fault=fault,
                        position_ticks=self.position, target_ticks=self.target,
                        target=self.intent, active=self.active, state=self.state,
                        age_ms=None if age is None else age*1000,
                        captured_ns=None if self.captured is None else int(self.captured*1e9),
                        submitted=self.submitted, units='encoder_ticks',
                        open_ticks=OPEN, closed_ticks=CLOSED)

    def command(self, value):
        if type(value) is not int or value not in (-1, 1):
            raise ValueError('invalid_gripper_command')
        with self.lock:
            if self.fault or self.stop.is_set():
                raise RuntimeError(self.fault or 'gripper_closed')
            changed = self.intent != value
            if changed:
                self.intent = value
                self.generation += 1
            self.lease = self.clock() + self.command_timeout
        if changed:
            self.wake.set()

    def hold(self):
        with self.lock:
            if self.intent is not None:
                self.intent = None
                self.generation += 1
                self.state = 'hold_pending'
        self.wake.set()

    def _step(self, transport):
        position = transport.read()
        now = self.clock()
        with self.lock:
            self.position, self.captured = position, now
            if self.intent is not None and now >= self.lease:
                self.intent = None
                self.generation += 1
            generation, intent = self.generation, self.intent
        if generation != self.applied:
            target = position if intent is None else OPEN if intent == -1 else CLOSED
            transport.goal(target)
            # A hold/reversal arriving during I/O remains pending. No old intent
            # is restored; the sole worker processes the new generation next.
            self.applied = generation
            self.started, self.initial = now, position
            self.history, self.stable = deque(), 0
            with self.lock:
                self.target, self.active = target, intent is not None
                self.state = 'held' if intent is None else 'opening' if intent == -1 else 'closing'
                self.submitted += int(intent is not None)
        if not self.active or generation != self.applied:
            return
        self.stable = self.stable+1 if abs(position-self.target) <= 20 else 0
        self.history.append((now, position))
        while len(self.history) > 1 and self.history[1][0] <= now-.6:
            self.history.popleft()
        values = [p for _, p in self.history]
        stalled = (now-self.started >= .8 and now-self.history[0][0] >= .6
                   and max(values)-min(values) <= 5)
        state = None
        if self.stable >= 3:
            state = 'reached'
        elif intent == 1 and self.initial > CLOSED+20 and stalled:
            transport.goal(position)
            if self.initial-position < 20:
                raise RuntimeError('gripper_stalled_without_closing_movement')
            state = 'contact_candidate'
        elif now-self.started >= 10:
            transport.goal(position)
            raise RuntimeError('gripper_motion_timeout')
        if state:
            with self.lock:
                self.active, self.state = False, state
                if state == 'contact_candidate':
                    self.target = position

    def _run(self):
        transport = None
        try:
            transport = self.transport_factory(self.device)
            transport.start()
            self._step(transport)
            self.ready.set()
            while not self.stop.is_set():
                # Fixed polling cadence: repeated 100 Hz intents do not reset
                # the move timeout, stability window or feedback sample rate.
                self.wake.wait(.02)
                self.wake.clear()
                self._step(transport)
        except Exception as exc:
            with self.lock:
                self.fault, self.state, self.active = 'gripper_fault: '+str(exc), 'fault', False
                self.intent = None
        finally:
            if transport is not None:
                try:
                    transport.goal(transport.read())
                except Exception as exc:
                    with self.lock:
                        self.fault = self.fault or 'gripper_shutdown_hold_failed: '+str(exc)
                finally:
                    try:
                        transport.close()
                    except Exception as exc:
                        with self.lock:
                            self.fault = self.fault or 'gripper_port_close_failed: '+str(exc)
            self.ready.set()

    def close(self):
        self.hold()
        self.stop.set()
        self.wake.set()
        self.thread.join(2)
        if self.thread.is_alive():
            raise RuntimeError('gripper_shutdown_timeout')
