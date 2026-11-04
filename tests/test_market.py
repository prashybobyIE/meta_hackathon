from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent_marketplace.market import MarketStore, create_server
from agent_marketplace.protocol import ApiError, json_request


class Clock:
    def __init__(self):
        self.now = 1000.0
    def __call__(self):
        return self.now


class MarketStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'market.sqlite3'
        self.clock = Clock()
        self.store = MarketStore(self.path, 'requester-secret', 'enroll-secret', self.clock)
        self.worker = self.register('worker-a')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, token='requester-secret'):
        return self.store.handle(method, path, token, body)

    def register(self, name, capability='python-module-v1', slots=1):
        return self.call('POST', '/v1/workers/register', {'worker_id': name, 'capabilities': [capability], 'provider': 'fixture', 'model': 'reference-fixture', 'slots': slots}, 'enroll-secret')

    def task(self, task_id='task-a', **changes):
        body = {'task_id': task_id, 'capability': 'python-module-v1', 'sensitivity': 'public', 'max_credits': 20, 'expires_at': self.clock() + 100}
        body.update(changes)
        return self.call('POST', '/v1/tasks', body)

    def bid(self, task_id='task-a', worker=None, credits=10):
        return self.call('POST', f'/v1/tasks/{task_id}/bids', {'credits': credits}, (worker or self.worker)['worker_token'])

    def assign(self, bid=None, task_id='task-a', attempt_id='attempt-a', seconds=5, **changes):
        body = {'bid_id': (bid or self.bid(task_id))['bid_id'], 'attempt_id': attempt_id, 'lease_seconds': seconds, 'requester_url': 'http://127.0.0.1:9876', 'dispatch_token': 'dispatch-only-public-token'}
        body.update(changes)
        return self.call('POST', f'/v1/tasks/{task_id}/assign', body)

    def receipt(self, attempt='attempt-a', receipt_id='receipt-a', **changes):
        body = {'attempt_id': attempt, 'artifact_sha256': 'a' * 64, 'report_sha256': 'b' * 64, 'receipt_id': receipt_id}
        body.update(changes)
        return body

    def assert_status(self, status, callback):
        with self.assertRaises(ApiError) as cm:
            callback()
        self.assertEqual(cm.exception.status, status)

    def test_accept_is_single_payment_and_survives_restart(self):
        self.task()
        self.assign()
        self.assertEqual(self.store.snapshot()['credits'], {'reserved': 10, 'settled': 0})
        result = self.call('POST', '/v1/tasks/task-a/accept', self.receipt())
        self.assertFalse(result['duplicate'])
        self.assertEqual(result['receipt']['worker_id'], 'worker-a')
        self.assertEqual(self.store.snapshot()['credits'], {'reserved': 0, 'settled': 10})
        self.store.close()
        self.store = MarketStore(self.path, 'requester-secret', 'enroll-secret', self.clock)
        self.assertTrue(self.call('POST', '/v1/tasks/task-a/accept', self.receipt())['duplicate'])
        self.assertEqual(self.store.snapshot()['credits']['settled'], 10)
        self.assert_status(409, lambda: self.call('POST', '/v1/tasks/task-a/accept', self.receipt(artifact_sha256='c' * 64)))
        assignments = self.call('GET', '/v1/workers/worker-a/assignments', token=self.worker['worker_token'])
        self.assertEqual(assignments['assignments'], [])

    def test_expiry_reopens_releases_and_fences_late_result(self):
        self.task()
        self.assign()
        self.clock.now += 5
        snap = self.store.snapshot()
        self.assertEqual(snap['tasks'][0]['state'], 'open')
        self.assertEqual(snap['attempts'][0]['status'], 'expired')
        self.assertEqual(snap['credits'], {'reserved': 0, 'settled': 0})
        self.assert_status(409, lambda: self.call('POST', '/v1/tasks/task-a/accept', self.receipt()))
        self.assert_status(409, lambda: self.assign(attempt_id='attempt-a'))
        second = self.register('worker-b')
        self.assign(self.bid(worker=second), attempt_id='attempt-b')
        self.assert_status(409, lambda: self.call('POST', '/v1/tasks/task-a/accept', self.receipt()))
        accepted = self.call('POST', '/v1/tasks/task-a/accept', self.receipt(attempt='attempt-b', receipt_id='receipt-b'))
        self.assertEqual(accepted['receipt']['worker_id'], 'worker-b')
        self.assertEqual(self.store.snapshot()['credits']['settled'], 10)

    def test_requester_rejection_releases_without_rewards_or_diagnostic_leaks(self):
        self.task()
        self.assign()
        self.call('POST', '/v1/tasks/task-a/reject', {'attempt_id': 'attempt-a', 'reason': 'secret-source-details-not-for-market'})
        snap = self.store.snapshot()
        self.assertEqual(snap['credits'], {'reserved': 0, 'settled': 0})
        self.assertEqual(snap['tasks'][0]['state'], 'open')
        self.assertNotIn('secret-source-details', json.dumps(snap))
        self.assert_status(409, lambda: self.call('POST', '/v1/tasks/task-a/accept', self.receipt()))

    def test_roles_and_worker_scope_cannot_accept_or_hijack_identity(self):
        self.task()
        self.assign()
        self.assert_status(401, lambda: self.call('POST', '/v1/tasks/task-a/accept', self.receipt(), self.worker['worker_token']))
        other = self.register('worker-b')
        self.assert_status(403, lambda: self.call('GET', '/v1/workers/worker-a/assignments', token=other['worker_token']))
        self.assert_status(409, lambda: self.register('worker-a'))
        self.assert_status(401, lambda: self.call('POST', '/v1/tasks', {}, 'enroll-secret'))
        self.assert_status(401, lambda: self.call('GET', '/v1/tasks', token='requester-secret'))

    def test_snapshot_excludes_credentials_payloads_and_dispatch_destination(self):
        self.task()
        self.assign()
        serialized = json.dumps(self.store.snapshot())
        for secret in ['requester-secret', 'enroll-secret', self.worker['worker_token'], 'dispatch-only-public-token', 'http://127.0.0.1:9876', 'token_hash']:
            self.assertNotIn(secret, serialized)
        actual = self.call('GET', '/v1/workers/worker-a/assignments', token=self.worker['worker_token'])
        self.assertEqual(actual['assignments'][0]['dispatch_token'], 'dispatch-only-public-token')

    def test_confidential_work_never_trusts_unprotected_workers(self):
        self.task(sensitivity='confidential')
        self.assertEqual(self.call('GET', '/v1/tasks', token=self.worker['worker_token'])['tasks'], [])
        self.assert_status(403, lambda: self.bid())
        body = {'worker_id': 'claimed-protected', 'capabilities': ['python-module-v1'], 'provider': 'fixture', 'model': 'reference', 'slots': 1, 'protected': True}
        self.assert_status(400, lambda: self.call('POST', '/v1/workers/register', body, 'enroll-secret'))

    def test_capacity_liveness_capability_and_budget_are_enforced(self):
        self.task()
        other = self.register('wrong-capability', capability='different-capability')
        self.assert_status(409, lambda: self.bid(worker=other))
        self.assert_status(400, lambda: self.bid(credits=21))
        self.task('task-b')
        bid_b = self.bid('task-b')
        self.assign()
        self.assert_status(409, lambda: self.assign(bid_b, task_id='task-b', attempt_id='attempt-b'))
        self.clock.now += 16
        self.assertEqual(self.call('GET', '/v1/tasks/task-b/bids')['bids'], [])
        self.call('POST', '/v1/workers/worker-a/heartbeat', {}, self.worker['worker_token'])
        self.assertEqual(len(self.call('GET', '/v1/tasks/task-b/bids')['bids']), 1)

    def test_assignments_are_atomic_under_concurrent_authorization(self):
        self.task()
        bid = self.bid()
        barrier = threading.Barrier(2)
        results = []
        def run(attempt):
            barrier.wait()
            try:
                self.assign(bid, attempt_id=attempt)
                results.append(200)
            except ApiError as exc:
                results.append(exc.status)
        threads = [threading.Thread(target=run, args=(f'attempt-{i}',)) for i in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(results, [200, 409])
        self.assertEqual(self.store.snapshot()['credits']['reserved'], 10)
        self.assertEqual(len(self.store.snapshot()['attempts']), 1)

    def test_validation_rejects_bool_nonfinite_unknown_data_and_unsafe_urls(self):
        self.assert_status(400, lambda: self.task(max_credits=True))
        self.assert_status(400, lambda: self.task(expires_at=float('nan')))
        self.assert_status(400, lambda: self.task(expires_at=10 ** 1000))
        self.assert_status(400, lambda: self.task(private_prompt='must never persist'))
        self.assert_status(400, lambda: self.call('POST', '/v1/workers/register', {'worker_id':'bad-unicode','capabilities':['python-module-v1'],'provider':'fixture','model':'\ud800','slots':1}, 'enroll-secret'))
        self.task()
        self.assert_status(400, lambda: self.assign(requester_url='http://example.com'))
        self.assert_status(400, lambda: self.assign(requester_url='https://user:pass@example.com'))
        self.assert_status(400, lambda: self.assign(requester_url='http://127.0.0.1:9876/path'))
        self.assert_status(400, lambda: self.assign(lease_seconds=True))


class MarketHTTPTests(unittest.TestCase):
    def setUp(self):
        self.store = MarketStore(':memory:', 'requester-secret', 'enroll-secret')
        self.server = create_server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()

    def test_http_auth_and_registration(self):
        self.assertFalse(json_request('GET', self.url + '/health')['confidential_execution'])
        with self.assertRaises(ApiError) as cm:
            json_request('POST', self.url + '/v1/tasks', {}, token='wrong')
        self.assertEqual(cm.exception.status, 401)
        result = json_request('POST', self.url + '/v1/workers/register', {'worker_id':'worker-http','capabilities':['python-module-v1'],'provider':'fixture','model':'reference','slots':1}, token='enroll-secret')
        self.assertIn('worker_token', result)
        self.assertNotIn(result['worker_token'], json.dumps(json_request('GET', self.url + '/v1/snapshot')))

    def test_bad_json_oversize_and_duplicate_fields_are_json_errors(self):
        for payload, headers, status in [
            (b'{"worker_id":"a","worker_id":"b"}', {}, 400),
            (b'{"expires_at":NaN}', {}, 400),
            (b'[]', {}, 400),
            (b'{}', {'Content-Length':'999999'}, 413),
            (b'{}', {'Content-Type':'text/plain'}, 415),
        ]:
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
            actual = {'Content-Type':'application/json', 'Authorization':'Bearer enroll-secret'}
            actual.update(headers)
            conn.request('POST', '/v1/workers/register', body=payload, headers=actual)
            response = conn.getresponse()
            self.assertEqual(response.status, status)
            self.assertIn('error', json.loads(response.read()))
            conn.close()

    def test_non_loopback_bind_is_explicit(self):
        with self.assertRaises(ValueError):
            create_server(self.store, '0.0.0.0')


if __name__ == '__main__':
    unittest.main()
