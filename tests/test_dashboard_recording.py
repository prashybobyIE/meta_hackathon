"""Progress and resource descriptions must stay truthful across demo states."""
import json
import shutil
import subprocess
import unittest

from agent_marketplace.dashboard import DASHBOARD
from test_dashboard_ui import DOM

TAIL = r'''
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 const progress=workflowProgress(payload.status,payload.trace||[]);
 const resources={};for(const id of ['requester','planner','worker-a','meta','verifier'])resources[id]=componentResource(id,payload.status);
 chooseComponent('requester');
 process.stdout.write(JSON.stringify({progress,resources,inspector:content(elements['component-resources']),phase:elements['progress-current'].textContent,count:elements['progress-count'].textContent,metaNode:elements['resource-meta'].textContent}));
})().catch(error=>{process.stderr.write(error.stack);process.exit(1)});
'''


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for dashboard checks')
class RecordingDashboardTests(unittest.TestCase):
    def run_ui(self, status, trace=()):
        script = DASHBOARD.split('<script>', 1)[1].split('</script>', 1)[0]
        result = subprocess.run([shutil.which('node'), '-e', DOM + script + TAIL],
                                input=json.dumps({'status': status, 'trace': trace}),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def status(**changes):
        value = {'job_id': 'job1', 'tasks': [{'kind': k, 'accepted': False} for k in ('parser', 'categories', 'summary')],
                 'attempts': [], 'events': [], 'plan': {'source': 'meta', 'tasks': {}},
                 'studio': {'phase': 'running', 'mode': 'meta', 'run_id': 'run1'},
                 'market': {'workers': [{'worker_id': 'worker-a', 'provider': 'meta', 'model': 'muse-spark-1.3'}], 'bids': [], 'events': [], 'attempts': [], 'credits': {}}, 'complete': False}
        value.update(changes)
        return value

    def test_all_module_acceptance_does_not_imply_final_application_delivery(self):
        status = self.status()
        for task in status['tasks']:
            task['accepted'] = True
        before = self.run_ui(status)
        self.assertEqual(before['progress']['stage'], 4)
        self.assertEqual(before['phase'], 'Verifying the assembled application')
        self.assertEqual(before['count'], '3 / 3 modules accepted')
        status['complete'] = True
        status['studio']['phase'] = 'complete'
        after = self.run_ui(status)
        self.assertEqual(after['progress']['stage'], 6)
        self.assertEqual(after['phase'], 'Verified work delivered')

    def test_parallel_work_and_verification_are_shown_as_concurrent(self):
        attempts = [{'attempt_id': 'a1', 'worker_id': 'worker-a', 'status': 'assigned', 'artifact_sha256': 'a' * 64},
                    {'attempt_id': 'a2', 'worker_id': 'worker-b', 'status': 'assigned'}]
        trace = [{'component': 'verifier', 'attempt_id': 'a1', 'event': 'verification_started'}]
        concurrent = self.run_ui(self.status(attempts=attempts), trace)
        self.assertEqual(concurrent['progress']['active'], [3, 4])
        self.assertIn('execution + independent verification', concurrent['progress']['title'])
        self.assertTrue(concurrent['progress']['detail'].startswith('1 worker process'))
        verifying = self.run_ui(self.status(attempts=attempts[:1]), trace)
        self.assertEqual(verifying['progress']['active'], [4])
        self.assertNotIn('Worker execution', verifying['progress']['title'])

    def test_resources_distinguish_local_agents_hosted_meta_and_free_simulation(self):
        live = self.run_ui(self.status())
        self.assertEqual(live['resources']['worker-a']['location'], 'This Mac · separate worker process')
        self.assertIn('hosted by Meta', live['resources']['worker-a']['compute'])
        self.assertEqual(live['resources']['meta']['location'], 'Meta hosted service')
        self.assertIn('Local orchestration', live['resources']['planner']['compute'])
        self.assertIn('Hosted', live['metaNode'])
        free_status = self.status(studio={'phase': 'running', 'mode': 'simulated-meta'}, market={'workers': [], 'bids': [], 'events': [], 'attempts': [], 'credits': {}})
        free = self.run_ui(free_status)
        self.assertEqual(free['resources']['meta']['location'], 'This Mac · local API simulator')
        self.assertEqual(free['resources']['meta']['compute'], 'No real Meta inference')
        self.assertIn('no AI', free['metaNode'])

    def test_resource_fields_are_projected_and_rendered_as_text(self):
        status = self.status(resources={'requester': {'location': '<example public text>', 'runtime': 'Python coordinator', 'api_key': 'DROP'}})
        result = self.run_ui(status)
        self.assertNotIn('api_key', result['resources']['requester'])
        self.assertNotIn('DROP', json.dumps(result))
        self.assertIn('<example public text>', result['inspector'])
        self.assertIn('Python coordinator', result['inspector'])

    def test_failure_does_not_claim_completed_delivery(self):
        status = self.status(studio={'phase': 'failed', 'mode': 'meta'})
        status['tasks'][0]['accepted'] = True
        result = self.run_ui(status)
        self.assertTrue(result['progress']['failed'])
        self.assertEqual(result['count'], '1 / 3 modules accepted')
        self.assertEqual(result['phase'], 'Run stopped before completion')
        self.assertNotEqual(result['progress']['stage'], 6)

    def test_workflow_progress_is_above_the_main_dashboard_and_not_duplicated(self):
        self.assertEqual(DASHBOARD.count('id="stepper"'), 1)
        self.assertLess(DASHBOARD.index('id="workflow-progress"'), DASHBOARD.index('<section class="stats"'))
        self.assertLess(DASHBOARD.index('id="workflow-progress"'), DASHBOARD.index('id="network"'))


if __name__ == '__main__':
    unittest.main()
