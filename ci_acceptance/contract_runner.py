import os
import sys
import types
import unittest
import xml.etree.ElementTree as ET

os.chdir('/tmp')
assert not any('/work/repo' in p for p in sys.path)
namespace = {'__name__': 'validation_contract'}
exec(compile(sys.argv[1], '<trusted-validation-contract>', 'exec'), namespace)


class Recorded(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self.records[test.id()] = {
            'classname': test.__class__.__module__ + '.' + test.__class__.__qualname__,
            'name': test._testMethodName,
        }

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.records[test.id()]['failure'] = self._exc_info_to_string(err, test)

    def addError(self, test, err):
        super().addError(test, err)
        self.records[test.id()]['error'] = self._exc_info_to_string(err, test)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.records[test.id()]['skipped'] = reason

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.records[test.id()]['skipped'] = 'expected failure requires review'

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.records[test.id()]['failure'] = 'unexpected success requires review'

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            self.records[test.id()]['failure'] = self._exc_info_to_string(err, test)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = {}


suite = unittest.defaultTestLoader.loadTestsFromModule(types.SimpleNamespace(**namespace))
result = unittest.TextTestRunner(verbosity=2, resultclass=Recorded).run(suite)
rows = list(result.records.values())
root = ET.Element('testsuites')
group = ET.SubElement(
    root,
    'testsuite',
    name='trusted-installed-wheel',
    tests=str(len(rows)),
    failures=str(sum('failure' in r for r in rows)),
    errors=str(sum('error' in r for r in rows)),
    skipped=str(sum('skipped' in r for r in rows)),
)
for row in rows:
    node = ET.SubElement(group, 'testcase', classname=row['classname'], name=row['name'])
    for status in ('failure', 'error', 'skipped'):
        if status in row:
            ET.SubElement(node, status).text = row[status]
ET.ElementTree(root).write('/tmp/results.xml', encoding='unicode')
sys.exit(
    0
    if result.wasSuccessful() and rows and not result.skipped and not result.expectedFailures
    else 1
)
