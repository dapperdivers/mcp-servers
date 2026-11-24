import datetime
import unittest
from unittest import mock

from mcp_servers import cve
from mcp_servers.httpjson import FetchError

NVD = {"vulnerabilities": [{"cve": {
    "id": "CVE-2021-44228", "published": "2021-12-10T10:15:09.143", "lastModified": "2026-02-01T00:00:00.000",
    "vulnStatus": "Analyzed",
    "descriptions": [{"lang": "es", "value": "no"}, {"lang": "en", "value": "Log4j JNDI lookup RCE."}],
    "metrics": {
        "cvssMetricV2": [{"type": "Primary", "source": "nvd@nist.gov", "baseSeverity": "HIGH",
                          "cvssData": {"version": "2.0", "baseScore": 9.3, "vectorString": "AV:N"}}],
        "cvssMetricV31": [
            {"type": "Secondary", "source": "vendor", "cvssData": {"version": "3.1", "baseScore": 9.0, "baseSeverity": "CRITICAL", "vectorString": "V"}},
            {"type": "Primary", "source": "nvd@nist.gov", "cvssData": {"version": "3.1", "baseScore": 10.0, "baseSeverity": "CRITICAL", "vectorString": "CVSS:3.1/AV:N"}},
        ],
    },
    "weaknesses": [{"description": [{"value": "CWE-502"}, {"value": "NVD-CWE-noinfo"}]}, {"description": [{"value": "CWE-20"}]}],
    "cisaExploitAdd": "2021-12-10", "cisaActionDue": "2021-12-24", "cisaVulnerabilityName": "Log4Shell",
    "cisaRequiredAction": "Apply updates.",
    "references": [{"url": f"https://example.org/{i}"} for i in range(9)],
}}]}
EPSS = {"data": [{"cve": "CVE-2021-44228", "epss": "0.999990000", "percentile": "1.000000000", "date": "2026-11-24"}]}


def fake(nvd=NVD, epss=EPSS):
    def get(url, **kw):
        if url.startswith(cve.EPSS_URL):
            return epss
        return nvd
    return get


class Lookup(unittest.TestCase):
    def setUp(self):
        cve._nvd.clear()

    def test_ids(self):
        self.assertEqual(cve.normalize_ids([" cve-2021-44228 ", "CVE-2021-44228", "log4shell", "", "CVE-21-1"]),
                         (["CVE-2021-44228"], ["LOG4SHELL", "CVE-21-1"]))

    def test_record(self):
        with mock.patch.object(cve, "get_json", fake()):
            r = cve.cve_lookup(["cve-2021-44228"])
        e = r["results"][0]
        self.assertTrue(e["found"])
        self.assertEqual(e["cvss"]["score"], 10.0)  # newest version, NVD's own assessment
        self.assertEqual(e["cvss"]["severity"], "CRITICAL")
        self.assertEqual(e["description"], "Log4j JNDI lookup RCE.")
        self.assertEqual(e["weaknesses"], ["CWE-20", "CWE-502"])
        self.assertTrue(e["known_exploited"])
        self.assertEqual(e["kev"]["added"], "2021-12-10")
        self.assertEqual(e["epss"], {"score": 0.99999, "percentile": 1.0, "date": "2026-11-24"})
        self.assertEqual(len(e["references"]), 5)
        self.assertEqual(e["url"], "https://nvd.nist.gov/vuln/detail/CVE-2021-44228")
        self.assertEqual(e["errors"], [])

    def test_not_in_kev_not_analysed_not_found(self):
        bare = {"vulnerabilities": [{"cve": {"id": "CVE-2026-11111", "vulnStatus": "Awaiting Analysis", "metrics": {}}}]}
        with mock.patch.object(cve, "get_json", fake(nvd=bare, epss={"data": []})):
            e = cve.cve_lookup(["CVE-2026-11111"])["results"][0]
        self.assertTrue(e["found"])
        self.assertIsNone(e["cvss"])
        self.assertFalse(e["known_exploited"])
        self.assertIsNone(e["epss"])
        cve._nvd.clear()
        with mock.patch.object(cve, "get_json", fake(nvd={"vulnerabilities": []}, epss={"data": []})):
            self.assertFalse(cve.cve_lookup(["CVE-2099-00001"])["results"][0]["found"])

    def test_limit_cache_and_bad_input(self):
        ids = [f"CVE-2026-1000{i}" for i in range(7)]
        with mock.patch.object(cve, "get_json", side_effect=fake(nvd={"vulnerabilities": []}, epss={"data": []})) as get:
            r = cve.cve_lookup(ids)
            cve.cve_lookup(ids[:2])  # NVD answers come from the cache
        self.assertEqual(len(r["results"]), 5)
        self.assertEqual(r["not_looked_up_over_limit"], ids[5:])
        self.assertEqual(sum(c.args[0].startswith(cve.NVD_URL) for c in get.call_args_list), 5)
        self.assertIn("at least one id", cve.cve_lookup(["nope"])["error"])

    def test_one_source_failing_does_not_hide_the_other(self):
        def get(url, **kw):
            if url.startswith(cve.EPSS_URL):
                raise FetchError("HTTP 503 from api.first.org")
            return NVD
        with mock.patch.object(cve, "get_json", get):
            e = cve.cve_lookup(["CVE-2021-44228"])["results"][0]
        self.assertTrue(e["found"])
        self.assertIsNone(e["epss"])
        self.assertEqual(e["errors"], ["EPSS: HTTP 503 from api.first.org"])

        def get2(url, **kw):
            if url.startswith(cve.EPSS_URL):
                return EPSS
            raise FetchError("HTTP 403 from services.nvd.nist.gov: rate limit")
        cve._nvd.clear()
        with mock.patch.object(cve, "get_json", get2):
            e = cve.cve_lookup(["CVE-2021-44228"])["results"][0]
        self.assertFalse(e["found"])
        self.assertEqual(e["epss"]["score"], 0.99999)
        self.assertIn("NVD:", e["errors"][0])


class Kev(unittest.TestCase):
    ENTRIES = [
        {"cveID": "CVE-2026-1", "dateAdded": "2026-11-23", "vendorProject": "Acme", "product": "NAS",
         "vulnerabilityName": "Acme NAS RCE", "shortDescription": "x", "knownRansomwareCampaignUse": "Known", "dueDate": "2026-12-14"},
        {"cveID": "CVE-2026-2", "dateAdded": "2026-11-18", "vendorProject": "B", "product": "C"},
        {"cveID": "CVE-2020-3", "dateAdded": "2021-11-03", "vendorProject": "D", "product": "E"},
    ]

    def test_recent(self):
        got = cve.recent_kev(self.ENTRIES, datetime.date(2026, 11, 18))
        self.assertEqual([g["id"] for g in got], ["CVE-2026-1", "CVE-2026-2"])
        self.assertEqual(got[0]["ransomware_use"], "Known")
        self.assertEqual(cve.recent_kev(self.ENTRIES, datetime.date(2026, 11, 24)), [])

    def test_tool(self):
        cve._kev.update(entries=[], fetched=0.0)
        with mock.patch.object(cve, "get_json", return_value={"vulnerabilities": self.ENTRIES, "catalogVersion": "v"}) as get:
            r = cve.kev_recent(days=100000)  # clamped to 90
            cve.kev_recent()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(r["catalogue_size"], 3)
        self.assertNotIn("CVE-2020-3", [a["id"] for a in r["added"]])
        cve._kev.update(entries=[], fetched=0.0)
        with mock.patch.object(cve, "get_json", side_effect=FetchError("HTTP 403 from www.cisa.gov")):
            self.assertIn("403", cve.kev_recent()["error"])


if __name__ == "__main__":
    unittest.main()
