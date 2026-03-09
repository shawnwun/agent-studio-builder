/**
 * PolyAI Agent Builder v2 — Node proxy server
 */
const express = require('express');
const https   = require('https');
const path    = require('path');
const fs      = require('fs');
const os      = require('os');
const { spawn } = require('child_process');

const app  = express();
const PORT = parseInt(process.env.PORT || '8081');

const AUTH0 = {
  domains:   { 'uk-1': 'login.uk-1.polyai.app', 'us-1': 'login.us-1.polyai.app', 'eu-1': 'login.eu-1.polyai.app' },
  clientIds: { 'uk-1': 'uHdlq2JZZoZ3RAzYDvl0o0R2E3glxj1q', 'us-1': 'kjV5BoAXagNnK6aGiUnJQ6xJ3hbphwE2', 'eu-1': 'kjV5BoAXagNnK6aGiUnJQ6xJ3hbphwE2' },
  audience:  'https://platform.polyai.app/api',
};
const PTB_HOST     = process.env.PTB_HOST    || 'localhost:8788';
const PTB_API_KEY  = process.env.PTB_API_KEY || 'ptb-dev-changeme';
const LAS_BIN      = process.env.LAS_BIN     || '/Users/shawnwen/local_agent_studio/.venv/bin/las';
const PROJECTS_DIR = path.join(os.homedir(), 'las-projects-v2');
if (!fs.existsSync(PROJECTS_DIR)) fs.mkdirSync(PROJECTS_DIR, { recursive: true });

app.use(express.json({ limit: '2mb' }));
app.use(express.static(path.join(__dirname, 'public')));

function httpsPost(domain, endpoint, body) {
  return new Promise((resolve, reject) => {
    const payload = new URLSearchParams(body).toString();
    const req = https.request({ hostname: domain, path: endpoint, method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(payload) }
    }, res => { let d=''; res.on('data',c=>d+=c); res.on('end',()=>{ try{resolve(JSON.parse(d));}catch(e){reject(e);} }); });
    req.on('error', reject); req.write(payload); req.end();
  });
}

function writePolyctxToken(token, region) {
  const dir = path.join(os.homedir(), '.polyctx');
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  const future = new Date(Date.now() + 14*60*1000).toISOString();
  fs.writeFileSync(path.join(dir, `token-platform-${region}`),
    JSON.stringify({ access_token: token, refresh_token:'', id_token:'', token_type:'Bearer', expires_in:900, expires_at:future }));
}

function runLas(args, cwd, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(LAS_BIN, args, { cwd, env, stdio:['pipe','pipe','pipe'] });
    child.stdin.end(); let out='', err='';
    child.stdout.on('data', d => { out+=d; console.log('[las]', d.toString().trim()); });
    child.stderr.on('data', d => err+=d);
    const t = setTimeout(()=>{ child.kill(); reject(new Error('las timed out')); }, 90000);
    child.on('close', code => { clearTimeout(t); code===0 ? resolve(out) : reject(new Error((err||out).trim().slice(0,500))); });
  });
}

function ptbFetch(p, opts={}) {
  const proto = (PTB_HOST.startsWith('localhost')||PTB_HOST.startsWith('127')) ? 'http' : 'https';
  return fetch(`${proto}://${PTB_HOST}${p}`, {
    ...opts, headers: { 'Authorization':`Bearer ${PTB_API_KEY}`, 'Content-Type':'application/json', ...(opts.headers||{}) }
  });
}

function buildHistory(messages) {
  const userMsgs = messages.filter(m => m.role==='user');
  const prompt = userMsgs.length ? userMsgs[userMsgs.length-1].content||'' : '';
  const history = [];
  let i=0;
  while (i < messages.length) {
    if (messages[i].role==='user' && i < messages.length-1) {
      let asst=[]; let j=i+1;
      while (j < messages.length && messages[j].role!=='user') {
        if (messages[j].role==='assistant' && messages[j].content) asst.push(messages[j].content);
        j++;
      }
      if (j < messages.length) history.push({ user: messages[i].content||'', assistant: asst.join('\n') });
      i=j;
    } else { i++; }
  }
  return { prompt, history };
}

// ── Auth ───────────────────────────────────────────────────────────────────
app.post('/api/auth/start', async (req,res) => {
  const {region='uk-1'} = req.body;
  try { res.json(await httpsPost(AUTH0.domains[region]||AUTH0.domains['uk-1'], '/oauth/device/code',
    { client_id: AUTH0.clientIds[region]||AUTH0.clientIds['uk-1'], scope:'offline_access', audience:AUTH0.audience }));
  } catch(e) { res.status(500).json({error:e.message}); }
});

app.post('/api/auth/poll', async (req,res) => {
  const {device_code, region='uk-1'} = req.body;
  try { res.json(await httpsPost(AUTH0.domains[region]||AUTH0.domains['uk-1'], '/oauth/token',
    { client_id: AUTH0.clientIds[region]||AUTH0.clientIds['uk-1'], device_code, grant_type:'urn:ietf:params:oauth:grant-type:device_code' }));
  } catch(e) { res.status(500).json({error:e.message}); }
});

app.post('/api/auth/refresh', async (req,res) => {
  const {refresh_token, region='uk-1'} = req.body;
  try { res.json(await httpsPost(AUTH0.domains[region]||AUTH0.domains['uk-1'], '/oauth/token',
    { client_id: AUTH0.clientIds[region]||AUTH0.clientIds['uk-1'], grant_type:'refresh_token', refresh_token }));
  } catch(e) { res.status(500).json({error:e.message}); }
});

// ── Project setup: writes token + las init + las pull (REQ 2+3) ───────────
app.post('/api/project/setup', async (req,res) => {
  const {accountId, projectId, region, token} = req.body;
  if (!accountId||!projectId||!region||!token) return res.status(400).json({error:'Missing fields'});
  const projectDir = path.join(PROJECTS_DIR, accountId, projectId);
  writePolyctxToken(token, region);
  const env = { ...process.env, POLYCTX_TOKEN: token, TERM:'dumb',
    PATH:`/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/Users/shawnwen/.local/bin:/Users/shawnwen/local_agent_studio/.venv/bin:${process.env.PATH||''}`,
    HOME: os.homedir() };
  try {
    fs.mkdirSync(projectDir, { recursive:true });
    const cfgPath = path.join(projectDir,'_gen','.agent_studio_config');
    let needsInit = true;
    if (fs.existsSync(cfgPath)) {
      try {
        const cfg = JSON.parse(Buffer.from(fs.readFileSync(cfgPath,'utf8'),'base64').toString());
        if (cfg.region===region && cfg.account_id===accountId && cfg.project_id===projectId) needsInit=false;
        else { fs.rmSync(projectDir,{recursive:true,force:true}); fs.mkdirSync(projectDir,{recursive:true}); }
      } catch(_) {}
    }
    if (needsInit) {
      console.log(`[setup] las init ${accountId}/${projectId} (${region})`);
      await runLas(['init','--region',region,'--account_id',accountId,'--project_id',projectId], PROJECTS_DIR, env);
    }
    console.log(`[setup] las pull ${projectDir}`);
    await runLas(['pull','--force'], projectDir, env); // REQ 3: always pull on session start
    res.json({ ok:true, projectPath:projectDir });
  } catch(e) {
    let msg = e.message;
    if (msg.includes('DEPLOYMENT_NOT_FOUND')||msg.includes('Deployment not found'))
      msg=`Project "${projectId}" not found in Agent Studio (${region}). Create it at https://studio.polyai.app first.`;
    res.status(400).json({error:msg});
  }
});

// ── Build: fire-and-forget → PTB (REQ 4: PTB handles las push + reauth) ──
app.post('/api/build', async (req,res) => {
  try {
    const {messages=[],region,accountId,projectId,projectPath,polyctxToken} = req.body;
    if (!accountId||!projectId||!region) return res.status(400).json({error:'Missing project fields'});

    // Guard: query PTB directly for any running job — survives Node restarts
    try {
      const jobsRes = await ptbFetch('/jobs');
      const jobs = await jobsRes.json();
      const running = Array.isArray(jobs) && jobs.find(j => j.status === 'running');
      if (running) {
        console.warn(`[build] REJECTED — already have running job ${running.job_id}`);
        return res.status(409).json({error:`A build is already running. Cancel it before starting a new one.`, job_id: running.job_id});
      }
    } catch(e) { console.warn('[build] could not check running jobs:', e.message); }

    const {prompt,history} = buildHistory(messages);
    console.log(`[build] ${accountId}/${projectId} (${region}) prompt="${prompt.slice(0,60)}"`);
    const r = await ptbFetch('/build', { method:'POST',
      body: JSON.stringify({account_id:accountId,project_id:projectId,region,prompt,history,
        polyctx_token:polyctxToken||null,project_path:projectPath||null}) });
    const text = await r.text();
    if (!r.ok) return res.status(r.status).json({error:text});
    const job_id = JSON.parse(text).job_id;
    console.log(`[build] started job ${job_id} for ${accountId}/${projectId}`);
    res.json({ job_id });
  } catch(e) { res.status(500).json({error:`PTB unreachable: ${e.message}`}); }
});

// ── Poll ───────────────────────────────────────────────────────────────────
app.get('/api/build/poll/:jobId', async (req,res) => {
  const offset = parseInt(req.query.offset||'0');
  try {
    const r = await ptbFetch(`/jobs/${req.params.jobId}/poll?offset=${offset}`);
    res.json(await r.json());
  } catch(e) { res.json({events:[],offset,done:false,status:'error',error:e.message}); }
});

// ── Cancel build (REQ 6) ──────────────────────────────────────────────────
app.post('/api/build/cancel/:jobId', async (req,res) => {
  const jobId = req.params.jobId;
  console.log(`[build] cancelling job ${jobId}`);
  try { const r = await ptbFetch(`/jobs/${jobId}/cancel`,{method:'POST'}); res.json(await r.json()); }
  catch(e) { res.json({ok:true}); }
});

// ── Job done callback (no-op now — PTB is source of truth) ────────────────
app.post('/api/build/done/:jobId', async (req,res) => { res.json({ok:true}); });

// ── Reauth SSE (REQ 4: auth failure → browser popup) ─────────────────────
const reauthClients = new Set();
app.get('/api/reauth/events', (req,res) => {
  res.setHeader('Content-Type','text/event-stream'); res.setHeader('Cache-Control','no-cache');
  res.setHeader('Connection','keep-alive'); res.setHeader('X-Accel-Buffering','no');
  if (res.socket) res.socket.setNoDelay(true); res.flushHeaders();
  reauthClients.add(res);
  const ping = setInterval(()=>{ if(!res.writableEnded) res.write(': ping\n\n'); },25000);
  req.on('close',()=>{ reauthClients.delete(res); clearInterval(ping); });
});
app.post('/api/reauth/notify', (req,res) => {
  const payload = JSON.stringify(req.body||{});
  reauthClients.forEach(c=>{ try{c.write(`data: ${payload}\n\n`);}catch(_){} });
  res.json({ok:true});
});

// ── Reauth: UI posts fresh token here after re-auth ──────────────────────
app.post('/api/reauth/token', (req,res) => {
  const {token, region} = req.body;
  if (!token || !region) return res.status(400).json({error:'Missing token or region'});
  try {
    writePolyctxToken(token, region);
    console.log(`[reauth] fresh token written for region ${region}`);
    res.json({ok:true});
  } catch(e) { res.status(500).json({error:e.message}); }
});

app.get('/api/health', (_,res) => res.json({ok:true}));

app.listen(PORT, () => {
  console.log(`\nPolyAI Agent Builder v2  →  http://localhost:${PORT}`);
  console.log(`PTB: http://${PTB_HOST}  key: ${PTB_API_KEY.slice(0,8)}...\n`);
});
