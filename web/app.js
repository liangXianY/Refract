/* Refract 前端：三个板块共用一套任务执行，按意图分流。
   会话走意图识别 → 派发子 Agent；文章/视频是显式入口，直达对应 Agent。 */

const $ = (id) => document.getElementById(id);

/* ============================ 通用 ============================ */

/* ============================ 账号 / 会话 ============================
   身份在服务端（HttpOnly Cookie），前端只缓存一份用于渲染。
   会话列表也由服务端给：换设备、换网络都能看到同一批对话。 */

const AUTH = { user: null, conversations: [], cid: '' };

function fmtTime(ts) {
  const t = new Date(ts * 1000), diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  return `${t.getMonth() + 1}/${t.getDate()}`;
}

function renderNavAuth() {
  const box = $('navAuth');
  if (!box) return;
  if (AUTH.user) {
    const initial = (AUTH.user.email || '?').slice(0, 1).toUpperCase();
    box.innerHTML = `
      <div class="navuser" title="${esc(AUTH.user.email)}">
        <span class="av">${esc(initial)}</span>
        <span class="em">${esc(AUTH.user.email)}</span>
        <button class="navout navpw" id="navPw">${AUTH.user.has_password ? '改密码' : '设密码'}</button>
        <button class="navout" id="navOut">退出</button>
      </div>`;
    $('navPw').onclick = () => openLogin('setpw');
    $('navOut').onclick = logout;
  } else {
    box.innerHTML = '<button class="btn-primary nav-cta" id="navLogin">登录</button>';
    $('navLogin').onclick = openLogin;
  }
}

function renderConvs() {
  const list = $('convList');
  if (!list) return;
  if (!AUTH.user) { list.innerHTML = ''; $('convFoot').innerHTML = ''; return; }
  if (!AUTH.conversations.length) {
    list.innerHTML = '<p class="convempty">还没有对话。<br>说第一句话就会自动建一条。</p>';
  } else {
    list.innerHTML = AUTH.conversations.map(c => `
      <button class="convi${c.id === AUTH.cid ? ' on' : ''}" data-cid="${esc(c.id)}">
        <span class="t">${esc(c.title || '新对话')}</span>
        <span class="m">${fmtTime(c.updated_at)}</span>
        <span class="x" data-del="${esc(c.id)}" title="删除这条会话">×</span>
      </button>`).join('');
  }
  $('convFoot').innerHTML = `<span>${AUTH.conversations.length} 条会话</span>
    <span>${esc((AUTH.user.email || '').split('@')[0])}</span>`;
  list.querySelectorAll('.convi').forEach(el => {
    el.onclick = (e) => {
      const del = e.target.closest('[data-del]');
      if (del) { e.stopPropagation(); delConv(del.dataset.del); return; }
      openConversation(el.dataset.cid);
    };
  });
}

function setCurrentConv(cid) {
  AUTH.cid = cid || '';
  if (AUTH.cid) localStorage.setItem('refract_cid', AUTH.cid);
  else localStorage.removeItem('refract_cid');
  renderConvs();
}

async function refreshAuth() {
  try {
    const d = await (await fetch('/api/auth/me')).json();
    AUTH.user = d.logged_in ? d.user : null;
    AUTH.conversations = d.conversations || [];
  } catch { AUTH.user = null; AUTH.conversations = []; }
  renderNavAuth();
  renderConvs();
  return AUTH.user;
}

async function syncConvs() {
  if (!AUTH.user) return;
  try {
    const d = await (await fetch('/api/conversations')).json();
    AUTH.conversations = d.conversations || [];
    renderConvs();
  } catch { /* 列表拉不到不影响聊天 */ }
}

async function newConversation() {
  setCurrentConv('');
  clearChat();
  $('chatInput').focus();
}

async function delConv(cid) {
  if (!confirm('删掉这条会话？对话记录会一起删掉，恢复不了。')) return;
  const r = await fetch(`/api/conversations/${cid}`, { method: 'DELETE' });
  if (!r.ok) return;
  AUTH.conversations = AUTH.conversations.filter(c => c.id !== cid);
  if (AUTH.cid === cid) { AUTH.cid = ''; localStorage.removeItem('refract_cid'); clearChat(); }
  renderConvs();
}

async function logout() {
  try { await fetch('/api/auth/logout', { method: 'POST' }); } catch { /* 断网也要能退 */ }
  AUTH.user = null; AUTH.conversations = []; AUTH.cid = '';
  localStorage.removeItem('refract_cid');
  renderNavAuth();
  renderConvs();
  clearChat();
  closeLogin();
  goLanding(true);
}

/* ---------------- 登录弹窗 ----------------
   四种模式：验证码登录（含忘密码找回）、密码登录、注册、给已登录账号设密码。
   注册走「邮箱验证码 + 设密码」，日常登录走密码——不用每次等邮件。 */

const AM = {
  mode: 'code',
  conf: {
    code:  { title: '验证码登录', sub: '填邮箱收一个 6 位验证码，第一次登录自动建账号。',
             tip: '忘了密码也走这条路：能收信就能进，进去再设一个新密码。',
             email: 1, go: '登录', code: 1, pw: 0, pw2: 0, tabs: 1 },
    pw:    { title: '密码登录', sub: '用注册时设的密码登录。',
             tip: '密码忘了？切回「验证码」，用邮箱收个码进来重设。',
             email: 1, go: '登录', code: 0, pw: 1, pw2: 0, tabs: 1 },
    reg:   { title: '注册', sub: '邮箱收验证码，再设一个密码，以后用密码直接登。',
             tip: '注册后，对话记录和长期偏好都挂在这个账号下，换设备也在。',
             email: 1, go: '注册并登录', code: 1, pw: 1, pw2: 1, tabs: 1 },
    setpw: { title: '设置密码', sub: '给这个账号设个密码，下次不用等邮件。',
             tip: '密码只存哈希（scrypt + 随机盐），服务端看不到明文。',
             go: '设置密码', email: 0, code: 0, pw: 1, pw2: 1, tabs: 0 },
  },
};

function setAmMode(mode) {
  const c = AM.conf[mode];
  if (!c) return;
  AM.mode = mode;
  $('amTitle').textContent = c.title;
  $('amSub').textContent = c.sub;
  $('amTip').textContent = c.tip;
  $('amGo').textContent = c.go;
  $('amEmailRow').hidden = !c.email;
  $('amCodeRow').hidden = !c.code;
  $('amPwRow').hidden = !c.pw;
  $('amPw2Row').hidden = !c.pw2;
  $('amTabs').hidden = !c.tabs;
  // 改密码要先过原密码这一关，不然别人拿到一台已登录的浏览器就能把号抢走。
  // 没设过密码的账号（验证码注册的第一次）不需要
  $('amOldRow').hidden = !(mode === 'setpw' && AUTH.user && AUTH.user.has_password);
  document.querySelectorAll('#amTabs button').forEach(b => {
    b.classList.toggle('on', b.dataset.amtab === mode);
  });
  amSay('');
}

let sendTimer = null;

function openLogin(mode = 'code') {
  $('authModal').hidden = false;
  setAmMode(mode);
  setTimeout(() => {
    const f = mode === 'pw' ? $('amPw')
      : (mode === 'setpw' && !$('amOldRow').hidden) ? $('amOld')
      : $('amEmail');
    f.focus({ preventScroll: true });
  }, 60);
}

function closeLogin() {
  $('authModal').hidden = true;
  if (sendTimer) { clearInterval(sendTimer); sendTimer = null; }
}

function amSay(msg, ok = false) {
  const el = $('amMsg');
  el.textContent = msg || '';
  el.className = 'am-msg' + (ok ? ' ok' : '');
}

async function sendCode() {
  const email = $('amEmail').value.trim();
  if (!email) { amSay('先填邮箱'); return; }
  const btn = $('amSend');
  btn.disabled = true;
  try {
    const r = await fetch('/api/auth/send-code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || '发送失败');
    if (d.dev_code) {
      // 服务器没配 SMTP 的开发模式：直接把码填好，不用去翻日志
      $('amCode').value = d.dev_code;
      amSay('开发模式：验证码已自动填入', true);
    } else {
      amSay(`${Math.round((d.ttl || 300) / 60)} 分钟内有效，去邮箱收一下`, true);
    }
    let left = 60;
    btn.textContent = `${left}s`;
    sendTimer = setInterval(() => {
      left -= 1;
      if (left <= 0) {
        clearInterval(sendTimer); sendTimer = null;
        btn.disabled = false; btn.textContent = '发送验证码';
      } else btn.textContent = `${left}s`;
    }, 1000);
  } catch (e) {
    amSay(e.message);
    btn.disabled = false;
  }
}

// 登录成功后要做的事：铺工作台、接上最近一条会话
function afterLogin(d) {
  AUTH.user = d.user;
  AUTH.conversations = d.conversations || [];
  AUTH.cid = '';
  localStorage.removeItem('refract_cid');
  closeLogin();
  renderNavAuth();
  renderConvs();
  clearChat();
  goWorkbench('chat', true);
  if (AUTH.conversations.length) openConversation(AUTH.conversations[0].id);
  else $('chatInput').focus({ preventScroll: true });
}

function readPw(needConfirm) {
  const pw = $('amPw').value;
  if (pw.length < 8) throw new Error('密码至少 8 位');
  if (needConfirm && pw !== $('amPw2').value) throw new Error('两次输入的密码不一样');
  return pw;
}

async function postAuth(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || '失败');
  return d;
}

async function submitAuth() {
  const mode = AM.mode;
  const email = $('amEmail').value.trim();
  const btn = $('amGo');
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '处理中…';
  try {
    if (mode === 'setpw') {
      const body = { password: readPw(true) };
      if (!$('amOldRow').hidden) body.old_password = $('amOld').value;
      await postAuth('/api/auth/set-password', body);
      amSay('密码设好了，下次直接用密码登录', true);
      setTimeout(closeLogin, 900);
      return;
    }
    if (!email) { amSay('先填邮箱'); return; }

    let d;
    if (mode === 'reg') {
      d = await postAuth('/api/auth/register', {
        email, code: $('amCode').value.trim(), password: readPw(true),
      });
      afterLogin(d);
    } else if (mode === 'pw') {
      d = await postAuth('/api/auth/login', { email, password: $('amPw').value });
      afterLogin(d);
    } else {
      d = await postAuth('/api/auth/verify', { email, code: $('amCode').value.trim() });
      afterLogin(d);
      // 验证码进来的是新账号（还没设过密码）：顺手引导设一个，省得下次还要等邮件
      if (d.is_new) {
        setTimeout(() => openLogin('setpw'), 1200);
      }
    }
  } catch (e) {
    amSay(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

$('convNew').onclick = newConversation;
$('amSend').onclick = sendCode;
$('amGo').onclick = submitAuth;
document.querySelectorAll('#amTabs button').forEach(b => {
  b.onclick = () => setAmMode(b.dataset.amtab);
});
// 换行即提交：验证码框输完直接回车，密码框也是
['amCode', 'amPw', 'amPw2', 'amOld'].forEach(id => {
  $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') submitAuth(); });
});
$('amEmail').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  if (AM.mode === 'pw') $('amPw').focus();
  else if (AM.conf[AM.mode].code) sendCode();
  else submitAuth();
});
document.querySelectorAll('[data-auth-close]').forEach(el => { el.onclick = closeLogin; });
addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('authModal').hidden) closeLogin();
});

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
  const d = await resp.json();
  const task_id = d.task_id;
  // 会话 id 由后端定（没带就新建一条），回来要记住，侧栏高亮也跟着挪
  if (d.conversation_id) setCurrentConv(d.conversation_id);

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

const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const mqMobile = matchMedia('(max-width: 900px)');

let currentMode = 'chat';
let modeLock = false;

function modeEl(m) { return $(`mode${m[0].toUpperCase()}${m.slice(1)}`); }

function updateModesInd() {
  const ind = $('modesInd');
  const btn = document.querySelector(`#modes button[data-mode="${currentMode}"]`);
  if (!ind || !btn) return;
  ind.style.width = btn.offsetWidth + 'px';
  ind.style.transform = `translateX(${btn.offsetLeft}px)`;
}

function switchMode(mode, instant = false) {
  if (!MODES.includes(mode)) return;
  document.querySelectorAll('#modes button').forEach(b => {
    b.classList.toggle('on', b.dataset.mode === mode);
    b.setAttribute('aria-selected', b.dataset.mode === mode ? 'true' : 'false');
  });
  if (mode === currentMode) { updateModesInd(); return; }
  if (modeLock && !instant) return;
  const from = modeEl(currentMode), to = modeEl(mode);
  currentMode = mode;
  updateModesInd();
  if (instant || REDUCED) {
    MODES.forEach(m => modeEl(m).classList.toggle('on', m === mode));
    if (mode === 'chat') setTimeout(() => $('chatInput').focus({ preventScroll: true }), 60);
    return;
  }
  modeLock = true;
  from.classList.add('leaving');
  setTimeout(() => {
    from.classList.remove('on', 'leaving');
    to.classList.add('on', 'enter');
    setTimeout(() => to.classList.remove('enter'), 220);
    if (mode === 'chat') $('chatInput').focus({ preventScroll: true });
  }, 120);
  setTimeout(() => { modeLock = false; }, 220);
}

document.querySelectorAll('#modes button').forEach(btn => {
  btn.onclick = () => {
    if (document.body.dataset.view === 'workbench') switchMode(btn.dataset.mode);
    else goWorkbench(btn.dataset.mode);
  };
});
addEventListener('resize', updateModesInd);

/* ==================== 落地页 ↔ 工作台（视图与转场） ==================== */

let transitioning = false;
let landingScrollY = 0;

function applyView(view) {
  document.body.dataset.view = view;
  $('landing').hidden = view !== 'landing';
  $('workbench').hidden = view !== 'workbench';
  localStorage.setItem('refract_view', view);
}

function enterWorkbench(mode) {
  applyView('workbench');
  document.body.classList.remove('boot-landing', 'tr-in');
  switchMode(mode, true);
  updateModesInd();
  const wb = $('workbench');
  if (!REDUCED) {
    wb.classList.add('wb-enter');
    setTimeout(() => wb.classList.remove('wb-enter'), 640);
  }
  if (mode === 'chat') setTimeout(() => $('chatInput').focus({ preventScroll: true }), 80);
}

function goWorkbench(mode = 'chat', instant = false) {
  // 工作台要登录：历史、长期记忆、配额都挂账号，游客进来只会是一张空页面
  if (!AUTH.user) { openLogin(); return; }
  if (transitioning) return;
  const wasWorkbench = document.body.dataset.view === 'workbench';
  // 内联脚本可能已把 data-view 预置为 workbench（防闪烁），
  // 此时 #workbench 仍带 hidden，必须先补一次 applyView 否则两个 main 都不显示
  applyView('workbench');
  if (wasWorkbench) { switchMode(mode); return; }
  landingScrollY = scrollY;
  if (instant || REDUCED) { enterWorkbench(mode); return; }
  transitioning = true;
  document.body.classList.add('tr-out');
  setTimeout(() => {
    document.body.classList.remove('tr-out');
    enterWorkbench(mode);
    transitioning = false;
  }, 240);
}

function goLanding(instant = false) {
  if (transitioning) return;
  if (document.body.dataset.view === 'landing') { scrollTo({ top: 0, behavior: REDUCED ? 'auto' : 'smooth' }); return; }
  const done = () => {
    applyView('landing');
    document.body.classList.remove('boot-landing');  // 返回时不重播首屏编排
    $('hero').classList.add('is-in');
    if (!instant && !REDUCED) {
      document.body.classList.add('tr-in');
      setTimeout(() => document.body.classList.remove('tr-in'), 320);
    }
    scrollTo(0, landingScrollY);
    requestParallax();
  };
  if (instant || REDUCED) { done(); return; }
  transitioning = true;
  document.body.classList.add('tr-out-wb');
  setTimeout(() => {
    document.body.classList.remove('tr-out-wb');
    done();
    transitioning = false;
  }, 200);
}

document.querySelectorAll('[data-go-workbench]').forEach(b => {
  b.onclick = () => goWorkbench('chat');
});
$('brandLink').onclick = (e) => { e.preventDefault(); goLanding(); };
addEventListener('keydown', (e) => {
  if (e.altKey && e.key === 'ArrowLeft') { e.preventDefault(); goLanding(); }
});
$('skipLink').onclick = (e) => {
  e.preventDefault();
  const main = document.body.dataset.view === 'workbench' ? $('workbench') : $('landing');
  main.focus({ preventScroll: false });
};

/* ==================== 动效模块（与业务解耦） ==================== */

/* reveal：入视口 20% 渐显一次；IO 异常则全部直显，绝不藏内容 */
try {
  const io = new IntersectionObserver((es) => es.forEach(e => {
    if (e.isIntersecting) { e.target.classList.add('rv-in'); io.unobserve(e.target); }
  }), { threshold: 0.2, rootMargin: '0px 0px -8% 0px' });
  document.querySelectorAll('.rv').forEach(el => io.observe(el));
} catch {
  document.querySelectorAll('.rv').forEach(el => el.classList.add('rv-in'));
}

/* 视差：光斑 / 点阵 / 描边字 / 视频卡，共用一个 rAF，只写 transform 变量 */
const parEls = [...document.querySelectorAll('[data-par]')];
let parQueued = false;

function requestParallax() {
  if (parQueued) return;
  parQueued = true;
  requestAnimationFrame(() => {
    parQueued = false;
    if (document.hidden || document.body.dataset.view !== 'landing') return;
    const y = scrollY;
    if (!REDUCED && !mqMobile.matches) {
      parEls.forEach(el => el.style.setProperty('--py', (y * +el.dataset.par).toFixed(1) + 'px'));
    }
    $('nav').classList.toggle('is-stuck', y > 40);
    $('hero').classList.toggle('scrolled', y > 80);
  });
}
addEventListener('scroll', requestParallax, { passive: true });
mqMobile.addEventListener?.('change', () => {
  if (mqMobile.matches) parEls.forEach(el => el.style.setProperty('--py', '0px'));
  requestParallax();
});
requestParallax();

/* 数字滚动：入视口一次，easeOutCubic，计数中 accent-ink 色 */
try {
  const nums = [...document.querySelectorAll('.db-num[data-count]')];
  const dio = new IntersectionObserver((es) => es.forEach(e => {
    if (!e.isIntersecting) return;
    dio.unobserve(e.target);
    nums.forEach(el => {
      const to = +el.dataset.count;
      if (REDUCED) { el.textContent = to; return; }
      el.classList.add('counting');
      const t0 = performance.now(), dur = 1200;
      (function tick(t) {
        const p = Math.min((t - t0) / dur, 1);
        el.textContent = Math.round(to * (1 - Math.pow(1 - p, 3)));
        if (p < 1) requestAnimationFrame(tick);
        else setTimeout(() => el.classList.remove('counting'), 300);
      })(t0);
    });
  }), { threshold: 0.4 });
  if ($('databand')) dio.observe($('databand'));
} catch { /* 数字本来就写着终值，无需降级 */ }

/* hero 视频：静音自动播，离屏暂停，被拦截则静默 */
const heroVideo = $('heroVideo');
if (heroVideo) {
  heroVideo.muted = true;
  heroVideo.play?.().catch(() => {});
  try {
    new IntersectionObserver((es) => es.forEach(e => {
      if (e.isIntersecting) { if (!REDUCED) heroVideo.play().catch(() => {}); }
      else heroVideo.pause();
    }), { threshold: 0.1 }).observe(heroVideo);
  } catch { /* 忽略 */ }
  if (REDUCED) { heroVideo.removeAttribute('autoplay'); heroVideo.pause(); }
}
function watchDemo() {
  document.getElementById('demo').scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth' });
  const v = $('demoVideo');
  if (v) { v.muted = false; v.play().catch(() => {}); }
}
$('heroCard').onclick = watchDemo;
$('heroCard').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); watchDemo(); }
});

/* 文章 tab 下划线滑动 */
function updateArtTabInd() {
  const ind = $('artTabInd');
  const tab = document.querySelector('#modeArticle .tab.on');
  if (!ind || !tab) return;
  ind.style.width = tab.offsetWidth + 'px';
  ind.style.transform = `translateX(${tab.offsetLeft}px)`;
}
addEventListener('resize', updateArtTabInd);

/* ============================ 会话 ============================ */

const chatlog = $('chatlog');

function addMsg(role, html) {
  const _ce = $('chatempty'); if (_ce) _ce.hidden = true;
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

// 模型决策记录：剧本、每镜选卡、闸门拦了什么、为什么落模板。
// 用户不干涉生成过程，但要能回看模型每一步的判断——所以一条条写下来，可折叠。
function decisionRow(scope, ev) {
  if (!scope) return;
  let wrap = scope.querySelector('.dcard');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'dcard';
    wrap.innerHTML = `<div class="dc-h">模型决策记录<span class="dc-n">0</span><span class="dc-caret"></span></div>
      <div class="dc-body"></div>`;
    const flip = () => wrap.classList.toggle('folded');
    wrap.querySelector('.dc-h').onclick = flip;
    scope.appendChild(wrap);
    scope.hidden = false;
  }
  const warn = ev.status === 'fallback' || ev.status === 'warn';
  const row = document.createElement('div');
  row.className = 'dc-row' + (warn ? ' warn' : '');
  row.innerHTML = `<b>${esc(ev.label || '')}</b>`
    + (ev.lines || []).map(l => `<span>${esc(l)}</span>`).join('');
  const body = wrap.querySelector('.dc-body');
  body.appendChild(row);
  wrap.querySelector('.dc-n').textContent = body.children.length;
  if (chatlog) chatlog.scrollTop = chatlog.scrollHeight;
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
      case 'decision':
        // 模型这一步的判断（简报/剧本/选卡/闸门），用户看得到才叫「自主但可解释」
        decisionRow(flow, ev);
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
        // 模型降级要说清楚，否则用户只会觉得画面质量莫名其妙变差了
        if (ev.fallback) {
          const bar = document.createElement('div');
          bar.className = 'vfallback';
          bar.innerHTML = `⚠️ <b>${esc(ev.fallback.from)}</b> 当前不可用，已降级为 <b>${esc(ev.fallback.to)}</b> 生成`;
          flow.appendChild(bar);
        }
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
      { input: text, conversation_id: AUTH.cid || '' },
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
    syncConvs();   // 新会话/标题/时间变了，侧栏跟着刷新
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

// 打开一条会话：清空当前对话，把历史事件逐条重放一遍。
// 重放的好处是前端不用区分「这是历史还是实时」——同一套 onEvent。
async function openConversation(cid) {
  if (!cid) return;
  setCurrentConv(cid);
  clearChat();
  try {
    const r = await fetch(`/api/session/${cid}`);
    if (!r.ok) return;
    const { messages } = await r.json();
    if (!messages || !messages.length) return;
    const ce = $('chatempty');
    if (ce) ce.hidden = true;
    for (const m of messages) {
      if (m.role === 'user') {
        addMsg('user', esc(m.text).replace(/\n/g, '<br/>'));
      } else {
        const turn = makeTurn();
        for (const ev of m.events || []) turn.handle(ev);
        turn.finish();
      }
    }
    chatlog.scrollTop = chatlog.scrollHeight;
  } catch { /* 拉不到历史就当空会话，不阻塞 */ }
}

// 清掉当前这一屏消息，但把空态提示留着（新建对话时要看到引导）
function clearChat() {
  const log = $('chatlog');
  [...log.children].forEach(el => { if (el.id !== 'chatempty') el.remove(); });
  const ce = $('chatempty');
  if (ce) ce.hidden = false;
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
    updateArtTabInd();
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
      // 只写「结合这个热点写一篇」，平台交给上面的平台按钮决定。
      // 之前这里写死了「平台小红书风格」，导致选公众号/口播稿也一律按小红书写。
      $('artInput').value = `结合「${b.dataset.obj}」这个热点写一篇`;
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
    // stale = 上游此刻拉不到，后端拿五分钟内的缓存顶上来了。
    // 不提示的话用户会以为这就是最新榜单。
    if (d.stale) {
      const tip = document.createElement('p');
      tip.className = 'ph';
      tip.style.cssText = 'color:var(--warn);margin:8px 0 0;font-size:11px;line-height:1.5';
      tip.textContent = '⚠️ 上游暂时拉不到，显示的是最近一次缓存，可能不是最新';
      $('hotList').prepend(tip);
    }
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
        if (ev.type === 'decision') decisionRow($('playerBox'), ev);
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
        if (ev.type === 'fallback') {
          // 首选模型不可用、已降级——必须让用户知道，
          // 否则画面质量变了用户只会觉得「这次怎么变差了」
          const flow = document.querySelector('#playerBox .vflow');
          if (flow && !flow.querySelector('.vfallback')) {
            const bar = document.createElement('div');
            bar.className = 'vfallback';
            bar.innerHTML = `⚠️ <b>${esc(ev.from)}</b> 当前不可用，已降级为 <b>${esc(ev.to)}</b> 生成`;
            flow.insertBefore(bar, flow.firstChild);
          }
          const genEl = document.getElementById('vStepGen');
          if (genEl) { const t = genEl.querySelector('.ttool'); if (t) t.textContent = ev.to; }
        }
        if (ev.type === 'gate') {
          clearInterval(window.__vTimer);
          const gen = document.getElementById('vStepGen');
          if (gen) { gen.querySelector('.tstat').className = 'tstat ok'; }
          const gate = document.getElementById('vStepGate');
          if (gate) {
            gate.classList.remove('wait');
            gate.querySelector('.tstat').className = `tstat ${ev.ok ? 'ok' : 'err'}`;
            // ev.attempts 是逐镜头尝试明细（list[dict]），不是「第几版」——
            // 直接插值会变成 [object Object]，改成统计回炉了几个镜头
            const retries = (ev.attempts || []).filter(a => (a.attempt || 1) > 1).length;
            gate.querySelector('.ttime').textContent = retries ? `回炉 ${retries} 次` : '一次通过';
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
    // 决策记录不能随成品一起被覆盖掉——用户回头还要看模型当时怎么想的
    const decs = $('playerBox').querySelector('.dcard');
    $('playerBox').innerHTML = `
      <div class="prow">
        <a href="${done.url}" target="_blank" rel="noopener">新窗口打开</a>
        <span class="meta">${esc(done.result?.model || '')}${d ? ' · 降级模板' : ''}</span>
        <button class="ghost" id="pBack">重新生成</button>
      </div>
      <iframe src="${done.url}" title="视频成品"></iframe>`;
    if (decs) $('playerBox').appendChild(decs);
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
        if (ev.type === 'decision') decisionRow($('playerBox'), ev);
        if (ev.type === 'stage') {
          const tx = document.getElementById('pstageTx');
          if (tx) tx.textContent = ev.label + (ev.model ? ` · ${ev.model}` : '');
        }
        if (ev.type === 'fallback') {
          // 首选模型不可用、已降级——必须让用户知道，
          // 否则画面质量变了用户只会觉得「这次怎么变差了」
          const flow = document.querySelector('#playerBox .vflow');
          if (flow && !flow.querySelector('.vfallback')) {
            const bar = document.createElement('div');
            bar.className = 'vfallback';
            bar.innerHTML = `⚠️ <b>${esc(ev.from)}</b> 当前不可用，已降级为 <b>${esc(ev.to)}</b> 生成`;
            flow.insertBefore(bar, flow.firstChild);
          }
          const genEl = document.getElementById('vStepGen');
          if (genEl) { const t = genEl.querySelector('.ttool'); if (t) t.textContent = ev.to; }
        }
        if (ev.type === 'gate') {
          clearInterval(window.__vTimer);
          const gen = document.getElementById('vStepGen');
          if (gen) { gen.querySelector('.tstat').className = 'tstat ok'; }
          const gate = document.getElementById('vStepGate');
          if (gate) {
            gate.classList.remove('wait');
            gate.querySelector('.tstat').className = `tstat ${ev.ok ? 'ok' : 'err'}`;
            // ev.attempts 是逐镜头尝试明细（list[dict]），不是「第几版」——
            // 直接插值会变成 [object Object]，改成统计回炉了几个镜头
            const retries = (ev.attempts || []).filter(a => (a.attempt || 1) > 1).length;
            gate.querySelector('.ttime').textContent = retries ? `回炉 ${retries} 次` : '一次通过';
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
    const decs2 = $('playerBox').querySelector('.dcard');
    $('playerBox').innerHTML = `
      <div class="prow">
        <a href="${done.url}" target="_blank" rel="noopener">新窗口打开</a>
        <span class="meta">${esc(plan.name)} · ${esc(done.result?.model || '')}${d ? ' · 降级模板' : ''}</span>
        <button class="ghost" id="pBack">换一套</button>
      </div>
      <iframe src="${done.url}" title="视频成品"></iframe>`;
    if (decs2) $('playerBox').appendChild(decs2);
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

  // 先问服务端认不认识我：这决定了下面进落地页还是工作台
  await refreshAuth();

  const QS = new URLSearchParams(location.search);
  const qm = QS.get('mode');

  function initLanding(withBoot) {
    applyView('landing');
    if (withBoot && !REDUCED) {
      document.body.classList.add('boot-landing');
      requestAnimationFrame(() => requestAnimationFrame(() => $('hero').classList.add('is-in')));
    } else {
      $('hero').classList.add('is-in');
    }
    requestParallax();
  }

  // ?demo=1 自动跑一篇文章，用于截图自检
  if (QS.get('demo') === '1') {
    goWorkbench('article', true);
    $('artInput').value = '从 GitHub 热榜挑一个项目，写篇公众号介绍它的架构';
    setTimeout(genArticle, 500);
  } else if (qm === 'demo') {
    // 旧 ?mode=demo 深链：落到落地页演示区，而不是 404
    initLanding(false);
    setTimeout(() => document.getElementById('demo').scrollIntoView(), 60);
  } else if (MODES.includes(qm)) {
    // ?mode=chat|article|video 深链直进工作台，不播转场
    goWorkbench(qm, true);
  } else if (AUTH.user) {
    // 已登录：直接进工作台，接上次那条会话；上次的没了就挑最近一条
    goWorkbench('chat', true);
    const last = localStorage.getItem('refract_cid');
    const target = AUTH.conversations.some(c => c.id === last) ? last
      : (AUTH.conversations[0] ? AUTH.conversations[0].id : '');
    if (target) await openConversation(target);
  } else {
    initLanding(true);
  }
  loadModels();
  updateModesInd();
  updateArtTabInd();
  // 字体加载完成后胶囊/tab 指示条宽度可能变化，重算一次
  if (document.fonts?.ready) document.fonts.ready.then(() => { updateModesInd(); updateArtTabInd(); });
})();
