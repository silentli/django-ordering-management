import unittest


class TimeoutContract(unittest.TestCase):
    def test_fraction(self):
        from ci_demo.config import parse_timeout

        self.assertEqual(parse_timeout('0.25'), 0.25)
        self.assertEqual(parse_timeout('1.5'), 1.5)

    def test_integer(self):
        from ci_demo.config import parse_timeout

        self.assertEqual(parse_timeout('12'), 12)

    def test_invalid_values(self):
        from ci_demo.config import parse_timeout

        for value in ('0', '-1', 'nan', 'inf', '-inf', 'invalid'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_timeout(value)

    def test_installed_origin(self):
        import sys
        from pathlib import Path

        import ci_demo.config

        self.assertTrue(Path(ci_demo.config.__file__).is_relative_to(sys.prefix))
