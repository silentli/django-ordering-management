"""One wheel/install/contract runner for every acceptance package."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def execute(argv):
    subprocess.run([str(x) for x in argv], check=True, cwd='/tmp')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project', choices=['logic', 'discovery', 'dependency'])
    parser.add_argument('--wheelhouse', type=Path, required=True)
    args = parser.parse_args()
    home = Path(__file__).resolve().parent
    source = home / args.project
    wheelhouse = args.wheelhouse.resolve()
    allowed = {}
    for line in (home / 'dependencies.lock').read_text().splitlines():
        if line and not line.startswith('#'):
            name = line.split()[0]
            if not re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+', name):
                raise ValueError('Only pinned approved dependencies are supported')
            allowed[name.lower()] = line
    deps = tomllib.loads((source / 'pyproject.toml').read_text())['project'].get('dependencies', [])
    if any(dep.lower() not in allowed for dep in deps):
        raise ValueError('Unapproved package dependency')
    with tempfile.TemporaryDirectory(prefix='ci-package-') as temporary:
        work = Path(temporary)
        for name in ['builder', 'application']:
            execute([sys.executable, '-I', '-m', 'venv', work / name])
        builder = work / 'builder/bin/python'
        application = work / 'application/bin/python'
        execute(
            [
                builder,
                '-I',
                '-m',
                'pip',
                '--isolated',
                'install',
                '--no-index',
                '--no-deps',
                '--require-hashes',
                '--find-links',
                wheelhouse,
                '-r',
                home / 'build-tools.lock',
            ]
        )
        clean_code = "\n".join(
            [
                "import importlib.metadata,json",
                "names = sorted(d.metadata['Name'] for d in importlib.metadata.distributions())",
                "print(json.dumps(names))",
            ]
        )
        clean = subprocess.run(
            [str(application), '-I', '-c', clean_code],
            check=True,
            capture_output=True,
            text=True,
            cwd='/tmp',
        )
        if json.loads(clean.stdout) != ['pip']:
            raise RuntimeError('Application environment is not clean')
        execute(
            [
                builder,
                '-I',
                '-m',
                'pip',
                '--isolated',
                'wheel',
                '--no-index',
                '--no-deps',
                '--no-build-isolation',
                '--wheel-dir',
                work / 'dist',
                source,
            ]
        )
        wheels = list((work / 'dist').glob('*.whl'))
        if len(wheels) != 1:
            raise RuntimeError('Expected exactly one project wheel')
        wheel = wheels[0]
        lock = work / 'install.lock'
        lock.write_text(
            str(wheel)
            + ' --hash=sha256:'
            + hashlib.sha256(wheel.read_bytes()).hexdigest()
            + '\n'
            + ''.join(allowed[d.lower()] + '\n' for d in deps)
        )
        execute(
            [
                application,
                '-I',
                '-m',
                'pip',
                '--isolated',
                'install',
                '--no-index',
                '--require-hashes',
                '--find-links',
                wheelhouse,
                '-r',
                lock,
            ]
        )
        execute(
            [
                application,
                '-I',
                home / 'contract_runner.py',
                (home / 'contracts' / (args.project + '.py')).read_text(),
            ]
        )


if __name__ == '__main__':
    main()
