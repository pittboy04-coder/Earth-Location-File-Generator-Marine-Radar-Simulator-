"""Offline tests for the detail-query and resilience upgrades.

These run entirely from the on-disk Overpass cache / synthetic payloads, so
they work without network access. Regenerate the cache by running
generate_location.py for Charleston Harbor once while online.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radarloc_generator import osm_query as q


class FeatureClassFromTags(unittest.TestCase):
    def test_structures(self):
        self.assertEqual(q._feature_class_from_tags({"man_made": "pier"}, ""), "pier")
        self.assertEqual(q._feature_class_from_tags({"man_made": "breakwater"}, ""), "breakwater")
        self.assertEqual(q._feature_class_from_tags({"waterway": "dock"}, ""), "dock")
        self.assertEqual(q._feature_class_from_tags({"leisure": "marina"}, ""), "dock")

    def test_wetlands(self):
        self.assertEqual(
            q._feature_class_from_tags({"natural": "wetland", "wetland": "saltmarsh"}, ""),
            "saltmarsh")
        self.assertEqual(q._feature_class_from_tags({"natural": "wetland"}, ""), "wetland")
        self.assertEqual(q._feature_class_from_tags({"natural": "mud"}, ""), "tidalflat")

    def test_water(self):
        self.assertEqual(q._feature_class_from_tags({"natural": "coastline"}, ""), "coastline")
        self.assertEqual(q._feature_class_from_tags({"waterway": "riverbank"}, ""), "river")
        self.assertEqual(q._feature_class_from_tags({"water": "lake"}, ""), "lake")


class QueryGroups(unittest.TestCase):
    def test_core_group_always_present_and_required(self):
        groups = q._build_overpass_query_groups(32.76, -79.90, 8334.0, "default")
        self.assertEqual(groups[0]["name"], "water_core")
        self.assertTrue(groups[0]["required"])
        self.assertTrue(all(not g["required"] for g in groups[1:]))

    def test_small_range_gets_detail_groups(self):
        names = {g["name"] for g in q._build_overpass_query_groups(32.76, -79.90, 700.0, "harbor_tidal")}
        self.assertIn("structures", names)
        self.assertIn("wetlands", names)
        self.assertIn("water_tidal", names)

    def test_wide_range_drops_structures(self):
        names = {g["name"] for g in q._build_overpass_query_groups(32.76, -79.90, 40_000.0, "default")}
        self.assertNotIn("structures", names)
        self.assertNotIn("wetlands", names)


class PayloadValidation(unittest.TestCase):
    def test_good_payload(self):
        self.assertIsNone(q._validate_overpass_payload({"elements": []}))

    def test_truncated_payload_rejected(self):
        problem = q._validate_overpass_payload(
            {"elements": [1], "remark": "runtime error: Query timed out"})
        self.assertIsNotNone(problem)

    def test_malformed_payload_rejected(self):
        self.assertIsNotNone(q._validate_overpass_payload({"nope": 1}))


class CacheRoundTrip(unittest.TestCase):
    def test_write_then_read(self):
        query = "[out:json];node(1);out;  /* cache-roundtrip-test */"
        payload = {"elements": [{"type": "node", "id": 1, "lat": 0.0, "lon": 0.0}]}
        q._write_query_cache(query, payload)
        try:
            cached = q._read_query_cache(query)
            self.assertIsNotNone(cached)
            self.assertEqual(cached["elements"][0]["id"], 1)
        finally:
            try:
                os.remove(q._cache_path_for_query(query))
            except OSError:
                pass


class PruningPassthrough(unittest.TestCase):
    def _pier(self, x):
        return {
            "id": f"way_{x}", "name": "pier", "feature_class": "pier",
            "closed": False,
            "points": [{"x": x, "y": 5000.0}, {"x": x + 30.0, "y": 5040.0}],
        }

    def test_detail_features_survive_pruning(self):
        water = [{
            "id": "w1", "name": "harbor", "feature_class": "water", "closed": True,
            "points": [{"x": -400.0, "y": -400.0}, {"x": 400.0, "y": -400.0},
                       {"x": 400.0, "y": 400.0}, {"x": -400.0, "y": 400.0},
                       {"x": -400.0, "y": -400.0}],
        }]
        # Far-away piers disconnected from the water network must survive.
        features = water + [self._pier(6000.0 + i * 100.0) for i in range(4)]
        kept = q._prune_origin_connected_harbor_network(
            features, radius_m=8000.0, simplify_epsilon=5.0)
        kept_classes = [f["feature_class"] for f in kept]
        self.assertEqual(kept_classes.count("pier"), 4)

    def test_merge_does_not_stitch_piers(self):
        # Two pier stubs 10 m apart must NOT be merged into one chain.
        a = self._pier(0.0)
        b = self._pier(40.0)
        merged = q._merge_open_feature_geometries(
            [a, b], simplify_epsilon=5.0, radius_m=8000.0)
        piers = [f for f in merged if f["feature_class"] == "pier"]
        self.assertEqual(len(piers), 2)
        self.assertTrue(all(not f.get("closed", False) for f in piers))


if __name__ == "__main__":
    unittest.main(verbosity=2)
