"""cve: what a vulnerability is, how bad, and whether it is being exploited, in one call.

  cve_lookup   NVD record (severity, vector, weakness, dates), CISA "known exploited" status
               and FIRST's EPSS exploit-probability score for up to five CVE ids.
  kev_recent   What CISA added to its Known Exploited Vulnerabilities catalogue lately.

All three sources are public and need no key.

Environment (optional):
  NVD_API_KEY  An NVD key raises its limit from 5 to 50 requests per 30 seconds.
"""

import datetime
import os
import re
import time
import urllib.parse

from mcp.server.mcpserver import MCPServer

from .httpjson import FetchError, get_json

NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
EPSS_URL = "https://api.first.org/data/v1/epss"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,}$")
MAX_IDS = 5  # NVD allows 5 requests per 30 seconds without a key, one CVE per request
TTL = 3600

server = MCPServer("cve")
_nvd = {}
_kev = {"entries": [], "version": None, "fetched": 0.0}


def normalize_ids(ids):
    """Upper-cased, de-duplicated, order kept; (valid, invalid)."""
    good, bad = [], []
    for raw in ids:
        cid = str(raw).strip().upper()
        if not cid:
            continue
        target = good if CVE_ID.match(cid) else bad
        if cid not in target:
            target.append(cid)
    return good, bad


def _best_cvss(metrics):
    """The newest CVSS version present, preferring NVD's own ("Primary") assessment."""
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key) or []
        if not entries:
            continue
        entry = next((e for e in entries if e.get("type") == "Primary"), entries[0])
        data = entry.get("cvssData") or {}
        return {
            "version": data.get("version"),
            "score": data.get("baseScore"),
            "severity": data.get("baseSeverity") or entry.get("baseSeverity"),
            "vector": data.get("vectorString"),
            "assessed_by": entry.get("source"),
        }
    return None


def shape_nvd(cve):
    """One NVD CVE object -> the flat record the tool returns."""
    desc = next((d.get("value") for d in cve.get("descriptions") or [] if d.get("lang") == "en"), None)
    weaknesses = sorted(
        {
            d.get("value")
            for w in cve.get("weaknesses") or []
            for d in w.get("description") or []
            if (d.get("value") or "").startswith("CWE-")
        }
    )
    kev = None
    if cve.get("cisaExploitAdd"):
        kev = {
            "added": cve.get("cisaExploitAdd"),
            "federal_due_date": cve.get("cisaActionDue"),
            "name": cve.get("cisaVulnerabilityName"),
            "required_action": cve.get("cisaRequiredAction"),
        }
    return {
        "description": desc,
        "published": (cve.get("published") or "")[:10],
        "last_modified": (cve.get("lastModified") or "")[:10],
        "nvd_status": cve.get("vulnStatus"),
        "cvss": _best_cvss(cve.get("metrics") or {}),
        "weaknesses": weaknesses,
        "known_exploited": kev is not None,
        "kev": kev,
        "references": [r.get("url") for r in (cve.get("references") or [])[:5]],
        "url": f"https://nvd.nist.gov/vuln/detail/{cve.get('id')}",
    }


def _nvd_record(cid):
    hit = _nvd.get(cid)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    headers = {"apiKey": os.environ["NVD_API_KEY"]} if os.environ.get("NVD_API_KEY") else None
    got = get_json(f"{NVD_URL}?{urllib.parse.urlencode({'cveId': cid})}", headers=headers)
    found = got.get("vulnerabilities") or []
    record = shape_nvd(found[0]["cve"]) if found else None
    _nvd[cid] = (time.time(), record)
    return record


def _epss(ids):
    got = get_json(f"{EPSS_URL}?{urllib.parse.urlencode({'cve': ','.join(ids)})}")
    out = {}
    for row in got.get("data") or []:
        try:
            out[row["cve"]] = {
                "score": round(float(row["epss"]), 5),
                "percentile": round(float(row["percentile"]), 5),
                "date": row.get("date"),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


@server.tool()
def cve_lookup(cve_ids: list[str]) -> dict:
    """Look up CVEs: severity, what it is, and whether it is exploited in the wild.

    cve_ids: up to 5 ids like "CVE-2021-44228".

    For each id:
      cvss             base score, severity and vector, from the newest CVSS version NVD has.
                       null while NVD has not analysed the CVE yet ("nvd_status" says so).
      known_exploited  true when the CVE is in CISA's Known Exploited Vulnerabilities catalogue;
                       "kev" then has the date added and the required action.
      epss             FIRST's estimate of the probability of exploitation in the next 30 days
                       (score 0-1) and the percentile among all CVEs. null if not scored yet.
      description, published, weaknesses (CWE), references, url.
    "found": false means NVD has no record under that id (too new, reserved, or mistyped).
    A source that failed is named in that entry's "errors"; report it as not checked.
    """
    ids, invalid = normalize_ids(cve_ids)
    if not ids:
        return {"error": 'give at least one id like "CVE-2021-44228"', "invalid_ids": invalid}
    skipped = ids[MAX_IDS:]
    ids = ids[:MAX_IDS]
    try:
        epss, epss_error = _epss(ids), None
    except FetchError as e:
        epss, epss_error = {}, str(e)
    results = []
    for cid in ids:
        entry = {"id": cid, "found": False, "errors": []}
        try:
            record = _nvd_record(cid)
        except FetchError as e:
            record = None
            entry["errors"].append(f"NVD: {e}")
        if record:
            entry.update(found=True, **record)
        entry["epss"] = epss.get(cid)
        if epss_error:
            entry["errors"].append(f"EPSS: {epss_error}")
        results.append(entry)
    return {"results": results, "invalid_ids": invalid, "not_looked_up_over_limit": skipped}


def _kev_catalogue():
    if _kev["entries"] and time.time() - _kev["fetched"] < TTL:
        return _kev
    got = get_json(KEV_URL, timeout=60)
    _kev.update(entries=got.get("vulnerabilities") or [], version=got.get("catalogVersion"), fetched=time.time())
    return _kev


def recent_kev(entries, since):
    """KEV entries added on or after `since` (a date), newest first."""
    cutoff = since.isoformat()
    out = [
        {
            "id": e.get("cveID"),
            "added": e.get("dateAdded"),
            "vendor": e.get("vendorProject"),
            "product": e.get("product"),
            "name": e.get("vulnerabilityName"),
            "summary": e.get("shortDescription"),
            "ransomware_use": e.get("knownRansomwareCampaignUse"),
            "federal_due_date": e.get("dueDate"),
            "url": f"https://nvd.nist.gov/vuln/detail/{e.get('cveID')}",
        }
        for e in entries
        if (e.get("dateAdded") or "") >= cutoff
    ]
    out.sort(key=lambda r: r["added"], reverse=True)
    return out


@server.tool()
def kev_recent(days: int = 7) -> dict:
    """What CISA added to its Known Exploited Vulnerabilities catalogue in the last `days` days
    (1-90), newest first: CVE id, date added, vendor, product, a one-line summary and whether
    it is known to be used by ransomware. An empty list is a real "nothing added": the whole
    catalogue was read. Source: https://www.cisa.gov/known-exploited-vulnerabilities-catalog
    """
    days = max(1, min(int(days), 90))
    try:
        cat = _kev_catalogue()
    except FetchError as e:
        return {"error": str(e)}
    since = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=days)
    added = recent_kev(cat["entries"], since)
    return {
        "catalogue_version": cat["version"],
        "catalogue_size": len(cat["entries"]),
        "since": since.isoformat(),
        "added": added,
    }
