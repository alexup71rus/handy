import assert from 'node:assert/strict';
import {createJiti} from '../.runtime/pi/node_modules/jiti/lib/jiti.mjs';
import {fileURLToPath} from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
const jiti = createJiti(import.meta.url, {alias: {
  '@earendil-works/pi-tui': root + '.runtime/pi/node_modules/@earendil-works/pi-tui/dist/index.js',
}});
const handy = await jiti.import(root + 'integration/pi/handy.ts', {default:true});
const previousArgv = process.argv;
const previousFlag = process.env.HANDY_REQUIRE_MODEL_SELECTION;
function registered(flag, args = [], execution = async () => ({code:0,stderr:''})) {
  process.env.HANDY_REQUIRE_MODEL_SELECTION = flag;
  process.argv = ['node', 'pi', ...args];
  const handlers = new Map();
  handy({registerCommand(){}, registerToolRenderer(){}, getActiveTools:()=>[],
    on:(event, handler)=>handlers.set(event, handler), exec:execution});
  return handlers;
}
function context(provider) {
  return {model:{provider,id:'fixture'},hasUI:false,mode:'rpc',aborted:0,abort(){this.aborted++;}};
}
const event = {systemPromptOptions:{promptGuidelines:[]}};
try {
  let ctx = context('openai');
  const handlers = registered('1');
  await assert.rejects(handlers.get('before_agent_start')(event, ctx), /\/models/);
  assert.equal(ctx.aborted, 1);
  let notice;
  await handlers.get('session_start')({}, {...ctx,hasUI:true,cwd:'/tmp',ui:{setTitle(){},setHeader(){},notify:text=>notice=text}});
  assert.match(notice, /\/models/);
  for (const source of ['set','cycle','restore']) {
    const selected = registered('1');
    ctx = context('openai');
    selected.get('model_select')({source});
    if (source === 'restore') await assert.rejects(selected.get('before_agent_start')(event,ctx), /\/models/);
    else await selected.get('before_agent_start')(event,ctx);
    assert.equal(ctx.aborted,source === 'restore' ? 1 : 0);
  }
  ctx = context('openai');
  await assert.rejects(registered('1',['--','--model']).get('before_agent_start')(event,ctx), /\/models/);
  assert.equal(ctx.aborted,1);
  for (const [flag,args,provider] of [['0',[],'openai'],['1',['--model','openai/fixture'],'openai'],
                                    ['1',['--session','saved.jsonl'],'openai'],['1',[],'handy-provider:system']]) {
    ctx = context(provider);
    await registered(flag,args).get('before_agent_start')(event,ctx);
    assert.equal(ctx.aborted,0);
  }
  for (const exec of [async()=>({code:1,stderr:'cannot activate'}),async()=>{throw new Error('timeout');}]) {
    ctx = context('handy-system');
    await assert.rejects(registered('0',[],exec).get('before_agent_start')(event,ctx));
    assert.equal(ctx.aborted,1);
  }
  console.log('Registered pi model-selection guards passed');
} finally {
  process.argv = previousArgv;
  if (previousFlag === undefined) delete process.env.HANDY_REQUIRE_MODEL_SELECTION;
  else process.env.HANDY_REQUIRE_MODEL_SELECTION = previousFlag;
}
