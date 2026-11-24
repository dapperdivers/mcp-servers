import os
import unittest
from unittest import mock

from mcp_servers import deals
from mcp_servers.httpjson import FetchError

DRIVE = {
    "title": "HGST Ultrastar He12 12TB SAS 12Gb/s 7.2K HUH721212AL5200",
    "price": {"value": "139.99", "currency": "USD"},
    "shippingOptions": [{"shippingCost": {"value": "19.00"}}, {"shippingCost": {"value": "12.50"}}],
    "condition": "Used",
    "seller": {"username": "x", "feedbackPercentage": "99.8", "feedbackScore": 5123},
    "itemLocation": {"city": "Austin", "stateOrProvince": "Texas", "country": "US"},
    "itemWebUrl": "https://www.ebay.com/itm/123?hash=abc",
}

LOTS = [
    {"itemName": "DELL R740 SERVER", "lotInfo": "<p>Dell R740 Server, various sizes</p>", "aucEndDt": "2026-11-27",
     "propertyCity": "CORVALLIS ", "propertyState": "OR", "saleNo": "S1", "lotNo": "008", "itemDescURL": "https://x/1"},
    {"itemName": "2012 Flat Rack Trailer", "lotInfo": "trailer", "aucEndDt": "2026-12-01",
     "propertyCity": "Roswell", "propertyState": "NM", "saleNo": "S2", "lotNo": "001", "itemDescURL": "https://x/2"},
    {"itemName": "2019 Ford F-150", "lotInfo": "pickup with ladder rack &amp; toolbox", "aucEndDt": "2026-11-25",
     "propertyCity": "Denver", "propertyState": "CO", "saleNo": "S3", "lotNo": "002", "itemDescURL": "https://x/3"},
    {"itemName": "Cisco switches", "lotInfo": None, "aucEndDt": "2026-11-26",
     "propertyCity": "Perry Point", "propertyState": "MD", "saleNo": "S4", "lotNo": "003", "itemDescURL": "https://x/4"},
]


class ShapeEbayItem(unittest.TestCase):
    def test_single_drive(self):
        o = deals.shape_ebay_item(DRIVE)
        self.assertEqual(o["shipping"], 12.5)  # the cheapest shipping option
        self.assertEqual(o["total"], 152.49)
        self.assertEqual(o["capacity_tb"], 12.0)
        self.assertEqual(o["usd_per_tb"], 12.71)
        self.assertFalse(o["multi_unit_listing"])
        self.assertEqual(o["url"], "https://www.ebay.com/itm/123")  # tracking query dropped
        self.assertEqual(o["location"], "Austin, Texas, US")

    def test_lots_get_no_per_tb_price(self):
        for title in ("Lot of 10 WD 14TB SAS drives", "4x 16TB Seagate Exos", "16TB Exos x4", "14TB HDD qty 5", "5 pcs 12TB"):
            o = deals.shape_ebay_item({"title": title, "price": {"value": "800"}})
            self.assertTrue(o["multi_unit_listing"], title)
            self.assertIsNone(o["usd_per_tb"], title)

    def test_no_capacity_no_shipping(self):
        o = deals.shape_ebay_item({"title": "Supermicro X12SPi-TF motherboard", "price": {"value": "610.00"}})
        self.assertIsNone(o["capacity_tb"])
        self.assertIsNone(o["shipping"])
        self.assertEqual(o["total"], 610.0)

    def test_decimal_capacity_is_not_misread(self):
        self.assertEqual(deals.shape_ebay_item({"title": "1.2TB SAS 10K", "price": {"value": "20"}})["capacity_tb"], 1.2)

    def test_missing_price(self):
        o = deals.shape_ebay_item({"title": "12TB drive"})
        self.assertIsNone(o["total"])
        self.assertIsNone(o["usd_per_tb"])


class EbaySearch(unittest.TestCase):
    def setUp(self):
        deals._token.update(value="", expires=0.0)

    def test_params(self):
        p = deals.ebay_search_params("12TB SAS", "used", min_price=50, max_price=0, limit=500)
        self.assertEqual(p["limit"], 50)
        self.assertEqual(p["sort"], "price")
        self.assertIn("buyingOptions:{FIXED_PRICE}", p["filter"])
        self.assertIn("itemLocationCountry:US", p["filter"])
        self.assertIn("price:[50..],priceCurrency:USD", p["filter"])
        self.assertNotIn("7000", p["filter"])  # "for parts" is never searched
        self.assertNotIn("price:", deals.ebay_search_params("x")["filter"])

    def test_summary_ignores_lots(self):
        items = [{"total": t, "multi_unit_listing": m} for t, m in ((10, True), (100, False), (120, False), (None, False))]
        self.assertEqual(
            deals.summarize_ebay(items), {"lowest_total": 100, "median_of_cheapest_5": 110.0, "single_unit_listings": 2}
        )
        self.assertIsNone(deals.summarize_ebay([])["lowest_total"])

    def test_not_configured(self):
        with mock.patch.dict(os.environ, {"EBAY_CLIENT_ID": "", "EBAY_CLIENT_SECRET": ""}):
            self.assertIn("not configured", deals.ebay_search("12TB SAS")["error"])

    def test_search_gets_a_token_once_and_shapes_results(self):
        calls = []

        def fake(url, **kw):
            calls.append((url, kw))
            if url == deals.EBAY_TOKEN_URL:
                return {"access_token": "tok", "expires_in": 7200}
            return {"total": 321, "itemSummaries": [DRIVE]}

        env = {"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": "secret"}
        with mock.patch.dict(os.environ, env), mock.patch.object(deals, "get_json", fake):
            first = deals.ebay_search("12TB SAS")
            deals.ebay_search("14TB SAS")
        self.assertEqual([u == deals.EBAY_TOKEN_URL for u, _ in calls], [True, False, False])
        self.assertEqual(calls[0][1]["headers"]["Authorization"], "Basic aWQ6c2VjcmV0")  # base64("id:secret")
        self.assertEqual(calls[1][1]["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(calls[1][1]["headers"]["X-EBAY-C-MARKETPLACE-ID"], "EBAY_US")
        self.assertEqual(first["matches_on_ebay"], 321)
        self.assertEqual(first["summary"]["lowest_total"], 152.49)

    def test_api_error_is_returned_not_raised(self):
        env = {"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": "secret"}
        with mock.patch.dict(os.environ, env), mock.patch.object(deals, "get_json", side_effect=FetchError("HTTP 500")):
            self.assertEqual(deals.ebay_search("x"), {"error": "HTTP 500"})
            self.assertIn("condition must be", deals.ebay_search("x", "broken")["error"])


class GsaAuctions(unittest.TestCase):
    def test_whole_word_match_and_order(self):
        found, total = deals.match_gsa_lots(LOTS, ["rack", "Dell R740", "switches"])
        self.assertEqual(total, 4)
        # name matches first, soonest close first; description-only matches last
        self.assertEqual([f["name"] for f in found], ["Cisco switches", "DELL R740 SERVER", "2012 Flat Rack Trailer", "2019 Ford F-150"])
        self.assertEqual(found[-1]["matched_in_description_only"], ["rack"])
        self.assertEqual(found[1]["pickup"], "CORVALLIS, OR")

    def test_partial_words_do_not_match(self):
        self.assertEqual(deals.match_gsa_lots(LOTS, ["switch"])[1], 0)  # "switches" is another word
        self.assertEqual(deals.match_gsa_lots(LOTS, ["R74"])[1], 0)

    def test_names_only_state_and_limit(self):
        self.assertEqual(deals.match_gsa_lots(LOTS, ["rack"], search_description=False)[1], 1)
        self.assertEqual(deals.match_gsa_lots(LOTS, ["rack"], state="co")[0][0]["name"], "2019 Ford F-150")
        found, total = deals.match_gsa_lots(LOTS, ["rack", "Dell R740", "switches"], limit=2)
        self.assertEqual((len(found), total), (2, 4))

    def test_description_is_cleaned(self):
        found, _ = deals.match_gsa_lots(LOTS, ["rack"], state="CO")
        self.assertEqual(found[0]["description"], "pickup with ladder rack & toolbox")

    def test_tool(self):
        deals._gsa.update(lots=[], fetched=0.0)
        with mock.patch.object(deals, "get_json", return_value={"Results": LOTS}) as fake:
            r = deals.gsa_auctions_search(["Dell R740"])
            deals.gsa_auctions_search(["rack"])  # served from the cache
        self.assertEqual(fake.call_count, 1)
        self.assertEqual((r["active_lots_checked"], r["matching"]), (4, 1))
        self.assertIn("at least one keyword", deals.gsa_auctions_search([" "])["error"])
        deals._gsa.update(lots=[], fetched=0.0)
        with mock.patch.object(deals, "get_json", side_effect=FetchError("HTTP 429 from api.gsa.gov")):
            self.assertIn("429", deals.gsa_auctions_search(["x"])["error"])


if __name__ == "__main__":
    unittest.main()
