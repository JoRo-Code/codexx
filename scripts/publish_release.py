#!/usr/bin/env python3
"""Publish complete GitHub release assets, making the release visible last."""
import argparse
import ast
import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import tempfile

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codexx_updates

REPO = 'JoRo-Code/codexx'
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--notes-file', type=Path, required=True)
parser.add_argument('--prerelease', action='store_true')
args = parser.parse_args()
notes = args.notes_file.resolve()
if not notes.is_file(): raise SystemExit('Release notes file is missing.')
def git(*args):
    return subprocess.check_output(['git', *args], cwd=root)
if git('status', '--porcelain').strip(): raise SystemExit('Commit changes before publishing.')
source = git('show', 'HEAD:codex-accounts')
tree = ast.parse(source.decode())
version = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in n.targets))
tag = 'v' + version
if git('rev-parse', tag + '^{commit}').strip() != git('rev-parse', 'HEAD').strip():
    raise SystemExit('The version tag must point at HEAD. Push the tag before publishing.')
with tempfile.TemporaryDirectory(prefix='codex-accounts-release-') as temp:
    directory = Path(temp)
    # Validate the exact committed pair before making any release visible.
    for name in codexx_updates.FILES:
        (directory / name).write_bytes(git('show', 'HEAD:' + name))
    loader = importlib.machinery.SourceFileLoader('release_engine', str(directory / 'codex-accounts'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    engine = importlib.util.module_from_spec(spec)
    loader.exec_module(engine)
    codexx_updates.validate(directory, engine)
    binary = directory / 'codex-accounts'
    binary.write_bytes(source)
    checksum = directory / 'codex-accounts.sha256'
    checksum.write_text(hashlib.sha256(source).hexdigest() + '  codex-accounts\n')
    archive = directory / ('codexx-' + version + '.tar.gz')
    subprocess.run(['git', 'archive', '--format=tar.gz', '--prefix=codexx-' + version + '/',
                    '-o', str(archive), tag], cwd=root, check=True)
    archive_checksum = directory / (archive.name + '.sha256')
    archive_checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
    command = ['gh', 'release', 'create', tag, str(binary), str(checksum), str(archive), str(archive_checksum),
               '--repo', REPO, '--verify-tag', '--draft', '--title', tag, '--notes-file', str(notes)]
    if args.prerelease: command.append('--prerelease')
    subprocess.run(command, check=True)
    # Readers never see a release without all its verification/download assets.
    subprocess.run(['gh', 'release', 'edit', tag, '--repo', REPO, '--draft=false'], check=True)
print('Published https://github.com/' + REPO + '/releases/tag/' + tag)
