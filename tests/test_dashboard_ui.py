"""Exercise dashboard state/render logic without network calls or a browser dependency."""
import json
import shutil
import subprocess
import unittest

from agent_marketplace.dashboard import DASHBOARD


DOM = r'''
const payload=JSON.parse(require('fs').readFileSync(0,'utf8'));
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.listeners={};this.attributes={};this.value='';this.checked=true;this.scrollTop=0;this.className='';this.textContent='';this.classList={add:()=>{},remove:()=>{},toggle:()=>{}}}
  append(...items){this.children.push(...items)}
  appendChild(item){this.children.push(item);return item}
  replaceChildren(...items){this.children=items;this.textContent=''}
  setAttribute(key,value){this.attributes[key]=value}
  getAttribute(key){return this.attributes[key]}
  addEventListener(name,handler){this.listeners[name]=handler}
  getBoundingClientRect(){return {width:100,height:100}}
  remove(){}
}
const elements={};
const components=['requester','planner','market','worker-a','worker-b','worker-fault','meta','verifier','assembler','ledger'].map(component=>{const n=new Element('button');n.dataset.component=component;return n});
const tabs=['tasks','bids','api','result'].map(tab=>{const n=new Element('button');n.dataset.tab=tab;return n});
const steps=Array.from({length:6},()=>new Element());
const document={getElementById:id=>elements[id]||(elements[id]=new Element()),createElement:tag=>new Element(tag),createElementNS:(ns,tag)=>new Element(tag),createTextNode:value=>({textContent:value}),querySelectorAll:selector=>selector==='[data-component]'?components:selector==='[data-tab]'?tabs:selector==='.step'?steps:[],querySelector:selector=>selector.includes('demo-session')?{content:'test-control-capability'}:components.find(n=>selector.includes('"'+n.dataset.component+'"'))||null};
elements.mode=new Element('select');elements.mode.value='simulated-meta';
document.documentElement=new Element('html');
const window={matchMedia:()=>({matches:true})};
const setInterval=()=>0,setTimeout=()=>0;
let currentResponse=payload.status;
let sampleResponse;
async function fetch(path,options){if(path==='/v1/application/run')return await new Promise(resolve=>sampleResponse=resolve);return {ok:true,json:async()=>currentResponse}}
function content(element){return (element.textContent||'')+element.children.map(child=>content(child)).join('')}
'''

FINISH = r'''
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  const output={spend:elements.spend.textContent,status:elements.status.textContent,connected:elements['connection-label'].textContent};
  selected='worker-b';output.workerCallerMatched=matchesComponent({component:'meta',caller_component:'worker-b',event:'model_call_started'});
  events=[{id:'1',time:1,component:'meta',event:'model_call_started',phase:'started',caller_component:'planner',operation:'planning',provider:'simulated-meta'},{id:'2',time:2,component:'meta',event:'model_call_completed',phase:'completed',caller_component:'planner',operation:'planning',provider:'simulated-meta'}];
  output.calls=makeApiCalls().map(call=>({caller:call.caller,start:call.start,end:call.end}));
  if(payload.staleSample){
    state={...payload.status,complete:true,studio:{...payload.status.studio,phase:'complete',run_id:'old-run'}};
    const pending=elements['run-application'].listeners.click();
    state={...state,studio:{...state.studio,run_id:'new-run'}};
    sampleResponse({ok:true,json:async()=>({report:{total_cents:999},run_id:'old-run'})});
    await pending;
    output.staleError=elements['application-error'].textContent;
    output.customResult=customResult;
  }
  process.stdout.write(JSON.stringify(output));
})().catch(error=>{process.stderr.write(error.stack);process.exit(1)});
'''


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for dashboard logic checks')
class DashboardUiTests(unittest.TestCase):
    def run_ui(self, status, **options):
        script = DASHBOARD.split('<script>', 1)[1].split('</script>', 1)[0]
        result = subprocess.run([shutil.which('node'), '-e', DOM + script + FINISH],
                                input=json.dumps({'status': status, **options}),
                                capture_output=True, text=True, timeout=10, check=True)
        return json.loads(result.stdout)

    @staticmethod
    def status(**changes):
        value = {'tasks': [], 'attempts': [], 'events': [], 'market': {'workers': [], 'bids': [], 'events': [], 'attempts': [], 'credits': {'reserved': 0, 'settled': 0}}, 'complete': False}
        value.update(changes)
        return value

    def test_legacy_observer_renders_without_studio_or_custom_result(self):
        result = self.run_ui(self.status())
        self.assertEqual(result['connected'], 'Backend connected')
        self.assertTrue(result['workerCallerMatched'])
        self.assertEqual(result['calls'], [{'caller': 'planner', 'start': 1, 'end': 2}])

    def test_rehearsal_never_displays_simulated_accounting_as_actual_meta_spend(self):
        result = self.run_ui(self.status(studio={'phase': 'idle', 'mode': 'simulated-meta', 'run_id': None}, budget={'used_usd': 12, 'run_used_usd': .1234}))
        self.assertEqual(result['spend'], '$0.0000')
        self.assertEqual(result['status'], 'Ready to submit')

    def test_live_spend_uses_current_run_and_rejects_stale_sample_result(self):
        result = self.run_ui(self.status(studio={'phase': 'complete', 'mode': 'meta', 'run_id': 'old-run'}, complete=True, budget={'used_usd': 12, 'run_used_usd': .1234}), staleSample=True)
        self.assertEqual(result['spend'], '$0.1234')
        self.assertIn('run changed', result['staleError'])
        self.assertIsNone(result['customResult'])


if __name__ == '__main__':
    unittest.main()
