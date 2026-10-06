#!/usr/bin/env python3
"""Install codex-accounts into ~/.local/bin without changing your shell settings."""
import os
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import importlib.machinery
import importlib.util

import codexx_updates

source = Path(__file__).resolve().with_name('codex-accounts')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin', help='Installation directory (default: ~/.local/bin)')
parser.add_argument('--setup', action='store_true', help='Run CLI account setup after installation')
parser.add_argument('--codexx-only', action='store_true', help='Add codexx alongside an existing installation without replacing codex-accounts')
args = parser.parse_args()
bin_dir = args.bin_dir.expanduser().resolve()
destination = bin_dir / 'codex-accounts'
wrapper_source = source.with_name('codexx')
wrapper_destination = bin_dir / 'codexx'
if args.codexx_only and args.setup:
    sys.exit('--codexx-only cannot be combined with --setup; run codexx accounts setup separately.')
if args.codexx_only and not destination.is_file():
    sys.exit('--codexx-only requires an existing codex-accounts in ' + str(bin_dir))
if wrapper_destination.exists() and 'Codex-compatible entry point using the existing codex-accounts installation' not in wrapper_destination.read_text():
    sys.exit('Refusing to overwrite an unrelated file: ' + str(wrapper_destination))
if destination.exists() and 'Local ChatGPT account selection for Codex CLI' not in destination.read_text():
    sys.exit('Refusing to overwrite an unrelated file: ' + str(destination))
if not shutil.which('codex'):
    sys.exit('Install Codex CLI first; codex was not found on PATH.')
bin_dir.mkdir(parents=True, exist_ok=True)
if not args.codexx_only or (bin_dir / '.codexx/current').exists():
    loader = importlib.machinery.SourceFileLoader('codexx_install_engine', str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    app = importlib.util.module_from_spec(spec)
    loader.exec_module(app)
    os.umask(0o077)
    generation = codexx_updates.install(source.parent, bin_dir, app, entry_only=args.codexx_only)
    print('Installed versioned Codexx bundle:', generation)
    print('Entry points:', bin_dir / 'codexx', 'and', bin_dir / 'codex-accounts')
    print('Accounts and running services are retained. Updates: codexx update')
    if str(bin_dir) not in os.environ.get('PATH', '').split(os.pathsep):
        import shlex
        print('Add to your shell: export PATH=' + shlex.quote(str(bin_dir)) + ':"$PATH"')
    if args.setup:
        import subprocess
        sys.exit(subprocess.call([sys.executable, str(bin_dir / 'codexx'), 'accounts', 'setup']))
    sys.exit(0)
fd, wrapper_temp = tempfile.mkstemp(dir=bin_dir)
os.close(fd)
try:
    shutil.copyfile(wrapper_source, wrapper_temp)
    os.chmod(wrapper_temp, 0o755)
    os.replace(wrapper_temp, wrapper_destination)
finally:
    if os.path.exists(wrapper_temp): os.unlink(wrapper_temp)
print('Installed:', wrapper_destination)
helper = bin_dir / 'codexx_updates.py'
if helper.exists() and 'Atomic, versioned installation of the Codexx entry point' not in helper.read_text():
    sys.exit('Refusing to overwrite an unrelated file: ' + str(helper))
fd, helper_temp = tempfile.mkstemp(dir=bin_dir)
os.close(fd)
try:
    shutil.copyfile(source.with_name('codexx_updates.py'), helper_temp)
    os.chmod(helper_temp, 0o600)
    os.replace(helper_temp, helper)
finally:
    if os.path.exists(helper_temp): os.unlink(helper_temp)
print('Uses the existing codex-accounts installation and data. No services restarted.')
print('For versioned bundle updates, run python3 install.py without --codexx-only.')
