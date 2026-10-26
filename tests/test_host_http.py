"""HTTP group orchestration and discovery with real local network endpoints."""

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bcamsync_host import HttpGroupClient, HttpNodeTarget, discover, interface_addresses
from bcamsync_host.http import DISCOVERY_ADDRESS, DISCOVERY_PROTOCOL


CONFIG = {"group_id": "lab", "reference_unit": "board-a", "nodes": [
    {"id": "node-a"}, {"id": "node-b"}], "units": [
    {"id": "board-a", "node_id": "node-a"}, {"id": "board-b", "node_id": "node-b"}]}


class HostCliTests(unittest.TestCase):
    def setUp(self):
        example = Path(__file__).resolve().parents[1] / "tools" / "manage_group.py"
        spec = importlib.util.spec_from_file_location("manage_group_example", example)
        self.example = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.example)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_path = Path(self.directory.name) / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))

    def test_discover_prints_preview_origins_without_requiring_all_nodes(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["manage_group.py", str(self.config_path), "discover", "--interface", "en7"]), \
                patch.object(self.example, "discover", return_value={"node-a": HttpNodeTarget("http://169.254.4.5:58083")}) as query, \
                patch.object(self.example, "HttpGroupClient") as client, redirect_stdout(output):
            self.example.main()
        self.assertEqual(json.loads(output.getvalue()), {"node-a": {"base_url": "http://169.254.4.5:58083"}})
        query.assert_called_once_with("lab", 2, "0.0.0.0", 58082, interface="en7")
        client.assert_not_called()

    def test_discover_rejects_ssh_transport(self):
        with patch.object(sys, "argv", ["manage_group.py", str(self.config_path), "discover", "--ssh-targets", "targets.json"]), \
                patch.object(self.example, "discover") as query, redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as error:
            self.example.main()
        self.assertEqual(error.exception.code, 2)
        query.assert_not_called()


class HttpHostTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.behavior = {}
        self.active = {"node-a": False, "node-b": False}
        self.servers = []
        targets = {}
        test = self
        for node in ("node-a", "node-b"):
            class Handler(BaseHTTPRequestHandler):
                def handle_request(self):
                    command = self.path.lstrip("/")
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"]))) if self.command == "POST" else None
                    node_id = self.server.node_id
                    test.calls.append((node_id, self.command, command, payload))
                    status, envelope = test.response(node_id, command)
                    encoded = json.dumps(envelope).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    try:
                        self.wfile.write(encoded)
                    except BrokenPipeError:
                        pass
                do_GET = handle_request
                do_POST = handle_request
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.node_id = node
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            self.servers.append((server, thread))
            targets[node] = HttpNodeTarget(f"http://127.0.0.1:{server.server_port}")
        self.client = HttpGroupClient(CONFIG, targets, timeout_seconds=0.2)

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join()

    def response(self, node, command):
        if (node, command) in self.behavior:
            return self.behavior[node, command]()
        result = {"node_id": node}
        if command == "configuration":
            result.update(sync_contract=deepcopy(self.client.sync_contract), reference_node="node-a")
        else:
            if command == "start":
                self.active[node] = True
            elif command == "stop":
                self.active[node] = False
            result["active"] = self.active[node]
        return 200, {"ok": True, "result": result}

    def test_http_start_followers_first_and_stop_reference_first(self):
        result = self.client.start()
        starts = [call for call in self.calls if call[2] == "start"]
        self.assertEqual([call[0] for call in starts], ["node-b", "node-a"])
        self.assertTrue(all(call[1] == "POST" and call[3] == {"operation_id": result["operation_id"]} for call in starts))
        self.assertTrue(self.client.status()["group"]["active"])
        self.client.stop()
        stops = [call for call in self.calls if call[2] == "stop"]
        self.assertEqual([call[0] for call in stops], ["node-a", "node-b"])
        self.assertTrue(all(call[3] == {} for call in stops))

    def test_http_node_error_keeps_detail_and_cleans_same_operation(self):
        self.behavior["node-a", "start"] = lambda: (409, {"ok": False, "error": "camera busy"})
        with self.assertRaisesRegex(RuntimeError, "node-a start failed: camera busy"):
            self.client.start()
        starts = [call for call in self.calls if call[2] == "start"]
        stops = [call for call in self.calls if call[2] == "stop"]
        self.assertEqual([call[0] for call in stops], ["node-a", "node-b"])
        self.assertTrue(all(call[3] == starts[0][3] for call in stops))

    def test_contract_mismatch_prevents_start(self):
        contract = deepcopy(self.client.sync_contract)
        contract["fps"] = 41
        self.behavior["node-b", "configuration"] = lambda: (200, {"ok": True, "result": {
            "node_id": "node-b", "reference_node": "node-a", "sync_contract": contract}})
        with self.assertRaisesRegex(RuntimeError, "contract differs"):
            self.client.start()
        self.assertFalse(any(call[2] == "start" for call in self.calls))

    def test_wrong_node_identity_is_rejected(self):
        self.behavior["node-a", "status"] = lambda: (200, {"ok": True, "result": {"node_id": "node-b"}})
        with self.assertRaisesRegex(RuntimeError, "node identity"):
            self.client.status()

    def test_http_timeout_is_bounded(self):
        def delayed():
            time.sleep(0.4)
            return 200, {"ok": True, "result": {"node_id": "node-a"}}
        self.behavior["node-a", "status"] = delayed
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.client.status()
        self.assertLess(time.monotonic() - started, 0.35)

    def test_missing_discovered_node_names_are_reported(self):
        with self.assertRaisesRegex(ValueError, r"missing=\['node-b'\]"):
            HttpGroupClient(CONFIG, {"node-a": self.client.targets["node-a"]})

    def test_target_rejects_paths_credentials_and_invalid_ports(self):
        for url in ("https://127.0.0.1", "http://user@127.0.0.1", "http://127.0.0.1/status", "http://127.0.0.1:0"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                HttpNodeTarget(url)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.channel.bind(("0.0.0.0", 0))
        self.port = self.channel.getsockname()[1]
        membership = socket.inet_aton(DISCOVERY_ADDRESS) + socket.inet_aton("127.0.0.1")
        self.channel.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        self.channel.settimeout(0.05)
        self.responses = []
        self.queries = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.respond, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stopped.set()
        self.thread.join()
        self.channel.close()

    def respond(self):
        while not self.stopped.is_set():
            try:
                data, address = self.channel.recvfrom(65535)
            except socket.timeout:
                continue
            self.queries.append(json.loads(data))
            for response in self.responses:
                self.channel.sendto(json.dumps(response).encode(), address)

    def announcement(self, node="node-a", port=8080, group="lab"):
        return {"protocol": DISCOVERY_PROTOCOL, "type": "announcement", "group_id": group,
                "node_id": node, "http_port": port, "url": "http://untrusted.example"}

    def test_discovers_source_address_and_ignores_other_groups_and_bad_ports(self):
        self.responses = [self.announcement(), self.announcement("node-b", group="other"),
                          self.announcement("node-c", port=True), self.announcement("node-d", port=0)]
        found = discover("lab", 0.15, "127.0.0.1", self.port)
        self.assertEqual(found, {"node-a": HttpNodeTarget("http://127.0.0.1:8080")})
        self.assertEqual(self.queries[0], {"protocol": DISCOVERY_PROTOCOL, "type": "query", "group_id": "lab"})

    def test_conflicting_endpoint_for_same_node_is_reported(self):
        self.responses = [self.announcement(), self.announcement(port=8081)]
        with self.assertRaisesRegex(RuntimeError, "Multiple management endpoints"):
            discover("lab", 0.15, "127.0.0.1", self.port)

    def test_timeout_returns_empty_membership(self):
        started = time.monotonic()
        self.assertEqual(discover("lab", 0.08, "127.0.0.1", self.port), {})
        self.assertLess(time.monotonic() - started, 0.25)

    def test_automatic_interfaces_do_not_require_node_addresses(self):
        self.responses = [self.announcement()]
        with patch("bcamsync_host.http.interface_addresses", return_value=["127.0.0.1"]) as addresses:
            found = discover("lab", 0.08, port=self.port, interface="en7")
        addresses.assert_called_once_with("en7")
        self.assertIn("node-a", found)

    def test_interface_enumeration_reads_mac_and_linux_without_mutation(self):
        mac = "lo0: flags=8049<UP,LOOPBACK,RUNNING>\n\tinet 127.0.0.1 netmask 0xff000000\nen7: flags=8863<UP,BROADCAST,RUNNING>\n\tinet 169.254.4.5 netmask 0xffff0000\nen9: flags=8822<BROADCAST,RUNNING>\n\tinet 10.0.0.1 netmask 0xffffff00\n"
        with patch("bcamsync_host.http.sys.platform", "darwin"), patch("bcamsync_host.http.subprocess.run") as run:
            run.return_value.stdout = mac
            self.assertEqual(interface_addresses("en7"), ["169.254.4.5"])
            self.assertEqual(run.call_args.args[0], ["/sbin/ifconfig", "en7"])
        linux = [{"addr_info": [{"family": "inet", "local": "169.254.4.6"}]}]
        with patch("bcamsync_host.http.sys.platform", "linux"), patch("bcamsync_host.http.subprocess.run") as run:
            run.return_value.stdout = json.dumps(linux)
            self.assertEqual(interface_addresses("eth0"), ["169.254.4.6"])
            self.assertEqual(run.call_args.args[0], ["ip", "-j", "-4", "addr", "show", "up", "dev", "eth0"])


if __name__ == "__main__":
    unittest.main()
