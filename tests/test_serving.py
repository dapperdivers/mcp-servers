"""Every server, started for real on a local port: health check, the token check, tools/list."""

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

import uvicorn

from mcp_servers import __main__ as entry
from mcp_servers import serving

TOKEN = "test-token"
EXPECTED_TOOLS = {
    "cve": {"cve_lookup", "kev_recent"},
    "deals": {"ebay_search", "gsa_auctions_search"},
    "weather": {"weather_outlook"},
    "web-fetch": {"fetch"},
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    """One server on 127.0.0.1 in a background thread."""

    def __init__(self, name):
        self.port = free_port()
        config = uvicorn.Config(serving.build_app(entry.load(name), TOKEN), host="127.0.0.1", port=self.port, log_level="error")
        self.uvicorn = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.uvicorn.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.uvicorn.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.uvicorn.should_exit = True
        self.thread.join(timeout=10)

    def request(self, path, body=None, token=None, host=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if host:
            headers["Host"] = host
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class Serving(unittest.TestCase):
    def test_every_listed_server_has_expected_tools(self):
        self.assertEqual(set(entry.SERVERS), set(EXPECTED_TOOLS), "add the new server's tools to EXPECTED_TOOLS")

    def test_serves_tools_behind_the_token(self):
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        for name, tools in EXPECTED_TOOLS.items():
            with self.subTest(server=name), Running(name) as srv:
                self.assertEqual(srv.request("/healthz"), (200, b"ok\n"))
                self.assertEqual(srv.request("/mcp", listing)[0], 401)
                self.assertEqual(srv.request("/mcp", listing, token="wrong")[0], 401)
                # the cluster's egress gateway sends the Service name as Host, with or without the port
                for host in (None, f"{name}.kagent.svc.cluster.local", f"{name}.kagent.svc.cluster.local:8000"):
                    status, body = srv.request("/mcp", listing, token=TOKEN, host=host)
                    self.assertEqual(status, 200, body)
                    self.assertEqual({t["name"] for t in json.loads(body)["result"]["tools"]}, tools)

    def test_tool_call_round_trip(self):
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "gsa_auctions_search", "arguments": {"keywords": [" "]}}}
        with Running("deals") as srv:
            status, body = srv.request("/mcp", call, token=TOKEN)
        self.assertEqual(status, 200, body)
        self.assertIn("at least one keyword", json.loads(body)["result"]["content"][0]["text"])


class Entrypoint(unittest.TestCase):
    def test_unknown_server_and_help(self):
        self.assertEqual(entry.main(["nope"]), 2)
        self.assertEqual(entry.main([]), 2)
        self.assertEqual(entry.main(["--help"]), 0)
        self.assertIn("deals", entry.usage())

    def test_refuses_to_serve_without_a_token(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"MCP_AUTH_TOKEN": ""}), self.assertRaises(SystemExit) as stop:
            serving.serve(object())
        self.assertIn("MCP_AUTH_TOKEN", str(stop.exception))


if __name__ == "__main__":
    unittest.main()
