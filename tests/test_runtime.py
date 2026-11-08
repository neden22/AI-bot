import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from orbit.runtime import InstanceLock


class RuntimeTests(unittest.TestCase):
    def test_end_to_end_and_worker_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = os.environ.copy()
            for key in ['TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','OLLAMA_MODEL']:
                env.pop(key, None)
            env['ORBIT_DB'] = str(Path(folder) / 'orbit.db')
            env['ORBIT_FEED'] = 'simulation'
            process = subprocess.Popen([sys.executable,'-m','orbit','--port',str(port)],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            base = f'http://127.0.0.1:{port}'

            def request(path, body=None, token=None, extra=None):
                headers = extra or {}
                if token:
                    headers['X-Orbit-Token'] = token
                if body is not None:
                    headers['Content-Type'] = 'application/json'
                req = Request(base+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
                with urlopen(req, timeout=3) as response:
                    return json.loads(response.read())

            def eventually(predicate, seconds=15):
                deadline = time.monotonic()+seconds
                last_error = None
                while time.monotonic() < deadline:
                    try:
                        value = predicate()
                        if value:
                            return value
                    except (OSError, ValueError) as exc:
                        last_error = exc
                    time.sleep(.1)
                self.fail(f'Timed out waiting for service: {last_error}')

            try:
                state = eventually(lambda: (s if len(s['quotes']) == 3 else None)
                                   if (s := request('/api/state')) else None)
                token = state['csrf']
                self.assertEqual(state['mode'], 'PAPER')
                self.assertTrue(all(q['source'] == 'SIMULATION' for q in state['quotes']))
                with self.assertRaises(RuntimeError):
                    with InstanceLock(env['ORBIT_DB']+'.lock'):
                        pass
                for headers, csrf in [({}, None), ({'Origin':'https://evil.example'},token),
                                      ({'Host':'evil.example'},token)]:
                    with self.assertRaises(HTTPError) as error:
                        request('/api/commands', {'kind':'kill','request_id':'security-test'}, csrf, headers)
                    self.assertEqual(error.exception.code, 403)
                body = {'kind':'buy','payload':{'symbol':'BTC-USD','budget':'250'},'request_id':'exactly-once-buy'}
                first = request('/api/commands',body,token)
                duplicate = request('/api/commands',body,token)
                self.assertEqual(first['id'], duplicate['id'])
                result = eventually(lambda: (r if r['status'] != 'queued' else None)
                                    if (r := request('/api/commands/'+str(first['id']))) else None)
                self.assertEqual(result['status'],'done')
                self.assertEqual(len(request('/api/state')['orders']),1)
                collision = {**body,'payload':{'symbol':'ETH-USD','budget':'250'}}
                with self.assertRaises(HTTPError) as error:
                    request('/api/commands',collision,token)
                self.assertEqual(error.exception.code,400)
                bad = request('/api/commands', {'kind':'close','payload':{'id':10**100},'request_id':'huge-order-id'},token)
                eventually(lambda: request('/api/commands/'+str(bad['id']))['status'] == 'failed')
                before = request('/api/state')['meta']['worker_pid']
                os.kill(before, signal.SIGTERM)
                after = eventually(lambda: (s if s['meta']['worker_pid'] != before and
                    s['meta']['heartbeat'] > state['meta']['heartbeat'] else None)
                    if (s := request('/api/state')) else None)
                self.assertNotEqual(before, after['meta']['worker_pid'])
                self.assertEqual(len(after['orders']),1)
                self.assertEqual(sum(not r['bucket'].startswith('manual') for r in after['reports']),1)
                reply = request('/api/chat',{'question':'Стан рахунку?'},token)
                self.assertEqual(reply['provider'],'rules')
                self.assertIn('PAPER',reply['answer'])
                with urlopen(base+'/api/export.csv', timeout=3) as response:
                    self.assertIn('BTC-USD',response.read().decode('utf-8-sig'))
            finally:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=3)
                    self.fail(f'Service did not shut down. Output: {stdout.decode()} {stderr.decode()}')
                if process.returncode != 0 and os.name != 'nt':
                    self.fail(f'Unclean service shutdown: {stdout.decode()} {stderr.decode()}')


if __name__ == '__main__':
    unittest.main()
