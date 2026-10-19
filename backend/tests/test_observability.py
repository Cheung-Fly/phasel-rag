import unittest
from types import SimpleNamespace

from app.observability import metrics_route_path


class MetricsRoutePathTests(unittest.TestCase):
    def test_uses_route_template_instead_of_concrete_path(self):
        route = SimpleNamespace(path="/api/tasks/{task_id}")
        self.assertEqual(metrics_route_path(route), "/api/tasks/{task_id}")

    def test_unmatched_requests_share_one_bounded_label(self):
        self.assertEqual(metrics_route_path(None), "_unmatched")
        self.assertEqual(metrics_route_path(SimpleNamespace()), "_unmatched")


if __name__ == "__main__":
    unittest.main()
