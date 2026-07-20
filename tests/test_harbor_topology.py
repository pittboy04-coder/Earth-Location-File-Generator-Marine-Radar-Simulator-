import unittest

from radarloc_generator.osm_query import _resolve_harbor_topology
from radarloc_generator.radarloc_builder import build_radarloc, validate_radarloc


def feature(feature_id, feature_class, points, *, closed=False):
    return {
        "id": feature_id,
        "name": feature_id,
        "feature_class": feature_class,
        "closed": closed,
        "points": [{"x": x, "y": y} for x, y in points],
    }


class HarborTopologyTests(unittest.TestCase):
    def test_range_cut_shoreline_terminates_on_ring(self):
        resolved, audit = _resolve_harbor_topology([
            feature("coast", "coastline", [(-150.0, 0.0), (0.0, 0.0), (150.0, 0.0)]),
        ], radius_m=100.0)

        self.assertEqual(audit["unresolved_endpoint_count"], 0)
        self.assertEqual(audit["range_clipped_feature_count"], 1)
        self.assertAlmostEqual(abs(resolved[0]["points"][0]["x"]), 100.0, places=1)
        self.assertAlmostEqual(abs(resolved[0]["points"][-1]["x"]), 100.0, places=1)

    def test_two_dangling_shorelines_continue_to_shared_intersection(self):
        resolved, audit = _resolve_harbor_topology([
            feature("west", "coastline", [(-100.0, 0.0), (-20.0, 0.0)]),
            feature("north", "coastline", [(0.0, 20.0), (0.0, 100.0)]),
        ], radius_m=100.0)

        self.assertEqual(audit["unresolved_endpoint_count"], 0)
        self.assertEqual(audit["extension_count"], 2)
        self.assertEqual(resolved[0]["points"][-1], {"x": 0.0, "y": 0.0})
        self.assertEqual(resolved[1]["points"][0], {"x": 0.0, "y": 0.0})

    def test_centerline_waterways_are_not_turned_into_boundaries(self):
        source = feature("creek", "stream", [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)])
        resolved, audit = _resolve_harbor_topology([source], radius_m=100.0)

        self.assertEqual(resolved[0]["points"], source["points"])
        self.assertNotIn("topology_extensions", resolved[0])
        self.assertEqual(audit["boundary_endpoint_count"], 0)

    def test_open_channel_is_not_closed_by_unrelated_parallel_lines(self):
        resolved, audit = _resolve_harbor_topology([
            feature("north_bank", "coastline", [(-100.0, 15.0), (-20.0, 15.0)]),
            feature("south_bank", "coastline", [(-100.0, -15.0), (-20.0, -15.0)]),
        ], radius_m=100.0)

        self.assertEqual(audit["unresolved_endpoint_count"], 2)
        self.assertEqual(resolved[0]["points"][-1], {"x": -20.0, "y": 15.0})
        self.assertEqual(resolved[1]["points"][-1], {"x": -20.0, "y": -15.0})

    def test_short_redundant_shoreline_fragment_does_not_fail_topology_audit(self):
        resolved, audit = _resolve_harbor_topology([
            feature(
                "river_poly",
                "river",
                [(0.0, 0.0), (20.0, 0.0), (20.0, 40.0), (0.0, 40.0), (0.0, 0.0)],
                closed=True,
            ),
            feature(
                "coast_shard",
                "coastline",
                [(0.0, 0.0), (19.0, 2.0), (19.0, 52.0)],
            ),
        ], radius_m=100.0)

        self.assertEqual(audit["unresolved_endpoint_count"], 0)
        shard = next(item for item in resolved if item["name"] == "coast_shard")
        self.assertEqual(shard["topology_unresolved_endpoints"], [])
        self.assertTrue(shard.get("topology_redundant_with_closed_boundary", False))

    def test_radarloc_metadata_carries_topology_audit(self):
        coast = feature("coast", "coastline", [(-10.0, 0.0), (10.0, 0.0)])
        coast["topology_unresolved_endpoints"] = ["start"]
        coast["topology_extensions"] = [{"endpoint": "end", "kind": "segment", "length_m": 5.0}]
        coast["topology_range_clipped"] = True

        document = build_radarloc("test", 0.0, 0.0, 1.0, [coast])
        metadata = document["metadata"]
        self.assertEqual(metadata["topology_unresolved_endpoint_count"], 1)
        self.assertEqual(metadata["topology_extension_count"], 1)
        self.assertEqual(metadata["topology_range_clipped_feature_count"], 1)
        self.assertFalse(metadata["topology_audit_passed"])
        validation = validate_radarloc(document)
        self.assertFalse(validation["valid"])
        self.assertEqual(validation["stats"]["topology_unresolved_endpoints"], 1)


if __name__ == "__main__":
    unittest.main()
