"""A small JSON-over-HTTPS client on the standard library, shared by the servers."""

import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "dapperdivers-mcp-servers/1.0 (personal homelab tools)"


class FetchError(RuntimeError):
    """A request failed. The message is safe to hand back to the calling agent."""


def get_json(url, *, data=None, headers=None, timeout=45):
    """GET (or POST when `data` is given) and parse the JSON body. Raises FetchError."""
    host = urllib.parse.urlsplit(url).netloc
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read(300).decode("utf-8", "replace")
        raise FetchError(f"HTTP {e.code} from {host}: {body}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise FetchError(f"request to {host} failed: {e}") from None
