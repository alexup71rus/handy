import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { boundToolResult, TOOL_TEXT_BYTES } from '../integration/pi/context.ts';
const root=await mkdtemp(join(tmpdir(),'handy-output-'));
try {
 const short={content:[{type:'text',text:'small result'}]};
 assert.equal(await boundToolResult(short,root),undefined);
 const text='Русский текст с данными\n'.repeat(2000);
 const result=await boundToolResult({content:[{type:'text',text}],structuredContent:{text}},root);
 assert(Buffer.byteLength(result.content[0].text)<=TOOL_TEXT_BYTES);
 assert.equal(result.structuredContent,undefined);
 assert(!result.content[0].text.includes('\uFFFD'));
 const path=result.content[0].text.match(/Полный результат: (.+?)\. Читай/)[1];
 assert.equal(await readFile(path,'utf8'),text);
 console.log('Tool-output budget: 5 checks passed');
} finally {await rm(root,{recursive:true,force:true});}
