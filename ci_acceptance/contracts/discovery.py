import unittest


class DiscoveryContract(unittest.TestCase):
    def test_public_api(self):
        from ci_demo import parse_timeout

        self.assertEqual(parse_timeout('0.25'), 0.25)

    def test_existing_implementation(self):
        from ci_demo import parse_timeout
        from ci_demo.config import parse_timeout as implementation

        self.assertIs(parse_timeout, implementation)

    def test_installed_origin(self):
        import sys
        from pathlib import Path

        import ci_demo

        self.assertTrue(Path(ci_demo.__file__).is_relative_to(sys.prefix))
