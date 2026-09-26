#!/usr/bin/env python3
"""Owns a disposable PostgreSQL instance; accepts no database URL."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'postgres:17-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652'
LEGACY = ('actions chat clarify connections cursors dedup executor google_mcp google_scopes '
          'hermes_activity links migration noise pocket push todo_tools web whatsapp').split()


def clean_env():
    # Allowlist instead of trying to enumerate every provider's secret variable.
    result = {k: v for k, v in os.environ.items() if k in (
        'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'DOCKER_HOST', 'DOCKER_CONTEXT')}
    result['PYTHON_DOTENV_DISABLED'] = '1'
    return result


def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def child(case, all_tests, browser=False):
    sys.path.insert(0, str(ROOT))
    from tests.cloud.support import deny_remote
    sys.addaudithook(deny_remote)
    if browser:
        import runpy
        runpy.run_path(str(ROOT / 'scripts/verify/verify_durable_work_browser.py'), run_name='__main__')
        return 0
    import unittest
    suite = (unittest.defaultTestLoader.discover(str(ROOT / 'tests/cloud'), top_level_dir=str(ROOT))
             if all_tests else unittest.defaultTestLoader.loadTestsFromName(case))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--case')
    group.add_argument('--all', action='store_true')
    group.add_argument('--browser', action='store_true')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.case and (not args.case.startswith('tests.cloud.test_') or
                      not args.case.replace('_', '').replace('.', '').isalnum()):
        parser.error('only tests.cloud.test_* modules are allowed')
    if args.child:
        return child(args.case, args.all, args.browser)
    # Refuse inherited test configuration as well as any command-line DSN.
    if os.environ.get('ATHENA_VERIFY_DSN'):
        parser.error('a supplied test database is not allowed')
    name = 'athena-cloud-test-' + uuid4().hex
    marker = secrets.token_hex(32)
    deps = Path(tempfile.mkdtemp(prefix='athena-cloud-deps-'))
    created = False
    try:
        env = clean_env()
        install = subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                                  '--require-hashes', '--target', str(deps), '-r', str(ROOT / 'requirements-cloud.lock')],
                                 env=env, capture_output=True, text=True)
        if install.returncode:
            print(install.stdout, install.stderr)
            return install.returncode
        try:
            docker('image', 'inspect', IMAGE)
        except subprocess.CalledProcessError:
            print('Pulling pinned disposable PostgreSQL image', flush=True)
            docker('pull', IMAGE, timeout=300)
        env['POSTGRES_PASSWORD'] = secrets.token_hex(24)
        docker('run', '-d', '--pull=never', '--name', name, '--label', 'ai.athena.verify=cloud',
               '--memory=512m', '--cpus=2', '--pids-limit=128',
               '--tmpfs', '/var/lib/postgresql/data:rw', '-p', '127.0.0.1::5432',
               '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_USER=athena_verify',
               '-e', 'POSTGRES_DB=athena_verify', IMAGE, env=env)
        created = True
        info = json.loads(docker('inspect', name))[0]
        port = info['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
        deadline = time.monotonic() + 30
        while True:
            try:
                docker('exec', name, 'pg_isready', '-U', 'athena_verify', '-d', 'athena_verify')
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('test_database_readiness_timeout')
                time.sleep(.2)
        env['ATHENA_VERIFY_DSN'] = (f"postgresql://athena_verify:{env.pop('POSTGRES_PASSWORD')}"
                                   f'@127.0.0.1:{port}/athena_verify')
        env['ATHENA_VERIFY_MARKER'] = marker
        env['ATHENA_VERIFY_CONTAINER'] = name
        env['PYTHONPATH'] = os.pathsep.join((str(deps), str(ROOT)))
        print(f'Python {sys.version.split()[0]}; PostgreSQL image {info["Image"]}', flush=True)
        selection = ['--all'] if args.all else ['--browser'] if args.browser else ['--case', args.case]
        rc = subprocess.run([sys.executable, __file__, '--child', *selection],
                            env=env, cwd=ROOT).returncode
        if args.all:
            for name_part in LEGACY:
                result = subprocess.run([sys.executable, str(ROOT / f'scripts/verify/verify_{name_part}.py')],
                                        env=env, cwd=ROOT, capture_output=True, text=True)
                print(f'legacy {name_part}: {"PASS" if result.returncode == 0 else "FAIL"}', flush=True)
                if result.returncode:
                    print(result.stdout, result.stderr)
                    rc = 1
        return rc
    finally:
        if created:
            info = json.loads(docker('inspect', name))[0]
            if info['Name'] != '/' + name or info['Config']['Labels'].get('ai.athena.verify') != 'cloud':
                raise RuntimeError('test_container_ownership_mismatch')
            docker('rm', '-f', name)
        shutil.rmtree(deps)


if __name__ == '__main__':
    raise SystemExit(main())
