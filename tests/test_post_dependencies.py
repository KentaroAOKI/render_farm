import sys
import unittest
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import read_json
from farm.post import check_dependencies, process_outputs
from farm.store import Store
from farm.worker import diagnose


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / 'runtime' / ('dependency_' + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        for name in ('ffmpeg.exe', 'python.exe', 'model.pth', 'models.py', 'fastdvdnet.py'):
            (self.root / name).write_bytes(b'fixture')
        self.config = {'ffmpeg': str(self.root / 'ffmpeg.exe'), 'fastdvdnet_python': str(self.root / 'python.exe'),
                       'fastdvdnet_repo': str(self.root), 'fastdvdnet_model': str(self.root / 'model.pth')}

    def test_torch_timeout_keeps_ffmpeg_available(self):
        with patch('farm.post.subprocess.run', side_effect=[subprocess.CompletedProcess([], 0),
                   subprocess.TimeoutExpired('torch', 60)]):
            log = self.root / 'logs' / 'post_dependencies.json'
            result = check_dependencies(self.root, self.config, log_path=log)
        self.assertTrue(result['ffmpeg'])
        self.assertFalse(result['fastdvdnet'])
        self.assertIn('fastdvdnet', result['dependency_errors'])
        checks = read_json(log)['checks']
        self.assertEqual(checks['ffmpeg']['status'], 'ok')
        self.assertEqual(checks['fastdvdnet']['status'], 'timeout')
        self.assertEqual(checks['fastdvdnet']['timeout_seconds'], 60)
        self.assertIn('elapsed_seconds', checks['fastdvdnet'])

    def test_nonzero_exit_records_stderr(self):
        with patch('farm.post.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 7, stdout=b'output', stderr=b'import failed')):
            log = self.root / 'logs' / 'post_dependencies.json'
            result = check_dependencies(self.root, self.config, required={'fastdvdnet'}, log_path=log)
        self.assertFalse(result['fastdvdnet'])
        check = read_json(log)['checks']['fastdvdnet']
        self.assertEqual(check['status'], 'exit_nonzero')
        self.assertEqual(check['returncode'], 7)
        self.assertEqual(check['stderr_tail'], 'import failed')

    def test_failed_job_keeps_dependency_log(self):
        job = {'settings': {'post': {'video': True, 'fastdvdnet': True}}}
        work = self.root / 'job'
        work.mkdir()
        with patch('farm.post.subprocess.run', side_effect=[subprocess.CompletedProcess([], 0),
                   subprocess.TimeoutExpired('torch', 60)]):
            with self.assertRaisesRegex(RuntimeError, 'post_dependencies.json'):
                process_outputs(self.root, work, job, self.config, lambda: False)
        checks = read_json(work / 'logs' / 'post_dependencies.json')['checks']
        self.assertEqual(checks['ffmpeg']['status'], 'ok')
        self.assertEqual(checks['fastdvdnet']['status'], 'timeout')
    def test_video_only_never_imports_torch(self):
        with patch('farm.post.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            result = check_dependencies(self.root, self.config, required={'ffmpeg'})
        self.assertTrue(result['ffmpeg'])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:], ['-version'])

    def test_optional_dependency_failure_does_not_remove_maya_capability(self):
        Store(self.root)
        with patch('farm.worker.installations', return_value={'2027': 'Maya2027'}), \
             patch('farm.worker.probe', return_value={'oiiotool': 'oiiotool.exe', 'plugins': {'mtoa': 'test'}, 'gpu': True}), \
             patch('farm.post.check_dependencies', return_value={'ffmpeg': True, 'fastdvdnet': False,
                   'dependency_errors': {'fastdvdnet': 'timeout'}}):
            caps, errors = diagnose(self.root, self.config)
        self.assertIn('2027', caps)
        self.assertTrue(caps['2027']['ffmpeg'])
        self.assertFalse(caps['2027']['fastdvdnet'])
        self.assertFalse(errors)
