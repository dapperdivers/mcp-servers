"""Entrypoint: `python -m mcp_servers <name>` serves one server over Streamable HTTP.

To add a server: write mcp_servers/<module>.py with a module-level `server` (an MCPServer
with its tools) and add it to SERVERS.
"""

import importlib
import sys

from . import serving

# name -> (module in this package, one-line description shown by --help)
SERVERS = {
    "cve": ("cve", "CVE lookup: NVD severity, CISA known-exploited status, EPSS score."),
    "deals": ("deals", "Hardware prices: live eBay US listings and active GSA Auctions surplus lots."),
    "weather": ("weather", "US forecast, alerts and tonight's sky for a point (NWS, Skyfield)."),
    "web-fetch": ("web_fetch", "Read a public web page as Markdown (upstream mcp-server-fetch)."),
}


def load(name):
    """The MCPServer for a name in SERVERS. Imported on demand: a server's packages and
    environment are only touched when that server is the one being run."""
    return importlib.import_module(f"{__package__}.{SERVERS[name][0]}").server


def usage():
    lines = ["usage: python -m mcp_servers <server>", "", "servers:"]
    lines += [f"  {name:<10} {text}" for name, (_, text) in sorted(SERVERS.items())]
    return "\n".join(lines)


def main(argv):
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(usage())
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    name = argv[0]
    if name not in SERVERS:
        print(f"unknown server {name!r}\n\n{usage()}", file=sys.stderr)
        return 2
    serving.serve(load(name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
