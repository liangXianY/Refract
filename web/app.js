/* Refract 前端：三个板块共用一套任务执行，按意图分流。
   会话走意图识别 → 派发子 Agent；文章/视频是显式入口，直达对应 Agent。 */

const $ = (id) => document.getElementById(id);

/* ============================ 通用 ============================ */

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function inlineMd(s) {
  return esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`(.+?)`/g, '<code>$1</code>');
}

/** 够用的 Markdown 渲染，流式每帧全量重排，所以要快 */
function renderMd(text, withCursor) {
  const out = [];
  let inList = false;
  for (const rawLine of String(text).split('\n')) {
    const line = rawLine.replace(/\s+$/, '');
    if (!line.trim()) { if (inList) { out.push('</ul>'); inList = false; } continue; }
    if (line.startsWith('### ')) { if (inList) { out.push('</ul>'); inList = false; } out.push(`<h4>${inlineMd(line.slice(4))}</h4>`); }
    else if (line.startsWith('## ')) { if (inList) { out.push('</ul>'); inList = false; } out.push(`<h3>${inlineMd(line.slice(3))}</h3>`); }
    else if (line.startsWith('# ')) { if (inList) { out.push('</ul>'); inList = false; } out.push(`<h2>${inlineMd(line.slice(2))}</h2>`); }
    else if (line.startsWith('> ')) out.push(`<blockquote>${inlineMd(line.slice(2))}</blockquote>`);
    else if (/^\s*[-*]\s+/.test(line)) { if (!inList) { out.push('<ul>'); inList = true; } out.push(`<li>${inlineMd(line.replace(/^\s*[-*]\s+/, ''))}</li>`); }
    else { if (inList) { out.push('</ul>'); inList = false; } out.push(`<p>${inlineMd(line)}</p>`); }
  }
  if (inList) out.push('</ul>');
  if (withCursor) out.push('<span class="cursor"></span>');
  return out.join('\n');
}

/** 起任务 + 读 SSE。onEvent 收到每一个事件。 */
async function runTask(payload, onEvent, path = '/api/generate') {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch { /* 保持原样 */ }
    throw new Error(detail);
  }
  const { task_id } = await resp.json();

  return new Promise((resolve, reject) => {
    const es = new EventSource(`/api/tasks/${task_id}/events`);
    es.onmessage = (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      onEvent?.(ev);
      if (ev.type === 'done') { es.close(); resolve(ev); }
      if (ev.type === 'error') { es.close(); reject(new Error(ev.message)); }
    };
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) reject(new Error('连接中断'));
    };
  });
}

/* ============================ 模式切换 ============================ */

const MODES = ['chat', 'article', 'video'];

function switchMode(mode) {
  MODES.forEach(m => {
    $(`mode${m[0].toUpperCase()}${m.slice(1)}`).classList.toggle('on', m === mode);
  });
  document.querySelectorAll('#modes button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  if (mode === 'chat') setTimeout(() => $('chatInput').focus(), 60);
}

document.querySelectorAll('#modes button').forEach(btn => {
  btn.onclick = () => switchMode(btn.dataset.mode);
});

/* ============================ 会话 ============================ */

const chatlog = $('chatlog');

function addMsg(role, html) {
  $('chatempty')?.remove();
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  el.innerHTML = `<div class="av">${role === 'user' ? '你' : 'R'}</div><div class="body">${html}</div>`;
  chatlog.appendChild(el);
  chatlog.scrollTop = chatlog.scrollHeight;
  return el.querySelector('.body');
}

/** 意图识别。
 *  现在是关键词规则——够用，但要诚实：真正的意图识别应该走模型。
 *  单独抽一层是因为它决定「派给哪个子 Agent」，是路由核心。 */
function detectIntent(text) {
  const t = text.toLowerCase();
  if (/(视频|短片|动画|分镜|镜头|video)/.test(t)) return { agent: 'Director', label: '视频生成', style: 'auto' };
  if (/(链接|网址|https?:\/\/|抓取|爬)/.test(t)) return { agent: 'Researcher', label: '链接解析', style: null };
  if (/(小红书|xhs)/.test(t)) return { agent: 'Writer', label: '文章生成 · 小红书', platform: 'xiaohongshu' };
  return { agent: 'Writer', label: '文章生成 · 公众号', platform: 'wechat' };
}

let chatting = false;

async function sendChat(text) {
  text = (text || '').trim();
  if (!text || chatting) return;
  chatting = true;
  $('chatSend').disabled = true;
  $('chatInput').value = '';
  autosize($('chatInput'));

  addMsg('user', esc(text).replace(/\n/g, '<br/>'));

  const intent = detectIntent(text);
  const body = addMsg('ai', `<div class="intent"><i></i>意图：${intent.label} · 派给 ${intent.agent}</div>
    <div class="md" data-md style="font-size:13.5px"><p class="ph">正在拆解…</p></div>`);
  const mdBox = body.querySelector('[data-md]');

  if (intent.agent === 'Researcher') {
    mdBox.innerHTML = '<p>链接解析还没接上（P1 剩余部分）。当前只接受文本输入，抓取层需要 <code>trafilatura</code> + 无头浏览器截图，会单独做。</p>';
    $('chatHint').textContent = 'Researcher · 未实现';
    chatting = false;
    $('chatSend').disabled = false;
    return;
  }

  let raw = '';
  let pending = false;
  const flush = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      mdBox.innerHTML = renderMd(raw, true);
      chatlog.scrollTop = chatlog.scrollHeight;
    });
  };

  try {
    $('chatHint').textContent = `${intent.agent} 执行中…`;
    const done = await runTask(
      { input: text, platform: intent.platform || 'wechat', style: intent.style || 'auto', words: 'medium' },
      (ev) => {
        if (ev.type === 'delta' && ev.kind === 'content') { raw += ev.text; flush(); }
        if (ev.type === 'stage') $('chatHint').textContent = `${intent.agent} · ${ev.label}`;
        if (ev.type === 'reflection') $('chatHint').textContent = `${intent.agent} · 第 ${ev.reflection.round} 轮 ${ev.reflection.total} 分`;
      }
    );

    mdBox.innerHTML = renderMd(raw, false);

    // 产物卡：让"生成完了"变成可点的东西，而不是一段死文字
    const art = document.createElement('div');
    art.className = 'artifact';
    art.innerHTML = `<div class="ai-ico">${intent.style ? '▶' : '文'}</div>
      <div class="ai-tx"><b>${intent.style ? '动画成品' : '文章初稿'}</b>
        <span>${done.result.word_count} 字 · ${done.result.rounds.length} 轮 · ${(done.result.elapsed_ms / 1000).toFixed(1)}s</span></div>
      <a href="${done.url}" target="_blank" rel="noopener">打开</a>`;
    body.appendChild(art);
    chatlog.scrollTop = chatlog.scrollHeight;

    $('chatCost').textContent = `${done.result.word_count} 字 · ${done.result.model}`;
    $('chatHint').textContent = '意图识别 → 子 Agent 调度';
  } catch (e) {
    mdBox.innerHTML = `<p style="color:var(--err)">${esc(e.message)}</p>`;
    $('chatHint').textContent = '出错了';
  } finally {
    chatting = false;
    $('chatSend').disabled = false;
  }
}

function autosize(el) {
  el.style.height = '24px';
  el.style.height = Math.min(120, el.scrollHeight) + 'px';
}

$('chatInput').addEventListener('input', (e) => autosize(e.target));
$('chatInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(e.target.value); }
});
$('chatSend').onclick = () => sendChat($('chatInput').value);
document.querySelectorAll('.ce-tips button').forEach(b => {
  b.onclick = () => { $('chatInput').value = b.dataset.tip; autosize($('chatInput')); $('chatInput').focus(); };
});

/* ============================ 文章 ============================ */

let artPlatform = 'wechat', artWords = 'medium', artBusy = false;

document.querySelectorAll('#artPlatform button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('#artPlatform button').forEach(x => x.classList.remove('on'));
    b.classList.add('on'); artPlatform = b.dataset.p;
  };
});
document.querySelectorAll('#artWords button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('#artWords button').forEach(x => x.classList.remove('on'));
    b.classList.add('on'); artWords = b.dataset.w;
  };
});
document.querySelectorAll('#modeArticle .tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('#modeArticle .tab').forEach(t => t.classList.remove('on'));
    tab.classList.add('on');
    document.querySelectorAll('#modeArticle .view').forEach(v => v.classList.remove('on'));
    $('view' + tab.dataset.v.charAt(0).toUpperCase() + tab.dataset.v.slice(1)).classList.add('on');
  };
});

const STAGE_ORDER = ['plan', 'compose', 'reflect', 'render'];

function setStages(name) {
  const i = STAGE_ORDER.indexOf(name);
  document.querySelectorAll('#artStages .stage').forEach((el, k) => {
    el.classList.toggle('done', k < i);
    el.classList.toggle('on', k === i);
  });
}

async function genArticle() {
  const text = $('artInput').value.trim();
  if (text.length < 2 || artBusy) return;
  artBusy = true;
  $('artGo').disabled = true;
  $('artGo').textContent = '生成中…';

  $('artEmpty').hidden = true;
  $('artThink').hidden = false;
  $('artBrief').classList.remove('on');
  $('artBrief').innerHTML = '';
  $('artMd').innerHTML = '<p class="ph">正在拆解输入…</p>';
  $('reflectList').innerHTML = '<p class="ph">每次执行后，反思会给出分维度评分、问题清单和修复动作。</p>';
  $('rCount').textContent = '0';
  document.querySelectorAll('#artStages .stage').forEach(el => { el.className = 'stage'; });

  let raw = '', pending = false, rn = 0;
  const flush = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      $('artMd').innerHTML = renderMd(raw, true);
    });
  };

  try {
    const done = await runTask(
      { input: text, platform: artPlatform, style: 'auto', words: artWords },
      (ev) => {
        if (ev.type === 'stage') { setStages(ev.stage); $('artThinkText').textContent = ev.label || ''; }
        if (ev.type === 'brief') renderBrief(ev.brief);
        if (ev.type === 'round_start') {
          raw = '';
          if (ev.round > 1) $('artMd').innerHTML = '<p class="ph">按上一轮问题重写…</p>';
        }
        if (ev.type === 'delta' && ev.kind === 'content') { raw += ev.text; flush(); }
        if (ev.type === 'reflection') { addReflection(ev.reflection); $('rCount').textContent = String(++rn); }
      }
    );
    $('artMd').innerHTML = renderMd(raw, false);
    document.querySelectorAll('#artStages .stage').forEach(el => { el.classList.remove('on'); el.classList.add('done'); });
    $('artThinkText').textContent = `完成 · ${done.result.word_count} 字 · ${(done.result.elapsed_ms / 1000).toFixed(1)}s`;
    $('cost').textContent = `${done.result.word_count} 字 · ${done.result.model}`;
    $('artGo').textContent = '再生成一篇';
  } catch (e) {
    $('artMd').innerHTML = `<p style="color:var(--err)">${esc(e.message)}</p>`;
    $('artThinkText').textContent = '失败：' + e.message;
    $('artGo').textContent = '重试';
  } finally {
    artBusy = false;
    $('artGo').disabled = false;
  }
}

function renderBrief(b) {
  const tags = [b.platform, b.audience, `${b.word_limit} 字`, b.tone]
    .filter(Boolean).map(t => `<span class="tag">${esc(String(t))}</span>`).join('');
  const ol = (b.outline || []).map(s => `<li>${esc(s.heading)}</li>`).join('');
  const facts = (b.facts || []).map(f =>
    `<div class="fact ${f.source ? '' : 'warn'}">${f.source ? '✓' : '⚠'} ${esc(f.claim)}${f.source ? '（' + esc(f.source) + '）' : '（无出处）'}</div>`).join('');
  $('artBrief').innerHTML = `<div class="bt">${esc(b.topic)}</div>
    <div class="bd">${esc(b.angle || '')}</div>
    <div class="tags">${tags}</div><ol>${ol}</ol>${facts}`;
  $('artBrief').classList.add('on');
}

function addReflection(r) {
  const scores = Object.entries(r.scores || {}).map(([k, v]) => `
    <div class="srow"><span>${esc(k)}</span>
      <span class="sbar"><i style="--w:${Math.max(0, Math.min(100, v))}%"></i></span>
      <span class="sval">${v}</span></div>`).join('');
  const issues = (r.issues || []).map(i => `<div>${esc(i)}</div>`).join('');
  const el = document.createElement('div');
  el.className = 'rcard';
  el.innerHTML = `<div class="rh"><b>第 ${r.round} 轮</b>
      <span class="act ${r.repair_action}">${r.repair_action.toUpperCase()}</span></div>
    ${scores}${issues ? `<div class="iss">${issues}</div>` : ''}
    <div class="rtotal">综合 ${r.total} / 100</div>`;
  const list = $('reflectList');
  if (list.querySelector('.ph')) list.innerHTML = '';
  list.prepend(el);
}

$('artGo').onclick = genArticle;
$('artInput').addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') genArticle();
});

/* ============================ 视频 ============================ */

/* 流程按 xilo-opus-video 的方法论：
   先出三套方案 → 人选 → 再让视频模型写整份动画。
   一步直达的问题不是慢，是选错方向后整份渲染代码全废。 */

let vidBusy = false;
let vidPlans = [];
let vidBrief = null;

async function loadModels() {
  try {
    const m = await (await fetch('/api/models')).json();
    const v = m.routes.video;
    const active = v.active;
    $('vidModel').innerHTML = active
      ? `<b>${esc(active.label)}</b><span>${esc(active.model)}</span>
         ${v.fallback ? '<i class="warn">（首选未配凭据，已降级）</i>' : ''}`
      : '<span class="ph">视频模型未配置，将用模板渲染</span>';
  } catch {
    $('vidModel').innerHTML = '<span class="ph">读取失败</span>';
  }
}

async function makePlans() {
  const text = $('vidInput').value.trim();
  const url = $('vidUrl').value.trim();
  if ((text.length < 2 && !url) || vidBusy) return;

  vidBusy = true;
  $('vidPlan').disabled = true;
  $('vidPlan').textContent = '正在想方案…';
  $('vidHint').textContent = '分析内容特征，给出三套不同的拍法';
  $('planList').innerHTML = '<p class="ph">生成中…</p>';
  $('playerBox').hidden = true;

  try {
    const resp = await fetch('/api/video/plans', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: text, url, platform: 'video' }),
    });
    if (!resp.ok) {
      let d = resp.statusText;
      try { d = (await resp.json()).detail || d; } catch { /* noop */ }
      throw new Error(d);
    }
    const data = await resp.json();
    vidPlans = data.plans;
    vidBrief = data.brief;
    renderPlans(data.plans, data.brief);
    $('vidHint').textContent = `三套方案 · ${data.model}${data.mocked ? ' · mock' : ''}`;
  } catch (e) {
    $('planList').innerHTML = `<p class="ph" style="color:var(--err)">${esc(e.message)}</p>`;
    $('vidHint').textContent = '失败：' + e.message;
  } finally {
    vidBusy = false;
    $('vidPlan').disabled = false;
    $('vidPlan').textContent = '重新出方案';
  }
}

function renderPlans(plans, brief) {
  const head = `
    <div class="planhead">
      <b>${esc(brief.topic)}</b>
      <span>${esc(brief.audience)} · 钩子：${esc(brief.hook || '—')}</span>
    </div>`;

  const cards = plans.map((p, i) => `
    <article class="plan" data-i="${i}">
      <header>
        <span class="pid">${esc(p.id)}</span>
        <h4>${esc(p.name)}</h4>
        <span class="ptag">${esc(p.style)} · ${p.duration}s</span>
      </header>
      <p class="pc">${esc(p.concept)}</p>
      <p class="pw"><em>为什么</em>${esc(p.why)}</p>
      <p class="pm"><em>运动</em>${esc(p.motion)}</p>
      <ol class="pbeats">${(p.beats || []).map(b => `<li>${esc(b)}</li>`).join('')}</ol>
      <div class="pfoot">
        <div class="swatches">${(p.palette || []).map(c => `<i style="background:${esc(c)}"></i>`).join('')}</div>
        <button class="go" data-i="${i}">用这套拍</button>
      </div>
    </article>`).join('');

  $('planList').innerHTML = head + `<div class="plangrid">${cards}</div>`;
  document.querySelectorAll('.plan .go').forEach(b => {
    b.onclick = () => renderVideo(+b.dataset.i);
  });
}

async function renderVideo(idx) {
  if (vidBusy) return;
  const plan = vidPlans[idx];
  if (!plan) return;

  vidBusy = true;
  document.querySelectorAll('.plan .go').forEach(b => { b.disabled = true; });
  $('planList').hidden = true;
  $('playerBox').hidden = false;
  $('playerBox').innerHTML = `
    <div class="pstage"><div class="spin"></div>
      <p>正在按「${esc(plan.name)}」写动画…</p>
      <span id="pstageTx">视频模型逐帧代码生成，通常 40-90 秒</span></div>`;
  $('vidHint').textContent = '生成中…';

  try {
    const done = await runTask(
      { input: $('vidInput').value.trim(), url: $('vidUrl').value.trim(), platform: 'video', plan },
      (ev) => {
        if (ev.type === 'stage') {
          const tx = document.getElementById('pstageTx');
          if (tx) tx.textContent = ev.label + (ev.model ? ` · ${ev.model}` : '');
        }
        if (ev.type === 'gate') {
          const tx = document.getElementById('pstageTx');
          if (tx) tx.textContent = ev.ok
            ? `闸门通过 · 第 ${ev.attempts} 版 · 覆盖 ${Math.round((ev.stats.phrase_cover || 0) * 100)}%`
            : `未过闸门：${(ev.failures || [])[0] || ''}`;
        }
      },
      '/api/video/render'
    );

    const d = done.degraded;
    $('playerBox').innerHTML = `
      <div class="prow">
        <a href="${done.url}" target="_blank" rel="noopener">新窗口打开</a>
        <span class="meta">${esc(plan.name)} · ${esc(done.result.model || '')}${d ? ' · 降级模板' : ''}</span>
        <button class="ghost" id="pBack">换一套</button>
      </div>
      <iframe src="${done.url}" title="视频成品"></iframe>`;
    document.getElementById('pBack').onclick = () => {
      $('playerBox').hidden = true;
      $('planList').hidden = false;
      document.querySelectorAll('.plan .go').forEach(b => { b.disabled = false; });
    };
    $('vidHint').textContent = d ? '模型产物没过闸门，已降级模板' : '完成 · 可直接播放';
  } catch (e) {
    $('playerBox').innerHTML = `<p class="ph" style="color:var(--err)">${esc(e.message)}</p>`;
    $('vidHint').textContent = '失败：' + e.message;
    $('planList').hidden = false;
    document.querySelectorAll('.plan .go').forEach(b => { b.disabled = false; });
  } finally {
    vidBusy = false;
  }
}

$('vidPlan').onclick = makePlans;
$('vidInput').addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') makePlans();
});

/* ============================ 启动 ============================ */

(async function boot() {
  try {
    const h = await (await fetch('/api/health')).json();
    $('modeDot').classList.add(h.mock ? 'mock' : 'live');
    $('modeText').textContent = h.mock ? 'MOCK 模式' : h.model;
  } catch { $('modeText').textContent = '离线'; }

  const QS = new URLSearchParams(location.search);

  // ?demo=1 自动跑一篇文章，用于截图自检
  if (QS.get('demo') === '1') {
    switchMode('article');
    $('artInput').value = '从 GitHub 热榜挑一个项目，写篇公众号介绍它的架构';
    setTimeout(genArticle, 500);
  }
  if (QS.has('mode')) switchMode(QS.get('mode'));
  loadModels();
})();
