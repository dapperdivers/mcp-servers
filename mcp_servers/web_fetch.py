"""web-fetch: read a public web page as Markdown.

This is the upstream `mcp-server-fetch` package, unchanged, served by this repository's
HTTP layer instead of over stdio. All fetching, robots.txt handling and the refusal of
private, loopback, link-local and metadata addresses (re-checked on every redirect) are
upstream's; nothing here loosens them. A network policy on the pod should be the second layer.

HTML is extracted with the pure-Python extractor: the image has no Node.js.

Environment (optional):
  FETCH_USER_AGENT  User-Agent sent with every request. The default names the client as an
                    automated agent, so sites that refuse bots can do so.
"""

import os

from mcp_server_fetch.server import create_server

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; homelab-research-agent)"

server = create_server(
    custom_user_agent=os.environ.get("FETCH_USER_AGENT") or DEFAULT_USER_AGENT,
    use_readability_js=False,
)
