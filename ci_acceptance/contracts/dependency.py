import unittest


class DependencyContract(unittest.TestCase):
    def test_mapping(self):
        from ci_demo.loader import load

        self.assertEqual(load('answer: 42'), {'answer': 42})

    def test_sequence(self):
        from ci_demo.loader import load

        self.assertEqual(load('items: [a, b]'), {'items': ['a', 'b']})

    def test_approved_installed_version(self):
        from importlib.metadata import version

        self.assertEqual(version('PyYAML'), '6.0.3')
