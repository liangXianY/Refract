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

/* 对话不再由前端猜意图——主 Agent（Supervisor）在后端规划。
   前端只负责把它的每一步亮出来：计划卡 → 工具卡 → 流式答复。 */

let chatting = false;

function toolCard(flow, title, tool) {
  const el = document.createElement('div');
  el.className = 'tcard';
  el.innerHTML = `<span class="tstat run"></span><span class="ttitle">${esc(title)}</span>
    <span class="ttool">${esc(tool)}</span><span class="ttime"></span>
    <div class="tdetail"></div>`;
  flow.appendChild(el);
  chatlog.scrollTop = chatlog.scrollHeight;
  return el;
}

function miniLine(flow, label, text, cls = '') {
  const el = document.createElement('div');
  el.className = `mini ${cls}`;
  el.innerHTML = `<b>${esc(label)}</b><span>${esc(text)}</span>`;
  flow.appendChild(el);
  chatlog.scrollTop = chatlog.scrollHeight;
  return el;
}

function planCard(flow, steps) {
  const el = document.createElement('div');
  el.className = 'plancard';
  el.innerHTML = `<div class="pc-h">执行计划 · ${steps.length} 步</div>` +
    steps.map((s, i) => `<div class="pc-s" data-i="${i}"><b>${i + 1}</b><span>${esc(s.title || s.tool)}</span><em>${esc(s.why || '')}</em></div>`).join('');
  flow.appendChild(el);
  chatlog.scrollTop = chatlog.scrollHeight;
  return el;
}

function plansCards(flow, plans) {
  const wrap = document.createElement('div');
  wrap.className = 'chatplans';
  wrap.innerHTML = plans.map((p, i) => `
    <div class="cplan">
      <header><b>${esc(p.id)} · ${esc(p.name)}</b><span>${esc(p.style)} · ${p.duration}s</span></header>
      <p>${esc(p.concept)}</p>
      <button class="go" data-i="${i}">用这套拍</button>
    </div>`).join('');
  flow.appendChild(wrap);
  wrap.querySelectorAll('.go').forEach(btn => {
    btn.onclick = () => playInChat(flow, plans[+btn.dataset.i], btn);
  });
  chatlog.scrollTop = chatlog.scrollHeight;
}

async function playInChat(flow, plan, btn) {
  btn.disabled = true;
  btn.textContent = '生成中…';
  const card = toolCard(flow, `按「${plan.name}」生成动画`, 'video_render');
  try {
    const done = await runTask(
      { input: $('chatInput').value.trim() || plan.concept, platform: 'video', plan },
      (ev) => {
        if (ev.type === 'stage') card.querySelector('.ttitle').textContent = ev.label + (ev.model ? ` · ${ev.model}` : '');
        if (ev.type === 'gate') card.querySelector('.ttime').textContent = ev.ok ? `闸门过·第${ev.attempts}版` : '未过闸';
      },
      '/api/video/render'
    );
    card.querySelector('.tstat').className = 'tstat ok';
    card.querySelector('.ttime').textContent = done.degraded ? '降级模板' : '完成';
    const player = document.createElement('div');
    player.className = 'chatplayer';
    player.innerHTML = `<a href="${done.url}" target="_blank" rel="noopener">新窗口打开</a>
      ${done.degraded ? '<span class="degrade">模型没过闸门，降级了模板片</span>' : ''}<iframe src="${done.url}" title="视频成品"></iframe>`;
    flow.appendChild(player);
    chatlog.scrollTop = chatlog.scrollHeight;
  } catch (e) {
    card.querySelector('.tstat').className = 'tstat err';
    card.querySelector('.tdetail').textContent = e.message;
  }
}

async function sendChat(text) {
  text = (text || '').trim();
  if (!text || chatting) return;
  chatting = true;
  $('chatSend').disabled = true;
  $('chatInput').value = '';
  autosize($('chatInput'));

  addMsg('user', esc(text).replace(/\n/g, '<br/>'));

  const body = addMsg('ai', `<div class="flow" data-flow></div>
    <div class="md" data-md style="font-size:13.5px" hidden></div>`);
  const flow = body.querySelector('[data-flow]');
  const mdBox = body.querySelector('[data-md]');

  let raw = '';
  let pending = false;
  const flush = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      mdBox.hidden = false;
      mdBox.innerHTML = renderMd(raw, true);
      chatlog.scrollTop = chatlog.scrollHeight;
    });
  };

  let activeCard = null;
  const cardFor = (i, tool, title) => {
    if (activeCard && +activeCard.dataset.i === i) return activeCard;
    activeCard = toolCard(flow, title || tool, tool);
    activeCard.dataset.i = i;
    return activeCard;
  };

  try {
    $('chatHint').textContent = '主 Agent 规划中…';
    await runTask(
      { input: text },
      (ev) => {
        switch (ev.type) {
          case 'plan':
            if (ev.steps.length) planCard(flow, ev.steps);
            $('chatHint').textContent = `执行 ${ev.steps.length} 步`;
            break;
          case 'step_start':
            activeCard = toolCard(flow, ev.title, ev.tool);
            activeCard.dataset.i = ev.i;
            $('chatHint').textContent = `${ev.title}…`;
            break;
          case 'tool_detail': {
            const box = (activeCard || flow.lastElementChild);
            const det = box?.querySelector('.tdetail');
            if (det) det.innerHTML = (ev.lines || []).map(l => `<div>${esc(l)}</div>`).join('');
            chatlog.scrollTop = chatlog.scrollHeight;
            break;
          }
          case 'step_end': {
            const card = flow.querySelector(`.tcard[data-i="${ev.i}"]`) || activeCard;
            if (card) {
              card.querySelector('.tstat').className = `tstat ${ev.status === 'ok' ? 'ok' : 'err'}`;
              card.querySelector('.ttime').textContent = ev.status === 'ok'
                ? `${((ev.elapsed_ms || 0) / 1000).toFixed(1)}s`
                : '失败';
              if (ev.summary) {
                const det = card.querySelector('.tdetail');
                const line = document.createElement('div');
                line.textContent = ev.summary;
                det.appendChild(line);
              }
            }
            break;
          }
          case 'stage':
            // write_article 内部的阶段：plan/compose/reflect/render
            miniLine(flow, ev.stage.toUpperCase(), ev.label, ev.stage === 'reflect' ? 'hl' : '');
            $('chatHint').textContent = ev.label;
            break;
          case 'reflection':
            miniLine(flow, `第 ${ev.reflection.round} 轮自检`, `${ev.reflection.total} 分 · ${ev.reflection.repair_action.toUpperCase()}`, 'hl');
            $('chatHint').textContent = `第 ${ev.reflection.round} 轮 ${ev.reflection.total} 分`;
            break;
          case 'delta':
            if (ev.kind === 'content') { raw += ev.text; flush(); }
            break;
          case 'artifact':
            if (ev.artifact === 'article') {
              const art = document.createElement('div');
              art.className = 'artifact';
              art.innerHTML = `<div class="ai-ico">文</div>
                <div class="ai-tx"><b>文章产物页</b><span>${esc(ev.summary || '')}</span></div>
                <div class="ai-links"><a href="${ev.url}" target="_blank" rel="noopener">打开</a>
                <a href="/api/articles/${ev.url.split('/').pop()}/md" download>下载 .md</a></div>`;
              flow.appendChild(art);
              chatlog.scrollTop = chatlog.scrollHeight;
            }
            break;
          case 'video_plans':
            plansCards(flow, ev.plans || []);
            $('chatHint').textContent = '挑一套拍法';
            break;
          case 'done_reply':
            break;
          case 'error':
            mdBox.hidden = false;
            mdBox.innerHTML = `<p style="color:var(--err)">${esc(ev.message)}</p>`;
            break;
        }
      },
      '/api/chat'
    );
    if (raw) { mdBox.hidden = false; mdBox.innerHTML = renderMd(raw, false); }
    if (!flow.children.length && !raw) {
      mdBox.hidden = false;
      mdBox.innerHTML = '<p class="ph">（无内容返回）</p>';
    }
    $('chatHint').textContent = '主 Agent 待命';
  } catch (e) {
    mdBox.hidden = false;
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

    // md 下载：产物页是给眼睛看的，发平台要的是文件
    const old = document.getElementById('artDl');
    if (old) old.remove();
    const dl = document.createElement('a');
    dl.id = 'artDl';
    dl.className = 'ghost';
    dl.textContent = '下载 .md';
    dl.href = `/api/articles/${done.task_id}/md`;
    dl.download = '';
    document.getElementById('artDlSlot').appendChild(dl);
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
  $('artBrief').innerHTML = `<div class="bnote">这是写作前的<b>规划输出</b>，不是文章：先定主题、读者、大纲和字数，再照着写。事实点 ✓ = 在链接里找到了出处；⚠ 黄色 = 没找到出处，写作时会回避或标注。</div>
    <div class="bt">${esc(b.topic)}</div>
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

/* ---- GitHub 热点选题 ----
   不自动拉取：一进页面就打 GitHub API 会白耗 60 次/小时的限额。
   两个榜单：new = 近 7 天新建的高星新仓库；hot = 高星且近期还在更新的老牌。
   「换一批」翻页，翻到头自动切另一个榜单。 */

let ghState = { mode: 'new', page: 1 };

function renderTrend(items) {
  $('ghList').innerHTML = items.map(i => `
    <button class="titem" data-obj="${esc(i.full_name)}">
      <b>${esc(i.full_name)}</b>
      <span>★ ${i.stars}${i.language ? ' · ' + esc(i.language) : ''}</span>
      <em>${esc((i.description || '').slice(0, 46))}</em>
    </button>`).join('');
  document.querySelectorAll('.titem').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('.titem').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      const name = b.dataset.obj;
      $('artInput').value = `用 GitHub 上的 ${name} 这个项目写一篇介绍（链接：https://github.com/${name}）`;
      autosize($('artInput'));
      $('artHint').textContent = '已选中，点「生成文章」';
    };
  });
}

$('ghLoad').onclick = async () => {
  $('ghLoad').disabled = true;
  const { mode, page } = ghState;
  $('ghLoad').textContent = page > 1 ? `翻页中…（${mode === 'new' ? '新秀' : '热门'}榜 第${page}页）` : '拉取中…';
  try {
    const d = await (await fetch(`/api/trending/github?days=7&limit=8&mode=${mode}&page=${page}`)).json();
    if (d.detail) throw new Error(d.detail);
    renderTrend(d.items);
    const label = mode === 'new'
      ? '新秀榜：近 7 天新建、star 涨得最快的仓库'
      : '热门榜：star 3000+ 且本周还在更新的仓库';
    $('ghMode').textContent = `${label} · 第 ${d.page} 页`;
    if (d.has_more) {
      ghState.page = page + 1;
      $('ghLoad').textContent = '换一批';
    } else {
      ghState.mode = mode === 'new' ? 'hot' : 'new';
      ghState.page = 1;
      $('ghLoad').textContent = `换到${ghState.mode === 'new' ? '新秀' : '热门'}榜`;
    }
  } catch (e) {
    $('ghList').innerHTML = `<p class="ph" style="color:var(--err)">${esc(e.message)}</p>`;
    $('ghLoad').textContent = '重试';
  } finally {
    $('ghLoad').disabled = false;
  }
};

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
