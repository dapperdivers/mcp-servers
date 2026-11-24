# mcp-servers

Custom [MCP](https://modelcontextprotocol.io) tool servers for a homelab's AI agents. One
repository, one image (`ghcr.io/dapperdivers/mcp-servers`); the container's argument picks
which server runs.

| Server      | Tools                                | What it reads                                                                               |
| ----------- | ------------------------------------ | ------------------------------------------------------------------------------------------- |
| `cve`       | `cve_lookup`, `kev_recent`           | NVD severity, CISA known-exploited status and EPSS score for a CVE; recent KEV additions.   |
| `deals`     | `ebay_search`, `gsa_auctions_search` | Live eBay US listings (eBay Browse API) and active federal surplus lots (GSA Auctions API). |
| `weather`   | `weather_outlook`                    | US forecast and active alerts for a point (National Weather Service API).                   |
| `web-fetch` | `fetch`                              | Any public web page, returned as Markdown (the upstream `mcp-server-fetch`, unchanged).     |

Every server here is read-only.

## Running one

```bash
docker run --rm -p 8000:8000 -e MCP_AUTH_TOKEN=change-me ghcr.io/dapperdivers/mcp-servers deals
```

- MCP Streamable HTTP at `/mcp` (stateless, JSON responses). Every request must send
  `Authorization: Bearer $MCP_AUTH_TOKEN`; the server refuses to start without the variable.
- `/healthz` answers `ok` without a token, for probes.
- `PORT` changes the listen port (default 8000).
- Runs as uid 65534 with a read-only root filesystem and needs no writable path.

Each server stays its own Deployment in the cluster, with its own secret and network
policy, so one server's network access and credentials are never shared with another.

## Servers

### deals

| Variable                               | Needed for            | Without it                                                              |
| -------------------------------------- | --------------------- | ----------------------------------------------------------------------- |
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | `ebay_search`         | The tool answers "eBay is not configured".                              |
| `GSA_API_KEY`                          | `gsa_auctions_search` | The shared `DEMO_KEY` is used, which allows only a few dozen calls a day. |

- **`ebay_search`** returns Buy It Now listings located in the US, cheapest first, with
  "for parts" excluded. Shipping, total and price per TB are computed in code. Lots and
  bundles are flagged and left out of the summary. These are asking prices; eBay's sold-price
  API is not open to ordinary developers.
- **`gsa_auctions_search`** matches whole words or phrases against every active lot (name
  and, optionally, description) and returns closing date, pickup location and link. The full
  list is cached for an hour.
- Network: `api.ebay.com`, `api.gsa.gov` and the S3 address it redirects to, all on 443.

The eBay keys are a production keyset from <https://developer.ebay.com> (App ID and Cert
ID); the GSA key is free from <https://api.data.gov/signup>.

### cve

| Variable      | Needed for | Without it                                              |
| ------------- | ---------- | ------------------------------------------------------- |
| `NVD_API_KEY` | nothing    | NVD allows 5 requests per 30 seconds instead of 50.     |

- **`cve_lookup`** takes up to five CVE ids and returns, for each: the NVD record (CVSS score,
  severity and vector from the newest version present, weaknesses, dates), whether it is in
  CISA's Known Exploited Vulnerabilities catalogue, and its EPSS score. If one source fails,
  the others are still returned and the failure is named.
- **`kev_recent`** lists what CISA added to the catalogue in the last N days.
- Answers are cached for an hour.
- Network: `services.nvd.nist.gov`, `api.first.org`, `www.cisa.gov`, all on 443. No key needed.

### weather

- **`weather_outlook`** takes a latitude and longitude and returns active alerts (most severe
  first), the forecast in 12-hour periods, and a summary with each night's low and each
  day's high as numbers. One tool on purpose: a caller that asks for the forecast cannot
  skip the alerts.
- The caller passes the coordinates; nothing about a location is stored or configured here.
- US only (a point outside National Weather Service coverage returns an error).
- Network: `api.weather.gov` on 443. No key, no variables.

### web-fetch

The upstream [`mcp-server-fetch`](https://pypi.org/project/mcp-server-fetch/) package, served
by this repository's HTTP layer instead of over stdio. Fetching, `robots.txt` handling and
the refusal of private, loopback, link-local and metadata addresses (re-checked on every
redirect) are upstream's and are left on; a test here fails if a loopback page ever comes
back. HTML is extracted with the pure-Python extractor (the image has no Node.js).

| Variable           | Default                                            | Meaning                              |
| ------------------ | -------------------------------------------------- | ------------------------------------ |
| `FETCH_USER_AGENT` | `Mozilla/5.0 (compatible; homelab-research-agent)` | User-Agent sent with every request.  |

- Network: any public address on 80 and 443. Give the pod a network policy that excludes the
  cluster and LAN ranges as a second layer.

## Adding a server

1. Write `mcp_servers/<module>.py` with a module-level `server = MCPServer("<name>")` and its
   tools. Use `httpjson.get_json` for outbound calls and return `{"error": ...}` from a tool
   instead of raising, so the agent can report the source as unchecked.
2. Add the name, module and a one-line description to `SERVERS` in `mcp_servers/__main__.py`.
3. Add its tool names to `EXPECTED_TOOLS` in `tests/test_serving.py` and write
   `tests/test_<name>.py`. Tests never call a real API.
4. Add a row to the table above and a section for its variables.

## Developing

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
MCP_AUTH_TOKEN=dev .venv/bin/python -m mcp_servers deals
```

CI builds the image and runs the same tests inside it; a push to `main` publishes the image
and prints the tag and digest to pin in the job summary.
