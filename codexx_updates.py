"""Atomic, versioned installation of the Codexx entry point and account engine."""
import argparse
import ast
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

BUNDLE_FORMAT = 1
FILES = ('codexx', 'codex-accounts', 'codexx_updates.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def literal(payload, name):
    tree = ast.parse(payload.decode('utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Missing bundle field: ' + name)


def metadata(app):
    return app.read_json(app.install_marker(Path(app.__file__).resolve()), {})


def bundle_root(app):
    value = metadata(app).get('bundle_root')
    if not value:
        raise app.Error('Install the versioned bundle first: python3 install.py')
    return Path(value)


@contextlib.contextmanager
def bundle_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def point(root, name, generation):
    """A single rename is the commit point for the whole bundle."""
    temporary = root / ('.' + name + '-' + str(os.getpid()))
    try:
        temporary.symlink_to(os.path.relpath(generation, root))
        os.replace(temporary, root / name)
    finally:
        temporary.unlink(missing_ok=True)


def validate(directory, app):
    for name in FILES:
        compile((directory / name).read_bytes(), str(directory / name), 'exec')
    version = app.installed_version(directory / 'codex-accounts')
    if literal((directory / 'codexx_updates.py').read_bytes(), 'BUNDLE_FORMAT') != BUNDLE_FORMAT:
        raise app.Error('This update requires a newer bundle installer.')
    env = dict(os.environ, CODEX_ACCOUNTS_NO_UPDATE='1')
    for command in (['codex-accounts', '--version'], ['codexx', '--bundle-version']):
        result = subprocess.run([sys.executable, str(directory / command[0]), command[1]],
                                capture_output=True, text=True, timeout=15, env=env)
        if result.returncode or result.stdout.strip() != version:
            raise app.Error('Bundle startup check failed: ' + command[0])
    return version


def stage(root, payloads, app, origin):
    identity = digest(b''.join(name.encode() + b'\0' + payloads[name] for name in FILES))
    releases = root / 'versions'
    releases.mkdir(exist_ok=True, mode=0o700)
    destination = releases / identity
    if destination.exists():
        if any((destination / name).read_bytes() != payloads[name] for name in FILES):
            raise app.Error('Existing bundle generation failed its integrity check.')
        validate(destination, app)
        return destination
    temporary = Path(tempfile.mkdtemp(prefix='.staging-', dir=releases))
    try:
        for name in FILES:
            with (temporary / name).open('wb') as stream:
                stream.write(payloads[name]); stream.flush(); os.fsync(stream.fileno())
            (temporary / name).chmod(0o755 if name != 'codexx_updates.py' else 0o600)
        version = validate(temporary, app)
        app.atomic_json(temporary / '.codex-accounts.install.json', {
            'repository': app.RELEASE_REPO, 'bundle_root': str(root), 'version': version})
        app.atomic_json(temporary / 'bundle.json', {
            'format': BUNDLE_FORMAT, 'version': version, 'generation': identity,
            'engine': digest(payloads['codex-accounts']),
            'live_abi': literal(payloads['codex-accounts'], 'LIVE_RUNTIME_ABI'),
            'hashes': {name: digest(payloads[name]) for name in FILES},
            'origin': origin, 'installed_at': time.time()})
        os.replace(temporary, destination)
    finally:
        if temporary.exists(): shutil.rmtree(temporary)
    return destination


def activate(root, generation):
    current = root / 'current'
    if current.exists() and current.resolve() == generation:
        return False
    if current.exists():
        point(root, 'previous', current.resolve())
    point(root, 'current', generation)
    return True


def install(source, bin_dir, app, entry_only=False):
    root = bin_dir / '.codexx'
    with app.lock('update'), bundle_lock(root):
        payloads = {name: (source / name).read_bytes() for name in FILES}
        if entry_only:
            payloads['codex-accounts'] = (bin_dir / 'codex-accounts').read_bytes()
        # Validate before replacing either public entry point.
        generation = stage(root, payloads, app, {'kind': 'checkout', 'path': str(source)})
        for name in ('codexx', 'codex-accounts'):
            destination = bin_dir / name
            signature = (b'Codex-compatible entry point using the existing codex-accounts installation'
                         if name == 'codexx' else b'Local ChatGPT account selection for Codex CLI')
            if destination.exists() and signature not in destination.read_bytes():
                raise app.Error('Refusing to overwrite an unrelated file: ' + str(destination))
        activate(root, generation)
        for name in ('codexx', 'codex-accounts'):
            destination = bin_dir / name
            backup = bin_dir / ('.' + name + '.pre-bundle')
            if destination.exists() and not backup.exists() and not destination.is_symlink():
                app.write_binary(backup, destination.read_bytes())
        # Pin the pair at invocation. A running entry point never imports a
        # different generation's engine during an atomic current-pointer change.
        shim = '''#!/usr/bin/env python3
"""Codex-compatible entry point using the existing codex-accounts installation."""
import os, sys
from pathlib import Path
target = (Path(__file__).absolute().parent / '.codexx/current').resolve(strict=True) / 'codexx'
os.execv(sys.executable, [sys.executable, str(target), *sys.argv[1:]])
'''
        app.write_binary(bin_dir / 'codexx', shim.encode())
        # Legacy live loaders already watch this path; it remains engine source.
        target = bin_dir / '.codex-accounts-link'
        try:
            target.symlink_to('.codexx/current/codex-accounts')
            os.replace(target, bin_dir / 'codex-accounts')
        finally:
            target.unlink(missing_ok=True)
        app.atomic_json(app.install_marker(bin_dir / 'codex-accounts'), {
            'repository': app.RELEASE_REPO, 'bundle_root': str(root)})
    return generation


def download(app, release):
    version = release['tag_name'].lstrip('v')
    name = 'codexx-' + version + '.tar.gz'
    asset = app.release_asset(release, name)
    checksum = app.release_asset(release, name + '.sha256')
    parts = app.fetch_public(checksum['browser_download_url'], 4096).decode('ascii').split()
    if len(parts) != 2 or parts[1].lstrip('*') != name:
        raise app.Error('Invalid bundle checksum file.')
    payload = app.fetch_public(asset['browser_download_url'], 10_000_000)
    actual = digest(payload)
    if actual != parts[0].lower() or asset.get('digest', 'sha256:' + actual) not in (None, 'sha256:' + actual):
        raise app.Error('Bundle checksum mismatch; current installation was retained.')
    prefix = 'codexx-' + version + '/'
    result = {}
    # Never extract archive paths, links, permissions, or other unneeded files.
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        for member in archive:
            if member.name not in [prefix + n for n in FILES]: continue
            name = member.name[len(prefix):]
            if name in result or not member.isfile() or member.size > 2_000_000:
                raise app.Error('Invalid bundle archive member.')
            result[name] = archive.extractfile(member).read()
    if set(result) != set(FILES): raise app.Error('Release does not contain a complete Codexx bundle.')
    if literal(result['codex-accounts'], 'VERSION') != version:
        raise app.Error('Bundle version does not match the release.')
    return result


def update(app, check=False, rollback=False, prerelease=False, source=None):
    root = bundle_root(app)
    with app.lock('update'), bundle_lock(root):
        current = app.read_json(root / 'current/bundle.json')
        if rollback:
            previous = root / 'previous'
            if not previous.exists(): raise app.Error('No previous bundle is available.')
            destination = previous.resolve()
            manifest = app.read_json(destination / 'bundle.json')
            if any(digest((destination / n).read_bytes()) != manifest['hashes'][n] for n in FILES):
                raise app.Error('Previous bundle failed its integrity check.')
            validate(destination, app)
            activate(root, destination)
            app.record_update(policy='notify', installed_version=manifest['version'], last_error=None)
            print('Rolled back the entire bundle. Update policy: notify.')
            return True
        if source:
            source = Path(source).expanduser().resolve()
            payloads = {name: (source / name).read_bytes() for name in FILES}
            origin = {'kind': 'checkout', 'path': str(source)}
        else:
            release = app.newest_release(prerelease)
            target = release['tag_name'].lstrip('v')
            app.record_update(last_check=time.time(), available_version=target, last_error=None)
            if app.version_tuple(target) <= app.version_tuple(current['version']):
                print('Already up to date (' + current['version'] + ').')
                return False
            if check:
                print('Update available: ' + current['version'] + ' -> ' + target)
                return False
            payloads = download(app, release)
            origin = {'kind': 'release', 'tag': release['tag_name']}
        generation = stage(root, payloads, app, origin)
        changed = activate(root, generation)
        manifest = app.read_json(generation / 'bundle.json')
        app.record_update(installed_version=manifest['version'], last_error=None)
        print(('Installed ' if changed else 'Already installed: ') + manifest['version'] + ' (' + generation.name[:12] + ').')
        print('Running work is retained. Compatible launcher code is adopted between turns.')
        return changed


def session_status(app, request=False):
    root = bundle_root(app)
    target = app.read_json(root / 'current/bundle.json')
    if request:
        app.atomic_json(app.ROOT / 'session-updates.json', {
            'generation': target['engine'], 'requested_at': time.time()})
    rows = []
    for run in app.active_runs():
        if not run.get('live_runtime_abi'):
            state = 'restart_required'
        elif run['live_runtime_abi'] != target['live_abi']:
            state = 'incompatible_restart_required'
        elif run.get('launcher_generation') == target['engine']:
            state = 'current'
        else:
            state = 'pending_safe_point'
        rows.append({k: run.get(k) for k in ('pid', 'session', 'account', 'phase')} | {'update': state})
    return {'version': target['version'], 'generation': target['generation'], 'sessions': rows,
            'scope': 'launcher code only; native Codex backends and open terminal code are not restarted'}


def main(app, args):
    parser = argparse.ArgumentParser(prog='codexx update', description=__doc__)
    parser.add_argument('action', nargs='?', choices=('all', 'status', 'policy'))
    parser.add_argument('value', nargs='?')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true')
    group.add_argument('--rollback', action='store_true')
    group.add_argument('--from-repo', type=Path)
    parser.add_argument('--prerelease', action='store_true')
    parser.add_argument('--json', action='store_true')
    options = parser.parse_args(args)
    if options.action == 'policy':
        if options.check or options.rollback or options.from_repo or options.prerelease or options.json:
            parser.error('policy cannot be combined with update flags')
        if options.value not in (None, 'auto', 'notify', 'off'): parser.error('policy must be auto, notify, or off')
        return app.update_policy(options.value)
    if options.action == 'all' and options.value != 'sessions': parser.error('use: codexx update all sessions')
    if options.value and options.action not in ('all', 'policy'): parser.error('unexpected argument')
    if options.prerelease and (options.rollback or options.from_repo):
        parser.error('--prerelease only applies to release checks and downloads')
    if options.json and options.action not in ('status', 'all'):
        parser.error('--json is supported by status and all sessions')
    if options.action == 'status' or options.action == 'all':
        if options.check or options.rollback or options.from_repo or options.prerelease:
            parser.error('install/check a version separately before requesting session updates')
        result = session_status(app, request=options.action == 'all')
        if options.json: print(json.dumps(result, indent=2))
        else:
            print('Installed bundle: ' + result['version'] + ' (' + result['generation'][:12] + ')')
            for row in result['sessions']:
                print(str(row['session'] or row['pid']) + '  ' + str(row['account']) + '  ' + row['update'])
            print(result['scope'])
            print('Legacy or incompatible sessions are left running; reopen individually when convenient.')
        return
    update(app, options.check, options.rollback, options.prerelease, options.from_repo)
