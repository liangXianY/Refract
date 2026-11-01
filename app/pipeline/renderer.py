"""产物渲染器：把 brief + 正文变成一个可播放的动画网页。

为什么不渲染 mp4：
服务器 1.9G 内存，Remotion 要拉 Chromium，峰值 2-4G，一跑就 OOM。
而这类 motion graphics 本质是前端动画，直接输出网页反而是更好的体验——
秒开、可暂停、可拖进度、清晰度无损、体积几十 KB。
要发平台时再离线导出 mp4，那是另一条链路。
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from ..schemas import ContentBrief


def _inline_md(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return text


_SEP_RE = None  # 惰性编译：模块顶部 import re 已有


def _is_table_sep(line: str) -> bool:
    """|---|---| 形式的分隔行：整行只由 -、|、: 和空格组成，且至少有一个 -。"""
    core = line.strip()
    return bool(core.strip("|-")) is False and "-" in core and "|" in core


def _render_table(rows: list[list[str]]) -> str:
    head, *body = rows
    th = "".join(f"<th>{_inline_md(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in r) + "</tr>" for r in body)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def _img_cap_ok(alt: str) -> bool:
    """图片说明只在有信息量时显示。占位时代写下的"这里放什么图"之类不算。"""
    a = (alt or "").strip()
    return bool(a) and not re.search(r"这里放|什么图|配图位|图片", a)


def md_to_html(md: str) -> str:
    """够用就行的 Markdown 渲染。不引第三方库，减少攻击面。

    支持表格（| 语法）和配图占位（![说明](prompt:...)）。
    配图占位不是偷懒——真图片要接图像 API（花钱），先把「哪张图、放哪、
    生成提示词」落进产物，接上生成器后同一个占位直接换成 <img>。
    """
    out: list[str] = []
    list_open = False
    table_buf: list[list[str]] = []
    lines = md.splitlines()
    i = 0

    def close_list():
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    def flush_table():
        nonlocal table_buf
        if table_buf:
            out.append(_render_table(table_buf))
            table_buf = []

    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if not line:
            close_list()
            flush_table()
            continue

        # 表格：| a | b | 行 + 下一行是 |---|---| 分隔
        if line.lstrip().startswith("|") and i < len(lines) and _is_table_sep(lines[i]):
            close_list()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 1  # 跳过分隔行
            rows = [header]
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            if len(rows) >= 2 and any(len(r) >= 2 for r in rows):
                table_buf = rows
                flush_table()
                continue

        # 真图片：![说明](https://...) —— 联网搜到的图。no-referrer 防盗链绕过，
        # 加载失败整块隐藏（搜到的图可能有失效/防盗链，别让裂图毁掉版面）
        m_real = re.match(r"^!\[([^\]]*)\]\((https?://[^)]+)\)\s*$", line.strip())
        if m_real:
            close_list()
            flush_table()
            alt = _inline_md(m_real.group(1)) or "配图"
            out.append(
                '<figure class="imgreal"><img src="'
                + _inline_md(m_real.group(2))
                + '" alt="' + alt + '" loading="lazy" referrerpolicy="no-referrer" '
                + 'onerror="this.closest(\'figure\').style.display=\'none\'">'
                + ("<figcaption>" + alt + "</figcaption>" if _img_cap_ok(m_real.group(1)) else "")
                + "</figure>"
            )
            continue

        # 配图占位：![说明](prompt: 生成提示词)
        m_img = re.match(r"^!\[([^\]]*)\]\((prompt:.+)\)\s*$", line.strip())
        if m_img:
            close_list()
            flush_table()
            out.append(
                '<figure class="imgslot"><figcaption>🖼 配图位：'
                + _inline_md(m_img.group(1))
                + '</figcaption><div class="imgprompt">生成提示词：'
                + _inline_md(m_img.group(2)[7:])
                + "</div></figure>"
            )
            continue

        if line.startswith("### "):
            close_list(); flush_table()
            out.append(f"<h4>{_inline_md(line[4:])}</h4>")
        elif line.startswith("## "):
            close_list(); flush_table()
            out.append(f"<h3>{_inline_md(line[3:])}</h3>")
        elif line.startswith("# "):
            close_list(); flush_table()
            out.append(f"<h2>{_inline_md(line[2:])}</h2>")
        elif line.lstrip().startswith(("- ", "* ")):
            flush_table()
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{_inline_md(line.lstrip()[2:])}</li>")
        elif line.startswith("> "):
            close_list(); flush_table()
            out.append(f"<blockquote>{_inline_md(line[2:])}</blockquote>")
        else:
            close_list()
            flush_table()
            out.append(f"<p>{_inline_md(line)}</p>")
    close_list()
    flush_table()
    return "\n".join(out)


def _title_of(brief: ContentBrief) -> str:
    return brief.topic


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__TITLE__</title>
<style>
  :root{
    --bg:#08080c; --ink:#ffffff; --dim:#8b8b9e; --line:rgba(255,255,255,.11);
    --accent:#6630f8; --accent2:#9a7bff;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{height:100%;background:var(--bg);color:var(--ink);
    font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    overflow:hidden;-webkit-font-smoothing:antialiased}
  #app{position:fixed;inset:0}

  /* ---------- 背景 ---------- */
  .bg{position:absolute;inset:0;background:
     radial-gradient(1100px 620px at 22% 12%, rgba(102,48,248,.22), transparent 62%),
     radial-gradient(900px 540px at 84% 86%, rgba(120,70,255,.14), transparent 66%),
     var(--bg)}
  .grid{position:absolute;inset:0;opacity:.34;
     background-image:linear-gradient(rgba(255,255,255,.05) 1px,transparent 1px),
                      linear-gradient(90deg,rgba(255,255,255,.05) 1px,transparent 1px);
     background-size:46px 46px;
     mask-image:radial-gradient(circle at 50% 46%,#000 8%,transparent 74%);
     -webkit-mask-image:radial-gradient(circle at 50% 46%,#000 8%,transparent 74%)}
  #dots{position:absolute;inset:0;opacity:.55}
  .vignette{position:absolute;inset:0;
     background:radial-gradient(circle at 50% 50%,transparent 52%,rgba(0,0,0,.7) 100%)}

  /* ---------- 场景 ---------- */
  .scene{position:absolute;inset:0;display:flex;flex-direction:column;
     justify-content:center;align-items:center;padding:0 8vw 44px;
     opacity:0;visibility:hidden;transition:opacity .5s ease,visibility .5s}
  .scene.on{opacity:1;visibility:visible}

  .kicker{font:600 12px/1 ui-monospace,monospace;letter-spacing:.34em;
     color:var(--accent2);text-transform:uppercase;margin-bottom:26px}
  .h1{font-size:clamp(26px,4.4vw,54px);font-weight:800;letter-spacing:-.01em;
     line-height:1.26;text-align:center;max-width:13em}
  .h1 em{font-style:normal;background:linear-gradient(96deg,#fff 6%,var(--accent2) 58%,#fff 96%);
     -webkit-background-clip:text;background-clip:text;color:transparent}
  .sub{margin-top:38px;font-size:clamp(14px,1.5vw,20px);color:var(--dim);
     max-width:44ch;text-align:center;line-height:1.75}

  /* 逐字 */
  .ch{display:inline-block;opacity:0;transform:translateY(.5em);
     animation:chIn .62s cubic-bezier(.22,1,.36,1) forwards}
  /* 静态自检模式：截图/无动画环境下强制显示所有元素，用来验证布局 */
  [data-static] .ch,[data-static] .card,[data-static] .fact,[data-static] .num{
     animation:none !important;opacity:1 !important;transform:none !important}
  [data-static] .card::after{display:none}
  /* 渐变文字靠 background-clip:text 实现，无头截图管线渲染不出来，自检时退回纯色 */
  [data-static] .h1 em,[data-static] .num b{
     background:none !important;-webkit-background-clip:border-box !important;
     background-clip:border-box !important;color:#fff !important}
  @keyframes chIn{to{opacity:1;transform:none}}

  /* 卡片 */
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(212px,1fr));
     gap:16px;width:min(1080px,92vw);margin-top:10px}
  .card{position:relative;border:1px solid var(--line);border-radius:14px;
     padding:20px 18px;background:linear-gradient(158deg,rgba(255,255,255,.06),rgba(255,255,255,.012));
     opacity:0;transform:translateY(26px) rotateX(9deg);transform-origin:top center;
     animation:cardIn .7s cubic-bezier(.22,1,.36,1) forwards;overflow:hidden}
  @keyframes cardIn{to{opacity:1;transform:none}}
  .card::after{content:"";position:absolute;top:0;left:-60%;width:45%;height:100%;
     background:linear-gradient(100deg,transparent,rgba(255,255,255,.09),transparent);
     animation:sweep 3.4s ease-in-out infinite}
  @keyframes sweep{0%{left:-60%}60%{left:130%}100%{left:130%}}
  .card .no{font:700 11px/1 ui-monospace,monospace;color:var(--accent2);
     letter-spacing:.2em;margin-bottom:10px}
  .card h3{font-size:16px;font-weight:750;line-height:1.4;margin-bottom:10px}
  .card p{font-size:12.5px;line-height:1.72;color:var(--dim)}

  /* 数字 */
  .nums{display:flex;gap:clamp(30px,6vw,86px);margin-top:8px;flex-wrap:wrap;justify-content:center}
  .num{text-align:center;opacity:0;animation:cardIn .7s cubic-bezier(.22,1,.36,1) forwards}
  .num b{display:block;font-size:clamp(34px,5.4vw,64px);font-weight:800;
     letter-spacing:-.02em;background:linear-gradient(180deg,#fff,#a58cff);
     -webkit-background-clip:text;background-clip:text;color:transparent}
  .num span{display:block;margin-top:8px;font-size:12px;color:var(--dim);letter-spacing:.12em}

  .facts{width:min(780px,92vw);margin-top:4px;display:grid;gap:13px}
  .fact{display:flex;gap:15px;align-items:flex-start;border:1px solid var(--line);
     border-left:2px solid var(--accent);border-radius:12px;padding:17px 20px;
     background:linear-gradient(158deg,rgba(255,255,255,.055),rgba(255,255,255,.012));
     opacity:0;animation:cardIn .6s ease forwards}
  .fact .dot{width:6px;height:6px;border-radius:50%;background:var(--accent2);
     margin-top:7px;flex:none;box-shadow:0 0 10px var(--accent)}
  .fact .tx{font-size:14px;line-height:1.7}
  .fact .src{display:block;margin-top:5px;font:11px/1.4 ui-monospace,monospace;color:#6c6c82}
  .fact .src.warn{color:#ffb347}

  /* ---------- 控制条 ---------- */
  .hud{position:absolute;left:0;right:0;bottom:0;padding:16px 26px 20px;
     display:flex;align-items:center;gap:16px;background:linear-gradient(transparent,rgba(0,0,0,.55))}
  .track{flex:1;height:2px;background:rgba(255,255,255,.12);border-radius:2px;overflow:hidden}
  .track i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2))}
  .btn{font:600 11.5px/1 ui-monospace,monospace;letter-spacing:.1em;color:#cfcfe0;
     border:1px solid var(--line);background:rgba(255,255,255,.04);border-radius:7px;
     padding:8px 13px;cursor:pointer;transition:.2s}
  .btn:hover{color:#fff;border-color:rgba(154,123,255,.6);background:rgba(102,48,248,.18)}
  .stamp{font:600 11px/1 ui-monospace,monospace;letter-spacing:.14em;color:#5b5b70}

  /* ---------- 全文视图 ---------- */
  #doc{position:absolute;inset:0;background:var(--bg);overflow:auto;padding:56px 20px 90px;
     display:none}
  #doc.on{display:block}
  #doc .wrap{max-width:720px;margin:0 auto}
  #doc h2{font-size:26px;margin:0 0 18px;line-height:1.35}
  #doc h3{font-size:18px;margin:34px 0 12px;color:#e9e6ff}
  #doc h4{font-size:15px;margin:22px 0 8px}
  #doc p{font-size:15px;line-height:1.95;color:#c9c9d8;margin:11px 0}
  #doc li{font-size:15px;line-height:1.9;color:#c9c9d8;margin-left:22px}
  #doc blockquote{border-left:2px solid var(--accent);padding-left:14px;color:#9c9cb2;
     margin:16px 0;font-size:15px;line-height:1.8}
  #doc .meta{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
     font:11.5px/1.9 ui-monospace,monospace;color:#5b5b70}
  .tag{display:inline-block;padding:3px 9px;border:1px solid var(--line);border-radius:99px;
     font:10.5px/1 ui-monospace,monospace;letter-spacing:.1em;color:#8b8b9e;margin-right:7px}

  @media(max-width:640px){
    .scene{padding:0 6vw}
    .cards{grid-template-columns:1fr;max-height:60vh;overflow:auto}
    .card{padding:15px}
    .hud{padding:12px 14px 14px}
  }
</style>
</head>
<body>
<div id="app">
  <div class="bg"></div>
  <div class="grid"></div>
  <canvas id="dots"></canvas>
  <div class="vignette"></div>
  <div id="stage"></div>

  <div class="hud">
    <span class="stamp" id="stamp">00:00</span>
    <div class="track"><i id="fill"></i></div>
    <button class="btn" id="toggle">暂停</button>
    <button class="btn" id="replay">重播</button>
    <button class="btn" id="docBtn">看全文</button>
  </div>

  <div id="doc"><div class="wrap" id="docBody"></div></div>
</div>

<script>
const QS = new URLSearchParams(location.search);
const STATIC = QS.has('static');
const FIXED = QS.has('scene') ? Math.max(0, Math.min(3, parseInt(QS.get('scene'), 10) || 0)) : -1;
if (STATIC) document.documentElement.dataset.static = '1';
const DATA = __DATA__;
const DUR = [3800, 5200, 4600, 3600];
const stage = document.getElementById('stage');
const fill = document.getElementById('fill');
const stamp = document.getElementById('stamp');
let idx = -1, playing = true, t0 = 0, elapsedBefore = 0, raf = 0;

function chars(text, startDelay){
  return [...text].map((c,i)=>
    `<span class="ch" style="animation-delay:${(startDelay + i*0.028).toFixed(3)}s">${c === ' ' ? '&nbsp;' : c}</span>`
  ).join('');
}

function buildScenes(){
  const s = [];

  // 1 封面
  s.push(`<section class="scene">
    <div class="kicker">${DATA.platformLabel}</div>
    <h1 class="h1"><em>${chars(DATA.topic, 0.15)}</em></h1>
    <p class="sub">${DATA.hook}</p>
  </section>`);

  // 2 方案卡
  const cards = DATA.sections.map((sec,i)=>`
    <div class="card" style="animation-delay:${0.25 + i*0.16}s">
      <div class="no">0${i+1}</div>
      <h3>${sec.heading}</h3>
      <p>${(sec.points||[]).slice(0,2).join(' · ')}</p>
    </div>`).join('');
  s.push(`<section class="scene">
    <div class="kicker">内容结构</div>
    <p class="h1" style="font-size:clamp(20px,2.6vw,32px);margin-bottom:34px">这篇稿子这样搭</p>
    <div class="cards">${cards}</div>
  </section>`);

  // 3 数据
  const facts = DATA.facts.length ? DATA.facts : [{claim:'本篇未引用外部数据', source:null}];
  const fl = facts.slice(0,4).map((f,i)=>`
    <div class="fact" style="animation-delay:${0.2 + i*0.15}s">
      <div class="dot"></div>
      <div class="tx">${f.claim}
        <span class="src ${f.source ? '' : 'warn'}">${f.source ? '来源：' + f.source : '⚠ 无出处，已在正文中弱化表述'}</span>
      </div>
    </div>`).join('');
  s.push(`<section class="scene">
    <div class="kicker">论据</div>
    <p class="h1" style="font-size:clamp(19px,2.3vw,29px);margin-bottom:28px">哪些数据有出处，哪些没有</p>
    <div class="facts">${fl}</div>
  </section>`);

  // 4 结尾
  s.push(`<section class="scene">
    <div class="kicker">完</div>
    <h1 class="h1" style="font-size:clamp(22px,3.4vw,42px)">${DATA.cta || '感谢阅读'}</h1>
    <p class="sub" style="font-size:13px;letter-spacing:.06em">
      ${DATA.meta}
    </p>
  </section>`);

  stage.innerHTML = s.join('');
  return [...stage.querySelectorAll('.scene')];
}

let scenes = [];

function show(i){
  scenes.forEach((el,k)=>el.classList.toggle('on', k===i));
  // 重新触发动画
  const el = scenes[i];
  el.querySelectorAll('[class*=ch],.card,.fact,.num').forEach(n=>{
    n.style.animation='none'; void n.offsetWidth; n.style.animation='';
  });
}

function fmt(ms){
  const s = Math.floor(ms/1000);
  return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
}

function loop(now){
  if(!playing) return;
  if(!t0) t0 = now;
  const total = DUR.reduce((a,b)=>a+b,0);
  const t = (elapsedBefore + (now - t0)) % total;
  let acc = 0, cur = 0;
  for(let i=0;i<DUR.length;i++){
    if(t < acc + DUR[i]){ cur = i; break; }
    acc += DUR[i];
  }
  if(cur !== idx){ idx = cur; show(cur); }
  fill.style.width = (t/total*100)+'%';
  stamp.textContent = fmt(t)+' / '+fmt(total);
  raf = requestAnimationFrame(loop);
}

function pause(){
  playing = false;
  cancelAnimationFrame(raf);
  elapsedBefore += performance.now() - t0; t0 = 0;
  document.getElementById('toggle').textContent = '播放';
}
function play(){
  playing = true;
  document.getElementById('toggle').textContent = '暂停';
  raf = requestAnimationFrame(loop);
}

document.getElementById('toggle').onclick = ()=> playing ? pause() : play();
document.getElementById('replay').onclick = ()=>{
  elapsedBefore = 0; t0 = 0; idx = -1;
  if(!playing) play();
};
document.getElementById('docBtn').onclick = ()=>{
  const d = document.getElementById('doc');
  d.classList.toggle('on');
  document.getElementById('docBtn').textContent = d.classList.contains('on') ? '看动画' : '看全文';
};

// ---------- 粒子背景 ----------
(function dots(){
  const c = document.getElementById('dots');
  const ctx = c.getContext('2d');
  let w,h,pts;
  function resize(){
    w = c.width = innerWidth * devicePixelRatio;
    h = c.height = innerHeight * devicePixelRatio;
    c.style.width = innerWidth+'px'; c.style.height = innerHeight+'px';
    pts = Array.from({length: innerWidth < 700 ? 34 : 62}, ()=>({
      x: Math.random()*w, y: Math.random()*h,
      vx:(Math.random()-.5)*.16*devicePixelRatio, vy:(Math.random()-.5)*.16*devicePixelRatio
    }));
  }
  resize(); addEventListener('resize', resize);
  function frame(){
    ctx.clearRect(0,0,w,h);
    const R = 132*devicePixelRatio;
    for(const p of pts){
      p.x+=p.vx; p.y+=p.vy;
      if(p.x<0||p.x>w) p.vx*=-1;
      if(p.y<0||p.y>h) p.vy*=-1;
    }
    for(let i=0;i<pts.length;i++){
      for(let j=i+1;j<pts.length;j++){
        const dx=pts[i].x-pts[j].x, dy=pts[i].y-pts[j].y;
        const d=Math.hypot(dx,dy);
        if(d<R){
          ctx.strokeStyle='rgba(120,90,230,'+(0.16*(1-d/R))+')';
          ctx.lineWidth=1*devicePixelRatio;
          ctx.beginPath(); ctx.moveTo(pts[i].x,pts[i].y); ctx.lineTo(pts[j].x,pts[j].y); ctx.stroke();
        }
      }
      ctx.fillStyle='rgba(160,140,255,.5)';
      ctx.beginPath(); ctx.arc(pts[i].x,pts[i].y,1.5*devicePixelRatio,0,7); ctx.fill();
    }
    requestAnimationFrame(frame);
  }
  frame();
})();

scenes = buildScenes();
document.getElementById('docBody').innerHTML = DATA.bodyHtml + '<div class="meta">' + DATA.meta + '</div>';
show(FIXED >= 0 ? FIXED : 0); idx = FIXED >= 0 ? FIXED : 0;
if (!STATIC && FIXED < 0) raf = requestAnimationFrame(loop);
</script>
</body>
</html>
"""


def render_artifact(brief: ContentBrief, body: str, artifact_id: str, meta_line: str) -> str:
    platform_label = {"wechat": "公众号长文", "xiaohongshu": "小红书图文", "video": "视频口播稿"}.get(brief.platform, "内容")

    data = {
        "topic": brief.topic,
        "hook": brief.hook,
        "platformLabel": platform_label,
        "sections": [{"heading": s.heading, "points": s.points} for s in brief.outline],
        "facts": [{"claim": f.claim, "source": f.source} for f in brief.facts],
        "cta": brief.cta,
        "bodyHtml": md_to_html(body),
        "meta": meta_line,
    }

    page = _TEMPLATE.replace("__TITLE__", html.escape(_title_of(brief)))
    page = page.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    return page


def save_artifact(brief: ContentBrief, body: str, artifact_id: str, data_dir: Path, meta_line: str) -> Path:
    from .textutil import clean_text

    out_dir = data_dir / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{artifact_id}.html"
    path.write_text(clean_text(render_artifact(brief, body, artifact_id, meta_line)), encoding="utf-8")
    return path


def render_article_page(brief: ContentBrief, body: str, meta_line: str) -> str:
    """文章的「阅读页」。

    之前 /r/{id} 打开的是动画页——产物混淆的根源：文章的阅读产物和动画产物
    是两个东西，必须分开存、分开看。这里只负责把成稿排版成能读的页面。
    """
    import html as _html

    title = _html.escape(brief.topic)
    meta = _html.escape(
        " · ".join(
            x
            for x in [
                brief.platform,
                brief.audience,
                f"{len(body)} 字",
                brief.tone,
                meta_line,
            ]
            if x
        )
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body{{margin:0;background:#fbfbf9;color:#111211;
    font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;
    line-height:1.9;-webkit-font-smoothing:antialiased}}
  main{{max-width:720px;margin:0 auto;padding:56px 22px 80px}}
  .kicker{{font-size:11px;letter-spacing:.18em;color:#12a37f;font-weight:700;margin-bottom:14px}}
  h1{{font-size:26px;letter-spacing:-.01em;line-height:1.4;margin:0 0 10px}}
  .meta{{font-size:12px;color:#858783;border-bottom:1px solid #e6e6e2;padding-bottom:18px;margin-bottom:30px}}
  .body{{font-size:15.5px}}
  .body > p:first-of-type{{font-size:17px;color:#1c1e1c;line-height:1.85;
    padding-bottom:18px;border-bottom:1px solid #eeeee9;margin-bottom:26px}}
  .body h2{{font-size:20px;margin:42px 0 16px;letter-spacing:-.01em;position:relative;
    padding-left:14px;line-height:1.5}}
  .body h2::before{{content:"";position:absolute;left:0;top:7px;bottom:7px;width:3px;
    border-radius:2px;background:#12a37f}}
  .body h3,.body h4{{font-size:16.5px;margin:28px 0 10px}}
  .body p{{margin:0 0 18px}}
  .body li{{margin:0 0 9px}}
  .body li::marker{{color:#12a37f}}
  .body strong{{color:#000;font-weight:700}}
  .body blockquote{{margin:20px 0;padding:12px 18px;border-left:3px solid #12a37f;
    background:#f6f8f7;border-radius:0 8px 8px 0;color:#3f423f}}
  .body hr{{border:0;border-top:1px solid #e6e6e2;margin:36px auto;width:64px}}
  .body table{{width:100%;border-collapse:collapse;margin:20px 0 26px;font-size:13.5px}}
  .body th{{text-align:left;background:#f2f4f2;padding:9px 12px;font-weight:700;
    border-bottom:2px solid #12a37f;white-space:nowrap}}
  .body td{{padding:9px 12px;border-bottom:1px solid #eeeee9;vertical-align:top}}
  .body tr:hover td{{background:#fafbfa}}
  .body figure.imgslot{{margin:24px 0;padding:14px 16px;border:1px dashed #c8d6d0;
    border-radius:10px;background:#f8faf9}}
  .body figure.imgslot figcaption{{font-size:13px;font-weight:600;color:#0d7a5f;margin-bottom:6px}}
  .body figure.imgreal{{margin:26px 0}}
  .body figure.imgreal img{{width:100%;border-radius:10px;border:1px solid #e6e6e2;display:block}}
  .body figure.imgreal figcaption{{font-size:11px;color:#a8aaa5;margin-top:7px;text-align:center}}
  .body figure.imgslot .imgprompt{{font-size:11.5px;color:#858783;font-family:ui-monospace,Consolas,monospace;
    line-height:1.7;word-break:break-all}}
  footer{{margin-top:52px;padding-top:16px;border-top:1px solid #e6e6e2;
    font-size:11px;color:#a8aaa5;font-family:ui-monospace,Consolas,monospace}}
</style></head><body><main>
  <div class="kicker">REFRACT · ARTICLE</div>
  <h1>{title}</h1>
  <div class="meta">{meta}</div>
  <div class="body">{md_to_html(body)}</div>
  <footer>{_html.escape(meta_line)} · 由 Refract 生成</footer>
</main></body></html>"""


def save_article(brief: ContentBrief, body: str, artifact_id: str, data_dir: Path, meta_line: str) -> Path:
    """文章阅读页，存 artifacts/{id}.html。动画产物走 /v/{id}，别再混。"""
    from .textutil import clean_text

    out_dir = data_dir / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{artifact_id}.html"
    path.write_text(clean_text(render_article_page(brief, body, meta_line)), encoding="utf-8")
    return path
