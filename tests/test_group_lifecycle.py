"""Public SDK-free group startup, group shutdown, and capture ownership checks."""

from contextlib import ExitStack
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from bcamsync import NodeConfig, SyncConfig, SyncSession, SyncUnitConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from synthetic_capture import SyntheticCapture


def await_condition(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Group lifecycle condition did not occur before its deadline")
        time.sleep(0.01)


def group_config(count, join_timeout=None):
    config = SyncConfig()
    config.group_id = "group-lifecycle"
    config.reference_unit = "unit-0"
    config.network_mode = "existing"
    config.clock_mode = "host"
    config.startup_timeout_seconds = 1.0
    config.request_timeout_seconds = 0.3
    config.join_timeout_seconds = join_timeout
    policy = config.phase_policy
    policy.window_seconds = 0.1
    policy.evaluation_seconds = 0.05
    policy.freshness_seconds = 0.5
    policy.ready_windows = 1
    config.phase_policy = policy
    nodes, units = [], []
    # Reserve all endpoints together so one fixture cannot select the same port twice.
    with ExitStack() as sockets:
        for index in range(count):
            endpoint = sockets.enter_context(socket.socket())
            endpoint.bind(("127.0.0.1", 0))
            node = NodeConfig()
            node.id, node.address, node.port = f"node-{index}", "127.0.0.1", endpoint.getsockname()[1]
            unit = SyncUnitConfig()
            unit.id, unit.node_id = f"unit-{index}", node.id
            nodes.append(node)
            units.append(unit)
    config.nodes, config.units = nodes, units
    config.validate()
    return config


class Group:
    def __init__(self, count, local_nodes=None, join_timeout=None, clock_mode="host"):
        self.config = group_config(count, join_timeout)
        self.config.clock_mode = clock_mode
        if clock_mode == "network":
            self.config.startup_timeout_seconds = 15
            # Exercise real chronyd lifecycle, not sub-millisecond VM clock accuracy.
            # Match the native clock tolerance and allow it through phase validation.
            self.config.clock_max_offset_ms = 10
            policy = self.config.phase_policy
            policy.max_mapping_span_ns = 10_000_000
            self.config.phase_policy = policy
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as endpoint:
                endpoint.bind(("127.0.0.1", 0))
                self.config.clock_port = endpoint.getsockname()[1]
        self.captures, self.sessions = {}, {}
        for index in range(count) if local_nodes is None else local_nodes:
            capture = SyntheticCapture(f"unit-{index}", phase_ns=0)
            self.captures[index] = capture
            self.sessions[index] = SyncSession(self.config, f"node-{index}", {capture.unit_id: capture})

    def __enter__(self):
        for capture in self.captures.values():
            capture.start_capture()
        return self

    def __exit__(self, kind, value, traceback):
        try:
            for session in self.sessions.values():
                session.stop()
        finally:
            for capture in self.captures.values():
                capture.stop_capture()

    def start(self, *nodes):
        for node in nodes:
            self.sessions[node].start()

    def ready(self, timeout=5):
        for session in self.sessions.values():
            if not session.wait_ready(timeout):
                raise AssertionError("Configured group did not become ready")

    def stopped(self):
        await_condition(lambda: all(s.status().state == "stopped" for s in self.sessions.values()))

    def capture_continues(self):
        before = {index: capture.sequence for index, capture in self.captures.items()}
        await_condition(lambda: all(c.sequence >= before[index] + 3
                                    for index, c in self.captures.items()), timeout=1)
        for capture in self.captures.values():
            if not capture.thread.is_alive() or capture.fault():
                raise AssertionError("Stopping synchronization affected application capture")


def stop_concurrently(sessions):
    errors = []

    def stop(session):
        try:
            session.stop()
        except Exception as error:
            errors.append(error)

    workers = [threading.Thread(target=stop, args=(session,), daemon=True) for session in sessions]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + 5
    for worker in workers:
        worker.join(max(0, deadline - time.monotonic()))
    if any(worker.is_alive() for worker in workers):
        raise AssertionError("Concurrent group stops did not finish before their deadline")
    if errors:
        raise errors[0]


class GroupLifecycleTests(unittest.TestCase):
    def test_late_members_join_in_either_startup_order(self):
        for first, second in ((0, 1), (1, 0)):
            with self.subTest(first=first), Group(2) as group:
                group.start(first)
                # Membership waiting must outlive the separate one-second startup budget.
                time.sleep(1.25)
                waiting = group.sessions[first].status()
                self.assertEqual(waiting.fault, "")
                self.assertFalse(waiting.ready)
                self.assertIn(f"node-{second}", waiting.waiting_nodes)
                group.start(second)
                group.ready()
                group.sessions[second].stop()
                group.stopped()
                group.capture_continues()

    def test_follower_stops_three_node_group_and_remote_sessions_restart_directly(self):
        with Group(3) as group:
            group.start(0, 1, 2)
            group.ready()
            group.sessions[1].stop()
            group.stopped()
            for session in group.sessions.values():
                status = session.status()
                self.assertEqual(status.fault, "")
                self.assertEqual((status.stop_node, status.stop_reason), ("node-1", "requested"))
                self.assertFalse(status.ready)
            group.capture_continues()
            # Nodes 0 and 2 have only exited remotely; no explicit local stop cleans them up.
            group.start(2, 0, 1)
            group.ready()
            group.sessions[2].stop()
            group.stopped()
            group.capture_continues()

    def test_joined_follower_can_stop_group_waiting_for_third_member(self):
        with Group(3, local_nodes=(0, 1)) as group:
            group.start(0, 1)
            await_condition(lambda: group.sessions[0].status().waiting_nodes == ["node-2"])
            group.sessions[1].stop()
            group.stopped()
            for session in group.sessions.values():
                status = session.status()
                self.assertEqual(status.fault, "")
                self.assertEqual((status.stop_node, status.stop_reason), ("node-1", "requested"))
            group.capture_continues()

    def test_simultaneous_stops_are_bounded_and_clean(self):
        with Group(3) as group:
            group.start(0, 1, 2)
            group.ready()
            stop_concurrently(group.sessions.values())
            group.stopped()
            for session in group.sessions.values():
                self.assertEqual(session.status().fault, "")
                self.assertEqual(session.status().stop_reason, "requested")
            group.capture_continues()

    @unittest.skipUnless(sys.platform == "linux", "Private chronyd preparation requires Linux")
    def test_private_clock_preparation_and_follower_stop_preserve_capture(self):
        with Group(2, clock_mode="network") as group:
            group.start(1)
            await_condition(lambda: group.sessions[1].status().state == "waiting")
            self.assertFalse(group.sessions[1].clock().valid)
            group.start(0)
            group.ready(timeout=20)
            self.assertTrue(all(session.clock().valid for session in group.sessions.values()))
            group.sessions[1].stop()
            group.stopped()
            self.assertTrue(all(not session.clock().valid for session in group.sessions.values()))
            group.capture_continues()

    def test_configured_join_deadline_faults_without_stopping_capture(self):
        with Group(2, local_nodes=(0,), join_timeout=0.25) as group:
            group.start(0)
            with self.assertRaisesRegex(RuntimeError, "join|membership"):
                group.sessions[0].wait_ready(3)
            self.assertFalse(group.sessions[0].status().ready)
            self.assertEqual(group.sessions[0].status().stop_reason, "fault")
            group.capture_continues()

    def test_abrupt_peer_exit_faults_surviving_session_and_preserves_capture(self):
        with Group(2, local_nodes=(0,)) as group, tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "group.json"
            path.write_text(group.config.to_json())
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                        "--peer", str(path)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                group.start(0)
                group.ready()
                readable, _, _ = select.select([process.stdout], [], [], 5)
                self.assertTrue(readable, "Child peer did not report readiness")
                self.assertEqual(process.stdout.readline().strip(), "READY")
                process.kill()
                process.wait(timeout=3)
                await_condition(lambda: bool(group.sessions[0].status().fault))
                status = group.sessions[0].status()
                self.assertFalse(status.ready)
                self.assertEqual((status.stop_node, status.stop_reason), ("node-1", "fault"))
                group.capture_continues()
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)

    def test_shutdown_warning_is_emitted_once_per_participating_node(self):
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--warnings"],
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        warnings = [line for line in process.stderr.splitlines()
                    if line.startswith("WARNING: Synchronization stopped by")]
        self.assertEqual(len(warnings), 3, process.stderr)
        self.assertTrue(all("Acquisition continues" in line for line in warnings), process.stderr)


def peer_main(path):
    config = SyncConfig.from_file(path)
    capture = SyntheticCapture("unit-1", phase_ns=0)
    capture.start_capture()
    try:
        with SyncSession(config, "node-1", {capture.unit_id: capture}) as session:
            if not session.wait_ready(5):
                raise AssertionError("Child peer did not become ready")
            print("READY", flush=True)
            threading.Event().wait()
    finally:
        capture.stop_capture()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--peer":
        peer_main(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "--warnings":
        with Group(3) as group:
            group.start(0, 1, 2)
            group.ready()
            group.sessions[1].stop()
            group.stopped()
            group.capture_continues()
    else:
        unittest.main()
