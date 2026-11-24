"""web-fetch is upstream code; what is checked here is that our wrapper keeps its guard on.

A page served on this machine's loopback address must never come back through the tool:
that is the same class of address as a cluster Service or a LAN host.
"""

import asyncio
import http.server
import threading
import unittest

from mcp_servers import web_fetch

MARKER = "PRIVATE-PAGE-CONTENT-7f3a"


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"<html><body><p>{MARKER}</p></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Guard(unittest.TestCase):
    def test_loopback_page_is_refused(self):
        httpd = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for host in ("127.0.0.1", "localhost"):
                url = f"http://{host}:{httpd.server_port}/"
                try:
                    result = asyncio.run(web_fetch.server.call_tool("fetch", {"url": url}))
                except Exception as e:  # a refusal raised as an error is as good as one returned
                    result = e
                self.assertNotIn(MARKER, repr(result), f"{url} was fetched")
                self.assertIn("not a public address", str(result), f"{url} was not refused by the guard")
        finally:
            httpd.shutdown()

    def test_user_agent_names_an_automated_agent(self):
        self.assertIn("agent", web_fetch.DEFAULT_USER_AGENT)


if __name__ == "__main__":
    unittest.main()
