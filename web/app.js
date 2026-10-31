/* 前端逻辑：SSE 流式接收，四段状态机。
   刻意不引框架——这个页面只有四块状态，React 的收益不如多一层构建成本。 */

const $ = (id) => document.getElementById(id);
const inputEl = $('input');
const goEl = $('go');
const mdEl = $('md');
const thinkEl = $('think');
const thinkTextEl = $('thinkText');
const briefEl = $('brief');
const stagesEl = $('stages');
const reflectListEl = $('reflectList');
const previewBox = $('previewBox');
const hintEl = $('hint');

let platform = 'wechat';
let vstyle = 'auto';
let words = 'medium';
let es = null;
let raw = '';
let reasoning = '';
let pending = false;
let reflectCount = 0;
let stageOrder = ['plan', 'compose', 'reflect', 'render'];

/* ---------------- 基础工具 ---------------- */

function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function inlineMd(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.+?)`/g, '<code>$1</code>');
}

/** 够用的 Markdown → HTML。流式渲染时每帧全量重排，所以必须够快。 */
function renderMd(text, withCursor) {
  const lines = text.split('\n');
  const out = [];
  let inList = false;
  for (const rawLine of lines) {
    const line = rawLine.replace(/\s+$/, '');
    if (!line.trim()) {
      if (inList) { out.push('</ul>'); inList = false; }
      continue;
    }
    if (line.startsWith('### ')) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<h4>${inlineMd(line.slice(4))}</h4>`);
    } else if (line.startsWith('## ')) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<h3>${inlineMd(line.slice(3))}</h3>`);
    } else if (line.startsWith('# ')) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<h2>${inlineMd(line.slice(2))}</h2>`);
    } else if (line.startsWith('> ')) {
      out.push(`<blockquote>${inlineMd(line.slice(2))}</blockquote>`);
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${inlineMd(line.replace(/^\s*[-*]\s+/, ''))}</li>`);
    } else {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<p>${inlineMd(line)}</p>`);
    }
  }
  if (inList) out.push('</ul>');
  if (withCursor) out.push('<span class="cursor"></span>');
  return out.join('\n');
}

function scheduleRender() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => {
    pending = false;
    mdEl.innerHTML = renderMd(raw, true);
    mdEl.parentElement.scrollTop = mdEl.parentElement.scrollHeight;
  });
}

/* ---------------- 状态机 ---------------- */

function setStage(name, label) {
  const i = stageOrder.indexOf(name);
  stagesEl.querySelectorAll('.stage').forEach((el, k) => {
    el.classList.toggle('done', k < i);
    el.classList.toggle('on', k === i);
    if (k === i && label) el.querySelector('em').textContent = label;
  });
}

function setThink(text, busy) {
  thinkTextEl.textContent = text;
  thinkEl.classList.toggle('busy', !!busy);
}

function resetUI() {
  raw = ''; reasoning = ''; reflectCount = 0;
  $('empty').hidden = true;
  thinkEl.hidden = false;
  mdEl.innerHTML = '<p class="ph">正在拆解输入…</p>';
  briefEl.classList.remove('on');
  briefEl.innerHTML = '';
  reflectListEl.innerHTML = '<p class="ph">每次执行后，反思会给出分维度评分、问题清单和修复动作。</p>';
  $('rCount').textContent = '0';
  previewBox.innerHTML = '<p class="ph">生成完成后，这里可以直接播放动画成品。</p>';
  stagesEl.querySelectorAll('.stage').forEach(el => {
    el.className = 'stage';
    el.querySelector('em').textContent = {plan:'拆解输入',compose:'按大纲写作',reflect:'校验与评分',render:'生成动画'}[el.dataset.s];
  });
  setThink('处理中', true);
}

function renderBrief(b) {
  const tags = [
    b.platform, b.audience, `${b.word_limit} 字`, b.tone
  ].filter(Boolean).map(t => `<span class="tag">${esc(String(t))}</span>`).join('');
  const outline = (b.outline || []).map(s => `<li>${esc(s.heading)}</li>`).join('');
  const facts = (b.facts || []).map(f =>
    `<div class="fact ${f.source ? '' : 'warn'}">${f.source ? '✓' : '⚠'} ${esc(f.claim)}${f.source ? '（' + esc(f.source) + '）' : '（无出处）'}</div>`
  ).join('');
  briefEl.innerHTML = `
    <div class="bt">${esc(b.topic)}</div>
    <div class="bd">${esc(b.angle || '')}</div>
    <div class="tags">${tags}</div>
    <ol>${outline}</ol>
    ${facts}`;
  briefEl.classList.add('on');
}

function addReflection(r) {
  reflectCount += 1;
  $('rCount').textContent = String(reflectCount);
  const scores = Object.entries(r.scores || {}).map(([k, v]) => `
    <div class="srow">
      <span>${esc(k)}</span>
      <span class="sbar"><i style="--w:${Math.max(0, Math.min(100, v))}%"></i></span>
      <span class="sval">${v}</span>
    </div>`).join('');
  const issues = (r.issues || []).map(i => `<div>${esc(i)}</div>`).join('');
  const el = document.createElement('div');
  el.className = 'rcard';
  el.innerHTML = `
    <div class="rh">
      <b>第 ${r.round} 轮</b>
      <span class="act ${r.repair_action}">${r.repair_action.toUpperCase()}</span>
    </div>
    ${scores}
    ${issues ? `<div class="iss">${issues}</div>` : ''}
    <div class="rtotal">综合 ${r.total} / 100</div>`;
  if (reflectListEl.querySelector('.ph')) reflectListEl.innerHTML = '';
  reflectListEl.prepend(el);
}

function showPreview(url, result) {
  const rows = (result.rounds || []).map(x => `第${x.round}轮 ${x.total}分·${x.action}`).join('  ');
  previewBox.innerHTML = `
    <div class="prow">
      <a href="${url}" target="_blank" rel="noopener">新窗口打开</a>
      <button id="reloadFrame">重播</button>
      <span class="meta">${esc(rows)}</span>
    </div>
    <iframe src="${url}" title="动画产物"></iframe>`;
  $('reloadFrame').onclick = () => {
    const f = previewBox.querySelector('iframe');
    f.src = f.src;
  };
}

/* ---------------- 事件分发 ---------------- */

function handleEvent(ev) {
  switch (ev.type) {
    case 'stage':
      setStage(ev.stage, ev.label);
      setThink(ev.label, true);
      break;

    case 'brief':
      renderBrief(ev.brief);
      if (!raw) mdEl.innerHTML = '<p class="ph">大纲已就绪，开始写作…</p>';
      break;

    case 'round_start':
      raw = '';
      reasoning = '';
      if (ev.round > 1) {
        mdEl.innerHTML = '<p class="ph">按上一轮的问题重写…</p>';
        setThink(`第 ${ev.round} 轮重写`, true);
      }
      break;

    case 'delta':
      if (ev.kind === 'reasoning') {
        reasoning += ev.text;
        setThink('思考：' + reasoning.slice(-90).replace(/\n/g, ' '), true);
      } else {
        raw += ev.text;
        scheduleRender();
      }
      break;

    case 'reflection':
      addReflection(ev.reflection);
      break;

    case 'note':
      setThink(ev.text, true);
      break;

    case 'done':
      setStage('render', '完成');
      stagesEl.querySelectorAll('.stage').forEach(el => {
        el.classList.remove('on');
        el.classList.add('done');
      });
      setThink(`完成 · ${resultLine(ev.result)}`, false);
      showPreview(ev.url, ev.result);
      $('cost').textContent = `${ev.result.word_count} 字 · 约 ${ev.result.tokens} tokens`;
      goEl.disabled = false;
      goEl.textContent = '再生成一篇';
      if (es) { es.close(); es = null; }
      break;

    case 'error':
      setThink('失败：' + ev.message, false);
      mdEl.innerHTML = `<p class="ph" style="color:#ff6b6b">${esc(ev.message)}</p>`;
      goEl.disabled = false;
      goEl.textContent = '重试';
      if (es) { es.close(); es = null; }
      break;
  }
}

function resultLine(r) {
  const sec = (r.elapsed_ms / 1000).toFixed(1);
  return `${r.mocked ? 'mock' : r.model} · ${r.rounds.length} 轮 · ${sec}s`;
}

/* ---------------- 交互 ---------------- */

document.querySelectorAll('#platform button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('#platform button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    platform = btn.dataset.p;
  };
});

document.querySelectorAll('#vstyle button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('#vstyle button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    vstyle = btn.dataset.s;
  };
});

document.querySelectorAll('#words button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('#words button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    words = btn.dataset.w;
  };
});

document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
    tab.classList.add('on');
    document.querySelectorAll('.view').forEach(v => v.classList.remove('on'));
    $('view' + tab.dataset.v.charAt(0).toUpperCase() + tab.dataset.v.slice(1)).classList.add('on');
  };
});

goEl.onclick = async () => {
  const text = inputEl.value.trim();
  if (text.length < 2) { inputEl.focus(); return; }

  goEl.disabled = true;
  goEl.textContent = '生成中…';
  resetUI();
  document.querySelector('.tab[data-v="doc"]').click();

  try {
    const resp = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: text, platform, style: vstyle, words })
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    const { task_id } = await resp.json();

    es = new EventSource(`/api/tasks/${task_id}/events`);
    es.onmessage = (m) => {
      try { handleEvent(JSON.parse(m.data)); } catch (e) { console.error(e); }
    };
    es.onerror = () => {
      if (es && es.readyState === EventSource.CLOSED) {
        setThink('连接中断', false);
        goEl.disabled = false;
        goEl.textContent = '重试';
      }
    };
  } catch (e) {
    setThink('失败：' + e.message, false);
    goEl.disabled = false;
    goEl.textContent = '重试';
  }
};

inputEl.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') goEl.click();
});

/* ---------------- 启动 ---------------- */

(async function boot() {
  // ?demo=1 自动填充并开跑，用于截图自检和演示
  if (new URLSearchParams(location.search).get('demo') === '1') {
    inputEl.value = '从 GitHub 热榜挑一个项目，写篇公众号介绍它的架构';
    setTimeout(() => goEl.click(), 500);
  }
  try {
    const h = await (await fetch('/api/health')).json();
    const dot = $('modeDot');
    dot.classList.add(h.mock ? 'mock' : 'live');
    $('modeText').textContent = h.mock ? 'MOCK 模式' : h.model;
    if (h.mock) {
      hintEl.textContent = 'Mock 模式：数据结构与真实调用完全一致，切真实模型只需设 LLM_MOCK=0。';
    }
  } catch { $('modeText').textContent = '离线'; }

  // 粒子背景
  const c = document.getElementById('dots');
  const ctx = c.getContext('2d');
  let w, h, pts;
  const resize = () => {
    w = c.width = innerWidth * devicePixelRatio;
    h = c.height = innerHeight * devicePixelRatio;
    c.style.width = innerWidth + 'px';
    c.style.height = innerHeight + 'px';
    pts = Array.from({ length: innerWidth < 700 ? 28 : 54 }, () => ({
      x: Math.random() * w, y: Math.random() * h,
      vx: (Math.random() - .5) * .13 * devicePixelRatio,
      vy: (Math.random() - .5) * .13 * devicePixelRatio
    }));
  };
  resize(); addEventListener('resize', resize);
  (function frame() {
    ctx.clearRect(0, 0, w, h);
    const R = 128 * devicePixelRatio;
    for (const p of pts) {
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > w) p.vx *= -1;
      if (p.y < 0 || p.y > h) p.vy *= -1;
    }
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y;
        const d = Math.hypot(dx, dy);
        if (d < R) {
          ctx.strokeStyle = 'rgba(120,90,230,' + (0.14 * (1 - d / R)) + ')';
          ctx.lineWidth = devicePixelRatio;
          ctx.beginPath(); ctx.moveTo(pts[i].x, pts[i].y); ctx.lineTo(pts[j].x, pts[j].y); ctx.stroke();
        }
      }
      ctx.fillStyle = 'rgba(160,140,255,.45)';
      ctx.beginPath(); ctx.arc(pts[i].x, pts[i].y, 1.4 * devicePixelRatio, 0, 7); ctx.fill();
    }
    requestAnimationFrame(frame);
  })();
})();
