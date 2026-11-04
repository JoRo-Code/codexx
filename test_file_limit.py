"""File-limit recovery must work in an existing router during live adoption."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from test_launcher import app


class FileLimitTests(unittest.TestCase):
    def test_low_limit_is_raised_without_changing_hard_limit(self):
        with patch.object(app.resource, 'getrlimit', return_value=(256, 4096)), \
                patch.object(app.resource, 'setrlimit') as change:
            app.prepare_file_limit()
            change.assert_called_once_with(app.resource.RLIMIT_NOFILE, (1024, 4096))

    def test_respects_hard_limit_and_preserves_higher_limits(self):
        for limits, expected in [((256, 512), (512, 512)),
                                 ((2048, 4096), None),
                                 ((256, app.resource.RLIM_INFINITY), (1024, app.resource.RLIM_INFINITY)),
                                 ((app.resource.RLIM_INFINITY, app.resource.RLIM_INFINITY), None)]:
            with self.subTest(limits=limits), \
                    patch.object(app.resource, 'getrlimit', return_value=limits), \
                    patch.object(app.resource, 'setrlimit') as change:
                app.prepare_file_limit()
                if expected is None:
                    change.assert_not_called()
                else:
                    change.assert_called_once_with(app.resource.RLIMIT_NOFILE, expected)

    def test_module_reload_recovers_exhausted_process_without_restart(self):
        source = str(Path(__file__).with_name('codex-accounts'))
        script = '''
import os, resource
source = open(%r).read()
namespace = {'__name__': 'file_limit_test', '__file__': %r}
exec(compile(source, %r, 'exec'), namespace)
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
fds = []
try:
    while True:
        fds.append(os.open('/dev/null', os.O_RDONLY))
except OSError:
    pass
pid = os.getpid()
exec(compile(source, %r, 'exec'), namespace)
extra = os.open('/dev/null', os.O_RDONLY)
assert extra >= 256
assert os.getpid() == pid
os.close(extra)
for fd in fds:
    os.close(fd)
''' % (source, source, source, source)
        subprocess.run([sys.executable, '-c', script], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
