import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
const base = import.meta.dirname;
const source = '/workspace/scratch/1ff238d8a5f3/Scriber/Frontend';
const bun = '/workspace/scratch/1ff238d8a5f3/Scriber-study/scripts/toolchain-study/runtime-tools/node_modules/bun/bin/bun.exe';
const hash = value => createHash('sha256').update(value).digest('hex');
const state = directory => Object.fromEntries(['package.json','package-lock.json','bun.lock','bun.lockb'].map(name=>[name,fs.existsSync(path.join(directory,name))?hash(fs.readFileSync(path.join(directory,name))):null]));
const report = {node:process.version,bunExecutable:bun,source,base,cases:{}};
function save(){fs.writeFileSync(path.join(base,'results.json'),JSON.stringify(report,null,2)+'\n');}
function runCase(name, extra=[]) {
 const directory=path.join(base,name);
 const before=state(directory);
 const args=['install','--frozen-lockfile','--no-save',...extra];
 console.log('Starting '+name);
 const result=spawnSync(bun,args,{cwd:directory,encoding:'utf8',timeout:120000,killSignal:'SIGKILL',maxBuffer:32*1024*1024,env:{...process.env,BUN_INSTALL_CACHE_DIR:path.join(base,'cache'),NO_COLOR:'1',FORCE_COLOR:'0'},detached:process.platform!=='win32'});
 const text=(result.stdout??'')+(result.stderr??'');
 fs.writeFileSync(path.join(base,name+'.log'),text);
 const after=state(directory);
 const nodeModules=path.join(directory,'node_modules');
 const metadata=fs.existsSync(nodeModules)?fs.readdirSync(nodeModules,{withFileTypes:true}).filter(e=>e.name.startsWith('.')).map(e=>({name:e.name,type:e.isDirectory()?'directory':e.isSymbolicLink()?'symlink':'file',entries:e.isDirectory()?fs.readdirSync(path.join(nodeModules,e.name)).slice(0,30):undefined})):[];
 const installed={};
 for(const target of ['bun','typescript','@typescript/old','eslint','esbuild','react','vite','vitest']) {
  const p=path.join(nodeModules,target,'package.json');
  if(fs.existsSync(p)){const m=JSON.parse(fs.readFileSync(p,'utf8'));installed[target]={name:m.name,version:m.version};}
 }
 const entry={command:[bun,...args],exitCode:result.error||result.signal?null:result.status,error:result.error?.message??null,signal:result.signal,before,after,lockUnchanged:before['package-lock.json']===after['package-lock.json'],manifestUnchanged:before['package.json']===after['package.json'],nodeModulesExists:fs.existsSync(nodeModules),metadata,installed,log:name+'.log'};
 if(fs.existsSync(path.join(nodeModules,'bun/bin/bun.exe'))) {
  const version=spawnSync(path.join(nodeModules,'bun/bin/bun.exe'),['--version'],{cwd:directory,encoding:'utf8',timeout:5000});
  entry.installedBunExecutable={status:version.status,error:version.error?.message??null,output:(version.stdout??'')+(version.stderr??'')};
 }
 report.cases[name]=entry;save();
 console.log(name+': '+JSON.stringify({exitCode:entry.exitCode,error:entry.error,lockUnchanged:entry.lockUnchanged,manifestUnchanged:entry.manifestUnchanged,generatedBunLock:!!after['bun.lock']||!!after['bun.lockb'],installedBunExecutable:entry.installedBunExecutable,log:entry.log}));
 if(result.error?.code==='ETIMEDOUT'){try{process.kill(-result.pid,'SIGKILL');}catch{}throw new Error('Install timed out');}
}
report.version=spawnSync(bun,['--version'],{encoding:'utf8',timeout:5000}).stdout.trim();
runCase('original');
runCase('trusted');
runCase('drift-incompatible');
runCase('drift-range');
runCase('missing-lock',['--dry-run']);
const platform={os:process.platform,cpu:process.arch,libc:process.platform==='linux'&&process.report.getReport().header.glibcVersionRuntime?'glibc':null};
const optionalReferences=new Map();
function platformMismatch(manifest){
 if(!manifest)return false;
 for(const[key,current]of Object.entries(platform)){
  if(!current||!manifest[key])continue;
  const values=Array.isArray(manifest[key])?manifest[key]:[manifest[key]];
  const positive=values.filter(v=>!v.startsWith('!'));
  if(values.includes('!'+current)||(positive.length&&!positive.includes(current)&&!positive.includes('any')))return true;
 }
 return false;
}
function graph(directory,captureReference=false){
 const cache=new Map(),visited=new Set(),contexts=new Set(),versions=new Set(),missing=new Set(),skipped=new Set();
 const manifest=folder=>{if(!cache.has(folder))cache.set(folder,JSON.parse(fs.readFileSync(path.join(folder,'package.json'),'utf8')));return cache.get(folder);};
 function resolvePackage(from,name){let current=from;for(;;){const candidate=path.join(current,'node_modules',name);if(fs.existsSync(path.join(candidate,'package.json')))return fs.realpathSync(candidate);const parent=path.dirname(current);if(parent===current)return null;current=parent;}}
 function identity(folder){const m=manifest(folder);const peers=Object.keys(m.peerDependencies??{}).sort().map(name=>{const p=resolvePackage(folder,name);const peer=p?manifest(p):null;return name+'='+(peer?peer.name+'@'+peer.version:'missing');});return m.name+'@'+m.version+'['+peers.join(',')+']';}
 function visit(folder,isRoot=false){
  folder=fs.realpathSync(folder);if(visited.has(folder))return;visited.add(folder);
  const m=manifest(folder),id=isRoot?'<root>':identity(folder),edges=[];
  if(!isRoot)versions.add(m.name+'@'+m.version);
  const groups={dependency:{...m.dependencies,...(isRoot?m.devDependencies:{})},optional:m.optionalDependencies??{},peer:m.peerDependencies??{}};
  for(const name of Object.keys(groups.optional))delete groups.dependency[name];
  for(const[kind,dependencies]of Object.entries(groups))for(const name of Object.keys(dependencies)){
   const resolved=resolvePackage(folder,name),target=resolved?manifest(resolved):null,edge=id+'|'+kind+':'+name;
   if(captureReference&&kind==='optional'&&target)optionalReferences.set(edge,target);
   if(kind==='optional'&&platformMismatch(target??optionalReferences.get(edge))){edges.push(kind+':'+name+'=>platform-skipped');skipped.add(edge);continue;}
   edges.push(kind+':'+name+'=>'+(resolved?identity(resolved):'missing'));
   if(!resolved&&(kind==='dependency'||(kind==='peer'&&!m.peerDependenciesMeta?.[name]?.optional)))missing.add(edge);
   if(resolved)visit(resolved);
  }
  contexts.add(id+'|'+edges.sort().join('|'));
 }
 visit(directory,true);return{contexts:[...contexts].sort(),versions:[...versions].sort(),missingRequired:[...missing].sort(),skippedOptional:[...skipped].sort()};
}
const reference=graph(source,true);
report.referenceGraph={sourceState:state(source),...reference};
for(const name of ['original','trusted']){
 if(report.cases[name].exitCode!==0)continue;
 const actual=graph(path.join(base,name));
 report.cases[name].graph={...actual,versionsMatch:JSON.stringify(actual.versions)===JSON.stringify(reference.versions),contextsMatch:JSON.stringify(actual.contexts)===JSON.stringify(reference.contexts),only:actual.contexts.filter(v=>!reference.contexts.includes(v)),missing:reference.contexts.filter(v=>!actual.contexts.includes(v)),versionsOnly:actual.versions.filter(v=>!reference.versions.includes(v)),versionsMissing:reference.versions.filter(v=>!actual.versions.includes(v))};
 console.log(name+' graph: '+JSON.stringify({versions:actual.versions.length,contexts:actual.contexts.length,versionsMatch:report.cases[name].graph.versionsMatch,contextsMatch:report.cases[name].graph.contextsMatch,missingRequired:actual.missingRequired.length}));
}
save();
