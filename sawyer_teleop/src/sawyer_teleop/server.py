"""B-side two-port service. No dataset or video dependencies."""
import socket
import sys
import collections
import threading
import time

from .bridge import BridgeCore
from .joint_protection import check_jump, JointJumpError
from .protocol import VERSION, ProtocolError, close_socket, configure_socket, encode, ns, pose, vector, integer
from .transport import Connection, read_one

B_SEND_TIMEOUT_S = 0.2


class BridgeServer:
    def __init__(self, backend, host="127.0.0.1", control_port=8765, ik_port=8766,
                 state_timeout=.1, command_timeout=.2, hz=100, reference_timeout=.12):
        if not 1 <= hz <= 500:
            raise ValueError("invalid bridge frequency")
        self.core = BridgeCore(backend, state_timeout, command_timeout, control_hz=hz,
                               reference_timeout=reference_timeout)
        self.host, self.control_port, self.ik_port = host, control_port, ik_port
        self.hz = hz
        self.stop = threading.Event()
        self.owner_lock = threading.Lock()
        self.owner = None
        self.ik_busy = threading.Lock()
        self.ik_slots = threading.BoundedSemaphore(2)
        self.ik_sockets = set()
        self.sockets_lock = threading.Lock()
        self.listeners = []
        self.threads = []
        self.error = None

    def _listen(self, port):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, port))
        sock.listen(4)
        sock.settimeout(.2)
        self.listeners.append(sock)
        return sock

    def start(self):
        try:
            control = self._listen(self.control_port)
            ik = self._listen(self.ik_port)
        except Exception:
            self.close()
            raise
        self.control_port, self.ik_port = control.getsockname()[1], ik.getsockname()[1]
        for target, args in ((self._accept_control, (control,)),
                             (self._accept_ik, (ik,)), (self._tick, ())):
            t = threading.Thread(target=target, args=args, daemon=True)
            self.threads.append(t)
            t.start()
        return self

    def _accept_control(self, listener):
        while not self.stop.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            configure_socket(sock)
            if not self.owner_lock.acquire(False):
                try:
                    sock.sendall(encode({"type": "error", "error": "bridge_busy"}))
                except OSError:
                    pass
                close_socket(sock)
                continue
            threading.Thread(target=self._control, args=(sock,), daemon=True).start()

    def _control(self, sock):
        session = None
        conn = None
        try:
            hello = read_one(sock)
            if hello != {"type": "hello", "version": VERSION}:
                raise ProtocolError("protocol_version_mismatch")
            response = self.core.connect()
            session = response["session"]
            sock.sendall(encode(response))
            replies = collections.OrderedDict()
            oldest_id = -1

            def receive(msg):
                nonlocal oldest_id
                try:
                    ident = None
                    if msg.get("type") not in ("target", "pose_target", "gripper_observation"):
                        ident = integer(msg.get("id"), "request id")
                        if ident in replies:
                            if replies[ident][0] != msg:
                                raise ProtocolError("request_id_reused")
                            conn.send(replies[ident][1])
                            return
                        if ident <= oldest_id:
                            raise ProtocolError("request_expired")
                    result = self.core.request(msg)
                    if result is not None:
                        reply = {"type": "reply", "id": msg.get("id"), "ok": True, "result": result}
                        if ident is not None:
                            replies[ident] = (msg, reply)
                            while len(replies) > 128:
                                removed, _ = replies.popitem(last=False)
                                oldest_id = max(oldest_id, removed)
                        conn.send(reply)
                except (ProtocolError, KeyError, TypeError, ValueError) as exc:
                    conn.send({"type": "reply", "id": msg.get("id"),
                               "ok": False, "error": str(exc)})

            def closed():
                print("A connection closed: " + str(conn.error or "unknown"), file=sys.stderr, flush=True)
                self.core.disconnect(session, conn.error or "unknown")

            # 2026-11-10: B->A send deadline 0.2 s (was 0.1) so a brief Wi-Fi stall does not drop A.
            conn = Connection(sock, receive, on_close=closed, timeout=B_SEND_TIMEOUT_S)
            self.owner = conn
            conn.start()
            while not self.stop.wait(.1) and not conn.closed.is_set():
                pass
        except Exception:
            close_socket(sock)
        finally:
            if conn:
                conn.close()
            if session:
                self.core.disconnect(session)
            self.owner = None
            self.owner_lock.release()

    def _tick(self):
        deadline = time.monotonic()
        while not self.stop.is_set():
            try:
                receipt, state = self.core.tick()
                drain = getattr(self.core, "drain_feedback_stream", None)
                stream = drain() if drain else None
                if stream:
                    # Recording-only side channel; merged forward if a state frame is replaced unsent.
                    state = dict(state, feedback_stream=stream)
                conn = self.owner
                if conn and not conn.closed.is_set():
                    if receipt:
                        conn.send(receipt)
                    conn.send(state, latest="state", merge="feedback_stream")
            except ConnectionError:
                pass
            except Exception as exc:
                self.error = str(exc)
                if self.owner:
                    self.owner.close("backend_failure")
            deadline += 1/self.hz
            now = time.monotonic()
            if deadline < now:
                deadline = now
            self.stop.wait(max(0, deadline-now))

    def _accept_ik(self, listener):
        while not self.stop.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            configure_socket(sock)
            if not self.ik_slots.acquire(False):
                close_socket(sock)
                continue
            with self.sockets_lock:
                self.ik_sockets.add(sock)
            threading.Thread(target=self._ik, args=(sock,), daemon=True).start()

    def _ik(self, sock):
        try:
            hello = read_one(sock)
            session = hello.get("session")
            if (hello.get("type") != "hello_ik" or hello.get("version") != VERSION
                    or not session or session != self.core.session):
                raise ProtocolError("ik_session_mismatch")
            sock.sendall(encode({"type": "hello_ik", "version": VERSION}))
            while not self.stop.is_set():
                try:
                    msg = read_one(sock, timeout=1)
                except TimeoutError:
                    continue
                result = {"type": "ik_result", "id": msg.get("id"), "ok": False}
                acquired = False
                try:
                    if msg.get("type") != "solve" or msg.get("session") != session:
                        raise ProtocolError("invalid_ik_request")
                    target, seed = pose(msg.get("pose")), vector(msg.get("seed"), 7)
                    budget = msg.get("timeout_s")
                    if type(budget) not in (int, float) or not 0 < budget <= .2:
                        raise ProtocolError("invalid_ik_timeout")
                    started = ns()
                    with self.core.lock:
                        if session != self.core.session:
                            raise ProtocolError("session_expired")
                        if not self.core.armed or msg.get("generation") != self.core.generation:
                            raise ProtocolError("generation_expired")
                        self.core._refresh()
                        self.core._reference(msg.get("feedback_seq"))
                    acquired = self.ik_busy.acquire(False)
                    if not acquired:
                        raise ProtocolError("ik_busy")
                    q = self.core.backend.solve(target, seed)
                    elapsed = (ns()-started)/1e9
                    with self.core.lock:
                        if (elapsed > budget or session != self.core.session
                                or not self.core.armed
                                or msg["generation"] != self.core.generation):
                            raise ProtocolError("ik_expired")
                        if q is not None:
                            try:
                                check_jump(q, seed, self.core.joint_protection, "B_IK")
                            except JointJumpError as exc:
                                self.core._hold("joint_jump", details=exc.details)
                                raise
                    if q is None:
                        raise ProtocolError("ik_no_solution")
                    result.update(ok=True, q=vector(q, 7), elapsed_s=elapsed)
                except Exception as exc:
                    result["error"] = str(exc)
                finally:
                    if acquired:
                        self.ik_busy.release()
                sock.sendall(encode(result))
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            close_socket(sock)
            with self.sockets_lock:
                self.ik_sockets.discard(sock)
            self.ik_slots.release()

    def close(self):
        self.stop.set()
        if self.owner:
            self.owner.close("server_shutdown")
        for sock in self.listeners:
            close_socket(sock)
        with self.sockets_lock:
            for sock in list(self.ik_sockets):
                close_socket(sock)
        for t in self.threads:
            if t is not threading.current_thread():
                t.join(timeout=1)
        self.core.backend.close()
