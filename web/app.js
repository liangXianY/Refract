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
  const linesAll = String(text).split('\n');
  let tableBuf = null;   // {header, rows}
  const isSep = (l) => /^\s*\|?[\s:|-]*-[-\s:|]*\|?\s*$/.test(l) && l.includes('-') && l.includes('|');
  const flushTable = () => {
    if (tableBuf) {
      const th = tableBuf.header.map(c => `<th>${inlineMd(c)}</th>`).join('');
      const trs = tableBuf.rows.map(r => '<tr>' + r.map(c => `<td>${inlineMd(c)}</td>`).join('') + '</tr>').join('');
      out.push(`<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`);
      tableBuf = null;
    }
  };
  for (let li = 0; li < linesAll.length; li++) {
    const rawLine = linesAll[li];
    const line = rawLine.replace(/\s+$/, '');
    if (!line.trim()) { if (inList) { out.push('</ul>'); inList = false; } flushTable(); continue; }
    // 表格：| 单元格 | 行 + 下一行是 |---|---| 分隔
    if (line.trim().startsWith('|') && li + 1 < linesAll.length && isSep(linesAll[li + 1])) {
      if (inList) { out.push('</ul>'); inList = false; }
      tableBuf = { header: line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim()), rows: [] };
      li++;  // 跳过分隔行
      while (li < linesAll.length && linesAll[li].trim().startsWith('|')) {
        tableBuf.rows.push(linesAll[li].trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim()));
        li++;
      }
      li--; flushTable();
      continue;
    }
    // 图片：真图直接渲染，配图占位渲染成占位卡
    const mImg = line.trim().match(/^!\[([^\]]*)\]\((https?:[^)]+)\)\s*$/);
    if (mImg) {
      if (inList) { out.push('</ul>'); inList = false; } flushTable();
      const cap = (mImg[1] || '').trim();
      const capHtml = cap && !/这里放|什么图|配图位|图片/.test(cap)
        ? `<figcaption>${esc(cap)}</figcaption>` : '';
      out.push(`<figure class="mdimg"><img src="${esc(mImg[2])}" alt="${esc(cap)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.closest('figure').style.display='none'">${capHtml}</figure>`);
      continue;
    }
    if (line.startsWith('### ')) { if (inList) { out.push('</ul>'); inList = false; } out.push(`<h4>${inlineMd(line.slice(4))}</h4>`); }
    else if (line.startsWith('## ')) { if (inList) { out.push('</ul>'); inList = false; } out.push(`<h3>${inlineMd(line.slice(3))}</h3>`); }
    else if (line.startsWith('# ')) { if (inList) { out.push('</ul>'); inList = false; } out.push(`<h2>${inlineMd(line.slice(2))}</h2>`); }
    else if (line.startsWith('> ')) out.push(`<blockquote>${inlineMd(line.slice(2))}</blockquote>`);
    else if (/^\s*[-*]\s+/.test(line)) { if (!inList) { out.push('<ul>'); inList = true; } out.push(`<li>${inlineMd(line.replace(/^\s*[-*]\s+/, ''))}</li>`); }
    else { if (inList) { out.push('</ul>'); inList = false; } flushTable(); out.push(`<p>${inlineMd(line)}</p>`); }
  }
  if (inList) out.push('</ul>');
  flushTable();
  if (withCursor) out.push('<span class="cursor"></span>');
  return out.join('\n');
}

/** 起任务 + 读 SSE。onEvent 收到每一个事件。 */
async function runTask(payload, onEvent, path = '/api/generate', timeoutMs = 90000) {
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
  const { task_id, session_id } = await resp.json();
  if (session_id) localStorage.setItem('refract_sid', session_id);

  return new Promise((resolve, reject) => {
    const es = new EventSource(`/api/tasks/${task_id}/events`);
    // 看门狗：timeoutMs 内没有任何事件就判卡死。默认 90s；
    // 视频成片要等 Opus 写 3-5 分钟代码，调用方得显式放宽
    let lastBeat = Date.now();
    let aborted = false;
    const watchdog = setInterval(() => {
      if (Date.now() - lastBeat > timeoutMs) {
        clearInterval(watchdog);
        es.close();
        reject(new Error('任务超时没有响应，已断开（可重试）'));
      }
    }, 5000);
    const finish = (fn, arg) => { clearInterval(watchdog); es.close(); fn(arg); };
    es.onmessage = (m) => {
      lastBeat = Date.now();
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      onEvent?.(ev);
      if (ev.type === 'done') finish(resolve, ev);
      if (ev.type === 'error') finish(reject, new Error(ev.message));
    };
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED && !aborted) finish(reject, new Error('连接中断'));
    };
    // 注册当前任务的中止函数，供暂停按钮调用
    currentAbort = () => {
      aborted = true;
      clearInterval(watchdog);
      es.close();
      const err = new Error('已暂停');
      err.paused = true;
      reject(err);
    };
  });
}

let currentAbort = null;  // 当前对话任务的中止函数（暂停用）

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
  el.className = 'tcard folded';
  el.innerHTML = `<span class="tstat run"></span><span class="ttitle">${esc(title)}</span>
    <span class="ttool">${esc(tool)}</span><span class="ttime"></span>
    <span class="tcaret"></span>
    <div class="tdetail"></div>`;
  // 整张卡片头部都可点击展开/收起（点标题、点箭头、点空白都行）
  const header = el.querySelector('.ttitle');
  const toggle = () => el.classList.toggle('folded');
  header.onclick = toggle;
  el.querySelector('.tcaret').onclick = toggle;
  flow.appendChild(el);
  flow.hidden = false;  // 有工具了，任务列表该露出来了
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

function planCard(planBox, steps) {
  const el = document.createElement('div');
  el.className = 'plancard';
  el.innerHTML = `<div class="pc-h">执行计划 · ${steps.length} 步</div>` +
    steps.map((s, i) => `<div class="pc-s" data-i="${i}"><b>${i + 1}</b><span>${esc(s.title || s.tool)}</span><em>${esc(s.why || '')}</em></div>`).join('');
  planBox.appendChild(el);
  planBox.hidden = false;
  chatlog.scrollTop = chatlog.scrollHeight;
  return el;
}

// 建一轮「AI 消息」的渲染环境，返回事件处理器。实时和重放共用同一套渲染逻辑。
function makeTurn() {
  const body = addMsg('ai', `<div class="planbox" data-plan hidden></div>
    <div class="thinking" data-thinking hidden><span class="tdot"></span>思考中…</div>
    <div class="md" data-md style="font-size:13.5px" hidden></div>
    <div class="flow" data-flow hidden></div>`);
  const flow = body.querySelector('[data-flow]');
  const mdBox = body.querySelector('[data-md]');
  const planBox = body.querySelector('[data-plan]');
  const thinkBox = body.querySelector('[data-thinking]');

  let raw = '';
  let pending = false;
  let settled = false;  // 收尾后，排队的 rAF 不许再把带光标的版本写回去
  let activeCard = null;
  let reasoning = false;  // 是否正在思考（推理模型先吐 reasoning 再吐正文）

  const flush = (withCursor) => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      if (settled && withCursor) return;  // 竞态防护：收尾后的延迟刷新直接丢弃
      mdBox.hidden = false;
      mdBox.innerHTML = renderMd(raw, withCursor);
      chatlog.scrollTop = chatlog.scrollHeight;
    });
  };

  const handle = (ev) => {
    switch (ev.type) {
      case 'plan':
        if (ev.steps && ev.steps.length) planCard(planBox, ev.steps);
        break;
      case 'step_start':
        activeCard = toolCard(flow, ev.title, ev.tool);
        activeCard.dataset.i = ev.i;
        break;
      case 'tool_detail': {
        const box = activeCard || flow.lastElementChild;
        const det = box?.querySelector('.tdetail');
        if (det) det.innerHTML = (ev.lines || []).map(l => `<div>${esc(l)}</div>`).join('');
        break;
      }
      case 'step_end': {
        // 查找对应卡片：优先按 data-i，找不到退回 activeCard，再退回最后一个 tcard
        let card = flow.querySelector(`.tcard[data-i="${ev.i}"]`) || activeCard;
        if (!card) card = flow.querySelector('.tcard:last-of-type');
        if (card) {
          card.querySelector('.tstat').className = `tstat ${ev.status === 'ok' ? 'ok' : 'err'}`;
          card.querySelector('.ttime').textContent = ev.status === 'ok'
            ? `${((ev.elapsed_ms || 0) / 1000).toFixed(1)}s` : '失败';
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
        miniLine(flow, ev.stage.toUpperCase(), ev.label, ev.stage === 'reflect' ? 'hl' : '');
        break;
      case 'reflection':
        miniLine(flow, `第 ${ev.reflection.round} 轮自检`, `${ev.reflection.total} 分 · ${ev.reflection.repair_action.toUpperCase()}`, 'hl');
        break;
      case 'delta':
        if (ev.kind === 'reasoning') {
          // 推理模型的思考过程：显示「思考中」，让用户知道没卡住
          reasoning = true;
          thinkBox.hidden = false;
        } else if (ev.kind === 'content') {
          // 正文来了：隐藏「思考中」，开始流式正文
          if (reasoning) { reasoning = false; thinkBox.hidden = true; }
          raw += ev.text;
          flush(true);
        }
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
        }
        break;
      case 'video': {
        // 直接生成的视频：嵌入播放器，网页直接播
        const player = document.createElement('div');
        player.className = 'chatplayer';
        player.innerHTML = `<a href="${esc(ev.url)}" target="_blank" rel="noopener">新窗口打开</a>
          ${ev.degraded ? '<span class="degrade">模型没过闸门，降级了模板片</span>' : ''}
          <iframe src="${esc(ev.url)}" title="视频成品"></iframe>`;
        flow.appendChild(player);
        chatlog.scrollTop = chatlog.scrollHeight;
        break;
      }
      case 'done_reply':
        break;
      case 'error':
        mdBox.hidden = false;
        mdBox.innerHTML = `<p style="color:var(--err)">${esc(ev.message)}</p>`;
        break;
    }
  };

  const finish = () => {
    settled = true;  // 收尾：后续排队的 rAF 不再写带光标版本
    thinkBox.hidden = true;  // 思考框收尾必隐藏
    if (raw) { mdBox.hidden = false; mdBox.innerHTML = renderMd(raw, false); }
    if (!flow.children.length && !raw) {
      mdBox.hidden = false;
      mdBox.innerHTML = '<p class="ph">（无内容返回）</p>';
    }
    if (!flow.children.length) flow.hidden = true;
    // 还停在「进行中」转圈的卡片，收尾时统一标记为完成（历史重放/暂停场景）
    flow.querySelectorAll('.tcard .tstat.run').forEach(s => { s.className = 'tstat ok'; });
  };

  return { flow, mdBox, planBox, handle, finish };
}

async function sendChat(text) {
  text = (text || '').trim();
  if (!text || chatting) return;
  chatting = true;
  setSendState('running');
  $('chatInput').value = '';
  autosize($('chatInput'));

  addMsg('user', esc(text).replace(/\n/g, '<br/>'));
  const turn = makeTurn();

  try {
    $('chatHint').textContent = '主 Agent 规划中…';
    await runTask(
      { input: text, session_id: localStorage.getItem('refract_sid') || '' },
      (ev) => { turn.handle(ev); if (ev.type === 'plan') $('chatHint').textContent = `执行 ${ev.steps.length} 步`; },
      '/api/chat'
    );
    turn.finish();
    $('chatHint').textContent = '主 Agent 待命';
  } catch (e) {
    if (e.paused) {
      // 用户主动暂停：保留已收到的内容，不报错
      turn.finish();
      $('chatHint').textContent = '已暂停';
    } else {
      turn.mdBox.hidden = false;
      turn.mdBox.innerHTML = `<p style="color:var(--err)">${esc(e.message)}</p>`;
      $('chatHint').textContent = '出错了：' + e.message;
    }
  } finally {
    chatting = false;
    currentAbort = null;
    setSendState('idle');
  }
}

// 发送按钮三态：idle / running / done
function setSendState(state) {
  const btn = $('chatSend');
  btn.classList.remove('idle', 'running');
  btn.classList.add(state);
  if (state === 'running') {
    btn.title = '暂停';
    btn.disabled = false;  // 进行中可点击 → 暂停
  } else {
    btn.title = '发送';
    btn.disabled = false;
  }
}

// 发送按钮点击：空闲→发送；进行中→暂停
$('chatSend').onclick = () => {
  if (chatting) {
    if (currentAbort) { currentAbort(); currentAbort = null; }
  } else {
    sendChat($('chatInput').value);
  }
};

// 刷新时按 session_id 拉历史，逐轮重放
async function loadHistory() {
  const sid = localStorage.getItem('refract_sid');
  if (!sid) return;
  try {
    const r = await fetch(`/api/session/${sid}`);
    if (!r.ok) return;
    const { messages } = await r.json();
    if (!messages || !messages.length) return;
    for (const m of messages) {
      if (m.role === 'user') {
        addMsg('user', esc(m.text).replace(/\n/g, '<br/>'));
      } else {
        const turn = makeTurn();
        for (const ev of m.events || []) turn.handle(ev);
        turn.finish();
      }
    }
  } catch { /* 拉不到历史就算了，不阻塞 */ }
}

function autosize(el) {
  el.style.height = '24px';
  el.style.height = Math.min(120, el.scrollHeight) + 'px';
}

$('chatInput').addEventListener('input', (e) => autosize(e.target));
$('chatInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(e.target.value); }
});
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

/* ---- 平台热榜（小红书/微博/知乎/抖音）----
   60s API，免费聚合接口。和 GitHub 一样点了才请求，不自动打。
   选中某条 → 填进输入框 → 点「生成文章」即成稿。 */
let hotSource = 'rednote';

function renderHot(items) {
  $('hotList').innerHTML = items.map(i => `
    <button class="titem" data-obj="${esc(i.title)}">
      <b>${esc(i.title)}</b>
      <span>${esc(i.score || '热度 ' + (i.rank || ''))}</span>
    </button>`).join('');
  document.querySelectorAll('#hotList .titem').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('#hotList .titem').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      $('artInput').value = `结合「${b.dataset.obj}」这个热点写一篇，平台小红书风格`;
      autosize($('artInput'));
      $('artHint').textContent = '已选中热点，点「生成文章」';
    };
  });
}

async function loadHot(source) {
  hotSource = source;
  document.querySelectorAll('.hot-tab').forEach(t => t.classList.toggle('on', t.dataset.s === source));
  $('hotList').innerHTML = '<p class="ph">拉取中…</p>';
  try {
    const d = await (await fetch(`/api/hot/${source}?limit=10`)).json();
    if (d.detail) throw new Error(d.detail);
    renderHot(d.items);
  } catch (e) {
    $('hotList').innerHTML = `<p class="ph" style="color:var(--err)">${esc(e.message)}</p>`;
  }
}

document.querySelectorAll('.hot-tab').forEach(t => {
  t.onclick = () => loadHot(t.dataset.s);
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

async function genVideo() {
  const text = $('vidInput').value.trim();
  const url = $('vidUrl').value.trim();
  if ((text.length < 2 && !url) || vidBusy) return;

  vidBusy = true;
  $('vidPlan').disabled = true;
  $('vidPlan').textContent = '生成中…';
  $('vidHint').textContent = '自动定视觉方向 → 选卡 → 逐镜头实现';
  $('planList').hidden = true;
  $('playerBox').hidden = false;
  const t0 = Date.now();

  $('playerBox').innerHTML = `
    <div class="vflow">
      <div class="tcard" id="vStepBrief"><span class="tstat run"></span><span class="ttitle">整理内容为分镜素材</span>
        <span class="ttool">deepseek</span><span class="ttime"></span></div>
      <div class="tcard wait" id="vStepPlan"><span class="tstat"></span><span class="ttitle">自动定视觉方向</span>
        <span class="ttool">deepseek</span><span class="ttime"></span></div>
      <div class="tcard wait" id="vStepGen"><span class="tstat"></span><span class="ttitle">选配方卡 · 逐镜头生成动画</span>
        <span class="ttool">opus</span><span class="ttime" id="vTimer">0s</span>
        <div class="tdetail"><div>映射镜头 → 逐镜头实现 → 拼装工程，通常 1-2 分钟</div></div></div>
      <div class="tcard wait" id="vStepGate"><span class="tstat"></span><span class="ttitle">闸门校验</span>
        <span class="ttool">静态检查</span><span class="ttime"></span>
        <div class="tdetail"><div>逐镜头 + 工程级检查，不过回炉一次</div></div></div>
    </div>
    <div class="pstage slim"><div class="spin"></div>
      <span id="pstageTx">生成中…</span></div>`;
  window.__vTimer = setInterval(() => {
    const el = document.getElementById('vTimer');
    if (el) el.textContent = Math.floor((Date.now() - t0) / 1000) + 's';
  }, 1000);

  try {
    const done = await runTask(
      { input: text, url, platform: 'video' },
      (ev) => {
        if (ev.type === 'stage') {
          const tx = document.getElementById('pstageTx');
          if (tx) tx.textContent = ev.label + (ev.model ? ` · ${ev.model}` : '');
          // 按 stage 推进状态卡
          const stepMap = { brief: 'vStepBrief', plan: 'vStepPlan', render: 'vStepGen', bundle: 'vStepGen' };
          const stepId = stepMap[ev.stage];
          if (stepId) {
            document.querySelectorAll('#playerBox .tcard .tstat').forEach(s => { if (s.className === 'tstat run') s.className = 'tstat ok'; });
            const step = document.getElementById(stepId);
            if (step) { step.classList.remove('wait'); step.querySelector('.tstat').className = 'tstat run'; }
          }
        }
        if (ev.type === 'gate') {
          clearInterval(window.__vTimer);
          const gen = document.getElementById('vStepGen');
          if (gen) { gen.querySelector('.tstat').className = 'tstat ok'; }
          const gate = document.getElementById('vStepGate');
          if (gate) {
            gate.classList.remove('wait');
            gate.querySelector('.tstat').className = `tstat ${ev.ok ? 'ok' : 'err'}`;
            gate.querySelector('.ttime').textContent = `第 ${ev.attempts} 版`;
            const det = gate.querySelector('.tdetail');
            if (det) det.innerHTML = ev.ok
              ? `<div>${ev.stats?.shots ?? '?'} 个镜头 · ${(ev.stats?.duration_s ?? 0).toFixed(1)}s${ev.stats?.fallbacks ? ` · ${ev.stats.fallbacks} 镜头落模板` : ''}</div>`
              : `<div>${esc((ev.failures || ['未过'])[0]).slice(0, 60)}</div>`;
          }
          const tx = document.getElementById('pstageTx');
          if (tx) tx.textContent = ev.ok ? '闸门通过，打包成网页…' : '未过闸门，保存降级片…';
        }
      },
      '/api/video/render',
      600000
    );

    const d = done.degraded;
    $('playerBox').innerHTML = `
      <div class="prow">
        <a href="${done.url}" target="_blank" rel="noopener">新窗口打开</a>
        <span class="meta">${esc(done.result?.model || '')}${d ? ' · 降级模板' : ''}</span>
        <button class="ghost" id="pBack">重新生成</button>
      </div>
      <iframe src="${done.url}" title="视频成品"></iframe>`;
    clearInterval(window.__vTimer);
    document.getElementById('pBack').onclick = () => {
      $('playerBox').hidden = true;
      $('planList').hidden = false;
      $('playerBox').querySelectorAll('.prow, .vframe-wrap, iframe').forEach(el => el.remove());
    };
    $('vidHint').textContent = d ? '模型产物没过闸门，已降级模板' : '完成 · 可直接播放';
  } catch (e) {
    clearInterval(window.__vTimer);
    $('playerBox').innerHTML = `<p class="ph" style="color:var(--err)">${esc(e.message)}</p>`;
    $('vidHint').textContent = '失败：' + e.message;
    $('planList').hidden = false;
  } finally {
    vidBusy = false;
    $('vidPlan').disabled = false;
    $('vidPlan').textContent = '生成动画';
  }
}

/* 关键帧预览：不调任何 API，用方案自己的配色在 150x84 canvas 上
   画一帧"大概长这样"的示意。方案是抽象文字，预览让它变成一眼能比较的东西。 */
function drawKeyframe(canvas, p) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const pal = p.palette && p.palette.length ? p.palette : ['#0b0f17', '#22d3ee', '#f8fafc'];
  const bg = pal[0], accent = pal[1] || '#22d3ee', accent2 = pal[2] || inkOf(bg);
  function inkOf(hex) {
    try {
      const n = parseInt(hex.slice(1), 16);
      return ((n >> 16 & 255) * 0.299 + (n >> 8 & 255) * 0.587 + (n & 255) * 0.114) > 140 ? '#1a1a1a' : '#f8fafc';
    } catch { return '#f8fafc'; }
  }
  const ink = pal[2] && pal[2].length > 3 ? pal[2] : inkOf(bg);
  ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H);
  const style = (p.style || 'hud').toLowerCase();

  if (['hud', 'tech', 'pixel', 'geo'].includes(style)) {
    // 网格底
    ctx.strokeStyle = 'rgba(128,128,128,.14)'; ctx.lineWidth = 1;
    for (let x = 12; x < W; x += 14) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
    for (let y = 10; y < H; y += 14) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
    // 三条能力管道 / 数据柱
    const bars = Math.min(3, Math.max(2, (p.beats || []).length || 3));
    for (let i = 0; i < bars; i++) {
      const bx = 18 + i * ((W - 36) / bars), bw = (W - 36) / bars - 8;
      const bh = 12 + ((i * 7 + p.duration) % 22);
      ctx.fillStyle = i === 0 ? accent : (i === 1 ? accent2 : 'rgba(128,128,128,.35)');
      ctx.globalAlpha = 0.85 - i * 0.18;
      ctx.fillRect(bx, H - 22 - bh, bw, bh);
      ctx.globalAlpha = 1;
    }
    ctx.strokeStyle = accent; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(W - 24, 20, 8, 0, Math.PI * 1.4); ctx.stroke();
  } else if (style === 'line' || style === 'sketch' || style === 'minimal') {
    // 纸面手绘感：白底 + 手绘线条
    ctx.strokeStyle = 'rgba(0,0,0,.08)';
    for (let y = 16; y < H; y += 12) { ctx.beginPath(); ctx.moveTo(10, y); ctx.lineTo(W - 10, y + ((y % 8) - 4) * 0.4); ctx.stroke(); }
    ctx.strokeStyle = accent; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(14, H - 18);
    ctx.bezierCurveTo(W * 0.3, H - 40, W * 0.55, H - 8, W - 18, H - 30); ctx.stroke();
    ctx.beginPath(); ctx.arc(W - 18, H - 30, 3.5, 0, Math.PI * 2); ctx.fillStyle = accent; ctx.fill();
  } else {
    // 兜底：几何拼贴
    ctx.fillStyle = accent; ctx.globalAlpha = 0.85;
    ctx.fillRect(W * 0.12, H * 0.24, W * 0.3, H * 0.4);
    ctx.fillStyle = accent2; ctx.globalAlpha = 0.5;
    ctx.beginPath(); ctx.arc(W * 0.68, H * 0.5, H * 0.26, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = 1;
  }
  // 标题字幕条
  ctx.fillStyle = ink; ctx.globalAlpha = 0.92;
  ctx.font = '600 9px system-ui, "PingFang SC", sans-serif';
  ctx.fillText(String(p.name || '').slice(0, 10), 12, 16);
  ctx.fillStyle = accent; ctx.fillRect(12, 20, 22, 2);
  ctx.globalAlpha = 1;
}

/* 等待生成时，把 4 套方案铺在下方空白区：默认收起，点开看详情、再点收起。
   生成的这版不带「用这套拍」按钮（正在生成），纯展示 + 折叠。 */
function compactPlans(plans, activeIdx) {
  const cards = plans.map((p, i) => `
    <article class="plan folded${i === activeIdx ? ' active' : ''}" data-i="${i}">
      <header class="ptoggle" data-i="${i}" role="button" tabindex="0">
        <span class="pid">${esc(p.id)}</span>
        <h4>${esc(p.name)}${i === activeIdx ? ' <span class="curtag">生成中</span>' : ''}</h4>
        <span class="ptag">${esc(p.style)} · ${p.duration}s</span>
        <span class="pcaret" aria-hidden="true"></span>
      </header>
      <div class="pbody">
        <canvas class="kf" width="300" height="84" data-p='${esc(JSON.stringify({style: p.style, palette: p.palette, name: p.name, duration: p.duration, beats: (p.beats||[]).length}))}'></canvas>
        <p class="pc">${esc(p.concept)}</p>
        <p class="pw"><em>为什么</em>${esc(p.why)}</p>
        <p class="pm"><em>运动</em>${esc(p.motion)}</p>
        <ol class="pbeats">${(p.beats || []).map(b => `<li>${esc(b)}</li>`).join('')}</ol>
        <div class="pfoot">
          <div class="swatches">${(p.palette || []).map(c => `<i style="background:${esc(c)}"></i>`).join('')}</div>
        </div>
      </div>
    </article>`).join('');
  const title = `<div class="wp-h">这轮出的 ${plans.length} 套方案（点开看详情，再点收起）</div>`;
  const wrap = `<div class="plangrid">${cards}</div>`;
  const html = title + wrap;
  // 关键帧预览
  const box = document.createElement('div');
  box.innerHTML = html;
  box.querySelectorAll('canvas.kf').forEach(c => {
    try { drawKeyframe(c, JSON.parse(c.dataset.p)); } catch { /* 忽略 */ }
  });
  return box.innerHTML;
}

function renderPlans(plans, brief) {
  const head = `
    <div class="planhead">
      <b>${esc(brief.topic)}</b>
      <span>${esc(brief.audience)} · 钩子：${esc(brief.hook || '—')}</span>
    </div>`;

  const cards = plans.map((p, i) => `
    <article class="plan" data-i="${i}">
      <header class="ptoggle" data-i="${i}" role="button" tabindex="0">
        <span class="pid">${esc(p.id)}</span>
        <h4>${esc(p.name)}</h4>
        <span class="ptag">${esc(p.style)} · ${p.duration}s</span>
        <span class="pcaret" aria-hidden="true"></span>
      </header>
      <div class="pbody">
        <canvas class="kf" width="300" height="84" data-p='${esc(JSON.stringify({style: p.style, palette: p.palette, name: p.name, duration: p.duration, beats: (p.beats||[]).length}))}'></canvas>
        <p class="pc">${esc(p.concept)}</p>
        <p class="pw"><em>为什么</em>${esc(p.why)}</p>
        <p class="pm"><em>运动</em>${esc(p.motion)}</p>
        <ol class="pbeats">${(p.beats || []).map(b => `<li>${esc(b)}</li>`).join('')}</ol>
        <div class="pfoot">
          <div class="swatches">${(p.palette || []).map(c => `<i style="background:${esc(c)}"></i>`).join('')}</div>
          <button class="go" data-i="${i}">用这套拍</button>
        </div>
      </div>
    </article>`).join('');

  $('planList').innerHTML = head + `<div class="plangrid">${cards}</div>`;
  // 关键帧预览
  document.querySelectorAll('.plan canvas.kf').forEach(c => {
    try { drawKeyframe(c, JSON.parse(c.dataset.p)); } catch { /* 预览失败不影响选择 */ }
  });
  document.querySelectorAll('.plan .go').forEach(b => {
    b.onclick = () => renderVideo(+b.dataset.i);
  });
  // 点标题折叠/展开方案详情
  document.querySelectorAll('.plan .ptoggle').forEach(h => {
    h.onclick = () => {
      const card = h.closest('.plan');
      card.classList.toggle('folded');
    };
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
  const t0 = Date.now();
  $('playerBox').innerHTML = `
    <div class="vflow">
      <div class="tcard done"><span class="tstat ok"></span><span class="ttitle">出方案</span><span class="ttool">deepseek + opus</span><span class="ttime">✓</span></div>
      <div class="tcard" id="vStepGen"><span class="tstat run"></span><span class="ttitle">选配方卡 · 逐镜头生成动画</span>
        <span class="ttool">${esc(plan.style)}</span><span class="ttime" id="vTimer">0s</span>
        <div class="tdetail" id="vGenTx"><div>映射镜头 → 逐镜头实现 → 拼装工程，通常 1-2 分钟</div></div></div>
      <div class="tcard wait" id="vStepGate"><span class="tstat"></span><span class="ttitle">闸门校验</span>
        <span class="ttool">静态检查</span><span class="ttime"></span>
        <div class="tdetail"><div>逐镜头 + 工程级检查，不过回炉一次</div></div></div>
    </div>
    <div class="pstage slim"><div class="spin"></div>
      <span id="pstageTx">生成中…</span></div>
    <div class="waitplans" id="waitPlans">${compactPlans(vidPlans, idx)}</div>`;
  window.__vTimer = setInterval(() => {
    const el = document.getElementById('vTimer');
    if (el) el.textContent = Math.floor((Date.now() - t0) / 1000) + 's';
  }, 1000);
  $('vidHint').textContent = '生成中…';
  // 等待区方案卡：可点开看详情、再收起
  document.querySelectorAll('#waitPlans .plan .ptoggle').forEach(h => {
    h.onclick = () => h.closest('.plan').classList.toggle('folded');
  });

  try {
    const done = await runTask(
      { input: $('vidInput').value.trim(), url: $('vidUrl').value.trim(), platform: 'video', plan },
      (ev) => {
        if (ev.type === 'stage') {
          const tx = document.getElementById('pstageTx');
          if (tx) tx.textContent = ev.label + (ev.model ? ` · ${ev.model}` : '');
        }
        if (ev.type === 'gate') {
          clearInterval(window.__vTimer);
          const gen = document.getElementById('vStepGen');
          if (gen) { gen.querySelector('.tstat').className = 'tstat ok'; }
          const gate = document.getElementById('vStepGate');
          if (gate) {
            gate.classList.remove('wait');
            gate.querySelector('.tstat').className = `tstat ${ev.ok ? 'ok' : 'err'}`;
            gate.querySelector('.ttime').textContent = `第 ${ev.attempts} 版`;
            const det = gate.querySelector('.tdetail');
            if (det) det.innerHTML = ev.ok
              ? `<div>${ev.stats?.shots ?? '?'} 个镜头 · ${(ev.stats?.duration_s ?? 0).toFixed(1)}s${ev.stats?.fallbacks ? ` · ${ev.stats.fallbacks} 镜头落模板` : ''}</div>`
              : `<div>${esc((ev.failures || ['未过'])[0]).slice(0, 60)}</div>`;
          }
          const tx = document.getElementById('pstageTx');
          if (tx) tx.textContent = ev.ok ? '闸门通过，打包成网页…' : '未过闸门，保存降级片…';
        }
      },
      '/api/video/render',
      600000
    );

    const d = done.degraded;
    $('playerBox').innerHTML = `
      <div class="prow">
        <a href="${done.url}" target="_blank" rel="noopener">新窗口打开</a>
        <span class="meta">${esc(plan.name)} · ${esc(done.result?.model || '')}${d ? ' · 降级模板' : ''}</span>
        <button class="ghost" id="pBack">换一套</button>
      </div>
      <iframe src="${done.url}" title="视频成品"></iframe>`;
    clearInterval(window.__vTimer);
    document.getElementById('pBack').onclick = () => {
      $('playerBox').hidden = true;
      $('planList').hidden = false;
      $('playerBox').querySelectorAll('.prow, .vframe-wrap, iframe').forEach(el => el.remove());
      document.querySelectorAll('.plan .go').forEach(b => { b.disabled = false; });
    };
    $('vidHint').textContent = d ? '模型产物没过闸门，已降级模板' : '完成 · 可直接播放';
  } catch (e) {
    clearInterval(window.__vTimer);
    $('playerBox').innerHTML = `<p class="ph" style="color:var(--err)">${esc(e.message)}</p>`;
    $('vidHint').textContent = '失败：' + e.message;
    $('planList').hidden = false;
    document.querySelectorAll('.plan .go').forEach(b => { b.disabled = false; });
  } finally {
    vidBusy = false;
  }
}

$('vidPlan').onclick = genVideo;
$('vidInput').addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') genVideo();
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
  loadHistory();  // 同一 IP 刷新后恢复历史对话
})();
