"""视频渲染器：生成「持续运动的 MG 动画」，不是分镜翻页。

核心转变（回应"这不就是 PPT 动态翻页么"的批评）：
之前是「分镜切页」——每个镜头一张静态排版，加转场翻过去，本质是动态 PPT。
现在改成「单镜头持续动画」——一个 20 秒的连贯画面里，十几个元素
按时间轴依次登场、持续运动（环形进度转、数字滚动、进度条生长、
日志逐条打出、波形波动、脉冲扩散），内容从 brief 动态注入。

这才是 mg-styles-15 / video-shotcraft 那种「动画」，不是翻页。

渲染模型：纯函数 drawFrame(t)。给定时间算出唯一画面，可实时播也可逐帧导出。
全程不调 LLM，零 API 费用。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from ..schemas import ContentBrief
from .storyboard import Shot, shots_to_data, storyboard
from .styles import Style, get_style, list_styles


# ============================================================
# HUD 风格：完整的持续动画（基于已验证的风格-HUD.html）
# ============================================================

_HUD_RENDER = r"""
const clamp=(v,a,b)=>v<a?a:v>b?b:v;
const seg=(t,s,d)=>{const p=(t-s)/d;return p<0?0:p>1?1:p};
const easeOut=p=>1-Math.pow(1-p,3);
const easeInOut=p=>p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;
function rnd(i){const x=Math.sin(i*127.1+311.7)*43758.5453;return x-Math.floor(x);}

const DUR = 18; // 全片 18 秒持续动画

function mono(px,w){ctx.font=(w||500)+' '+px+'px ui-monospace,SFMono-Regular,Menlo,monospace';}
function sans(px,w){ctx.font=(w||600)+' '+px+'px system-ui,-apple-system,"PingFang SC",sans-serif';}
function glow(c,b){ctx.shadowColor=c;ctx.shadowBlur=b;}
function noGlow(){ctx.shadowBlur=0;ctx.shadowColor='transparent';}

// 主题数据：从 brief 注入
const TITLE = DATA.topic || '内容生产流水线';
const HOOK = DATA.hook || '';
const SECTIONS = (DATA.shots||[]).filter(s=>s.task==='section').map(s=>s.screen_text);
const FACTS = DATA.facts || [];
const CTA = DATA.cta || '';

// 三条能力链：优先用大纲节，否则用默认
const BARS = (SECTIONS.length>=2?SECTIONS:['抓取解析','文案生成','动画产物']).slice(0,3).map((k,i)=>({
  k: ['FETCH','COMPOSE','RENDER'][i],
  zh: k,
  s: 2.2 + i*1.0,
  d: 2.4,
  col: ['#22d3ee','#4ade80','#f59e0b'][i]
}));

// trace 日志：展示执行步骤
const LOGS = [
  { s: 3.6, tag:'plan',    txt:'拆解主题 → 大纲', col:'#22d3ee' },
  { s: 4.5, tag:'compose', txt:(FACTS[0]?FACTS[0].claim:'生成正文'), col:'#4ade80' },
  { s: 5.5, tag:'reflect', txt:'校验 · 评分通过', col:'#4ade80' },
  { s: 6.5, tag:'render',  txt:'渲染动画产物', col:'#f59e0b' }
];

function drawGrid(t){
  ctx.fillStyle=STYLE.bg;ctx.fillRect(0,0,W,H);
  const g=ctx.createRadialGradient(W/2,H*.46,60,W/2,H*.46,W*.62);
  g.addColorStop(0,'rgba(20,70,60,.30)');g.addColorStop(1,'rgba(5,8,12,0)');
  ctx.fillStyle=g;ctx.fillRect(0,0,W,H);
  const cell=40,p=easeOut(seg(t,0,1.2));
  ctx.strokeStyle='rgba(64,190,150,.055)';ctx.lineWidth=1;ctx.beginPath();
  for(let x=0;x<=W;x+=cell){const h=H*p;ctx.moveTo(x+.5,H/2-h/2);ctx.lineTo(x+.5,H/2+h/2);}
  for(let y=0;y<=H;y+=cell){const w=W*p;ctx.moveTo(W/2-w/2,y+.5);ctx.lineTo(W/2+w/2,y+.5);}
  ctx.stroke();
  ctx.strokeStyle='rgba(64,190,150,.11)';ctx.beginPath();
  for(let x=0;x<=W;x+=cell*5){ctx.moveTo(x+.5,0);ctx.lineTo(x+.5,H);}
  for(let y=0;y<=H;y+=cell*5){ctx.moveTo(0,y+.5);ctx.lineTo(W,y+.5);}
  ctx.stroke();
}

function drawScan(t){
  const y=((t*190)%(H+200))-100;
  const g=ctx.createLinearGradient(0,y-90,0,y+90);
  g.addColorStop(0,'rgba(34,211,238,0)');g.addColorStop(.5,'rgba(34,211,238,.055)');g.addColorStop(1,'rgba(34,211,238,0)');
  ctx.fillStyle=g;ctx.fillRect(0,y-90,W,180);
}

function drawTopBar(t){
  const p=easeOut(seg(t,.4,.9));if(p<=0)return;
  ctx.globalAlpha=p;
  ctx.strokeStyle='rgba(120,230,190,.14)';ctx.lineWidth=1;ctx.beginPath();
  ctx.moveTo(118,74.5);ctx.lineTo(1482,74.5);ctx.stroke();
  mono(12,700);ctx.fillStyle=STYLE.accent;ctx.textAlign='left';
  ctx.fillText('REFRACT',118,58);
  mono(11.5,500);ctx.fillStyle=STYLE.dim;ctx.fillText('// '+DATA.platformLabel,190,58);
  const sec=Math.floor(t);
  mono(11.5,500);ctx.fillStyle='#a8cdd9';ctx.textAlign='right';
  ctx.fillText('T+'+String(Math.floor(sec/60)).padStart(2,'0')+':'+String(sec%60).padStart(2,'0'),1482,58);
  const blink=.45+.55*Math.abs(Math.sin(t*2.4));
  glow(STYLE.accent,12);ctx.fillStyle='rgba(74,222,128,'+blink+')';
  ctx.beginPath();ctx.arc(104,53,4,0,7);ctx.fill();noGlow();
  ctx.globalAlpha=1;ctx.textAlign='left';
}

function drawPulse(t){
  const cx=800,cy=430,base=seg(t,1.6,.8);if(base<=0)return;
  for(let i=0;i<3;i++){
    const ph=((t*.42+i/3)%1),r=150+ph*210,a=(1-ph)*.22*base;
    ctx.strokeStyle='rgba(34,211,238,'+a.toFixed(3)+')';ctx.lineWidth=1.2;
    ctx.beginPath();ctx.arc(cx,cy,r,0,7);ctx.stroke();
  }
}

function drawRing(t){
  const cx=800,cy=430,R=148,pIn=easeOut(seg(t,1.0,1.4));if(pIn<=0)return;
  const p=easeInOut(seg(t,1.8,9.0)); // 进度贯穿全片
  ctx.save();ctx.translate(cx,cy);ctx.rotate(t*.16);
  ctx.strokeStyle='rgba(120,230,190,.20)';ctx.lineWidth=1.4;
  for(let i=0;i<72;i++){const a=i/72*Math.PI*2,long=i%6===0,r1=R+22,r2=R+22+(long?13:6);
    ctx.beginPath();ctx.moveTo(Math.cos(a)*r1,Math.sin(a)*r1);ctx.lineTo(Math.cos(a)*r2,Math.sin(a)*r2);ctx.stroke();}
  ctx.restore();
  ctx.strokeStyle='rgba(120,230,190,.13)';ctx.lineWidth=2;
  ctx.beginPath();ctx.arc(cx,cy,R,0,Math.PI*2*pIn);ctx.stroke();
  const a0=-Math.PI/2,a1=a0+Math.PI*2*p*pIn;
  ctx.strokeStyle=STYLE.accent;ctx.lineWidth=3.4;ctx.lineCap='round';glow(STYLE.accent,16);
  ctx.beginPath();ctx.arc(cx,cy,R,a0,a1);ctx.stroke();
  ctx.strokeStyle='rgba(74,222,128,.55)';ctx.lineWidth=1.2;noGlow();
  ctx.beginPath();ctx.arc(cx,cy,R-14,a0,a0-Math.PI*1.4*p);ctx.stroke();
  if(p>.02){const ex=cx+Math.cos(a1)*R,ey=cy+Math.sin(a1)*R;glow(STYLE.accent,22);
    ctx.fillStyle='#b9f6ff';ctx.beginPath();ctx.arc(ex,ey,4.2,0,7);ctx.fill();noGlow();}
  const val=Math.round(p*100);
  sans(84,700);ctx.textAlign='center';ctx.textBaseline='middle';glow('#fff',18);
  ctx.fillStyle='#fff';ctx.fillText(String(val).padStart(2,'0'),cx,cy-10);noGlow();
  mono(15,600);ctx.fillStyle=STYLE.accent2;ctx.fillText('PIPELINE %',cx,cy+52);
  ctx.fillStyle=STYLE.dim;mono(11.5,500);ctx.fillText('REFRACT / CORE',cx,cy+78);
}

function drawBars(t){
  const x=118,y0=610,bw=380,gap=46;
  BARS.forEach((b,i)=>{
    const p=easeOut(seg(t,b.s,b.d)),pl=easeOut(seg(t,b.s-.8,.6));if(pl<=0)return;
    const y=y0+i*gap;
    ctx.globalAlpha=pl;mono(12.5,700);ctx.textAlign='left';ctx.textBaseline='alphabetic';
    ctx.fillStyle=b.col;ctx.fillText(b.k,x,y-8);
    sans(11.5,500);ctx.fillStyle=STYLE.dim;ctx.fillText(b.zh,x+78,y-8);ctx.globalAlpha=1;
    if(p<=0)return;
    ctx.fillStyle='rgba(255,255,255,.045)';ctx.fillRect(x,y,bw,3);
    const w=bw*p,g=ctx.createLinearGradient(x,0,x+w,0);
    g.addColorStop(0,b.col+'55');g.addColorStop(1,b.col);ctx.fillStyle=g;ctx.fillRect(x,y,w,3);
    glow(b.col,12);ctx.fillStyle=b.col;ctx.fillRect(x+w-2,y-2,3,7);noGlow();
    mono(11.5,600);ctx.fillStyle='#a8cdd9';ctx.textAlign='right';
    ctx.fillText(Math.round(p*100)+'%',x+bw+44,y+4);ctx.textAlign='left';
  });
}

function drawLog(t){
  const x=1082,y0=300,lh=34,pIn=easeOut(seg(t,3.4,.7));if(pIn<=0)return;
  ctx.strokeStyle='rgba(120,230,190,.16)';ctx.lineWidth=1;ctx.strokeRect(x-22,y0-58,436,286);
  mono(11,700);ctx.fillStyle=STYLE.dim;ctx.textAlign='left';ctx.fillText('TRACE',x-4,y0-34);
  ctx.fillStyle='rgba(120,230,190,.14)';ctx.fillRect(x-4,y0-26,396,1);
  LOGS.forEach((l,i)=>{
    const p=easeOut(seg(t,l.s,.5));if(p<=0)return;const y=y0+i*lh;
    ctx.globalAlpha=p;mono(12,700);ctx.fillStyle=l.col;ctx.fillText('▸',x-4,y);
    mono(11.5,600);ctx.fillStyle=l.col;ctx.fillText(l.tag.padEnd(8,' '),x+16,y);
    const n=Math.ceil(l.txt.length*p);ctx.fillStyle='#a8cdd9';mono(11.5,500);
    ctx.fillText(l.txt.slice(0,n),x+96,y);ctx.globalAlpha=1;
  });
  if(t>8.2){const on=Math.floor((t-8.2)*3)%2===0;if(on){ctx.fillStyle=STYLE.accent2;ctx.fillRect(x+96,y0+4*lh-11,7,13);}}
}

function drawWave(t){
  const x0=1082,y0=152,w=436,h=62,p=easeOut(seg(t,1.4,1.0));if(p<=0)return;
  ctx.strokeStyle='rgba(120,230,190,.10)';ctx.lineWidth=1;ctx.strokeRect(x0,y0,w,h);
  ctx.beginPath();
  for(let i=0;i<=w;i+=3){const u=i/w;
    const amp=(Math.sin(u*21+t*5.5)*.5+Math.sin(u*8.3-t*3.1)*.5)*Math.sin(u*Math.PI);
    const y=y0+h/2+amp*(h*.34)*p;i===0?ctx.moveTo(x0,y):ctx.lineTo(x0+i,y);}
  glow(STYLE.accent2,8);ctx.strokeStyle='rgba(74,222,128,.72)';ctx.lineWidth=1.4;ctx.stroke();noGlow();
  mono(10,700);ctx.fillStyle=STYLE.dim;ctx.textAlign='left';ctx.fillText('THROUGHPUT',x0+2,y0-10);
}

function drawTitle(t){
  const p=easeOut(seg(t,9.6,.9));if(p<=0)return;
  ctx.globalAlpha=p;
  sans(52,800);ctx.textAlign='center';glow('rgba(74,222,128,.5)',26);
  ctx.fillStyle='#fff';
  const n=Math.ceil(TITLE.length*p);ctx.fillText(TITLE.slice(0,n),800,810);
  noGlow();mono(12.5,500);ctx.fillStyle=STYLE.accent;ctx.fillText('PLAN · EXECUTE · REFLECT',800,846);
  if(HOOK){ctx.font='400 20px system-ui,"PingFang SC",sans-serif';ctx.fillStyle=STYLE.dim;
    ctx.fillText(HOOK.slice(0,Math.ceil(HOOK.length*p)),800,876);}
  ctx.globalAlpha=1;ctx.textAlign='left';
}

function drawFrame(t){
  t=clamp(t,0,DUR);
  drawGrid(t);
  drawScan(t);
  drawTopBar(t);
  drawWave(t);
  drawPulse(t);
  drawRing(t);
  drawBars(t);
  drawLog(t);
  drawTitle(t);
  noGlow();
}

// 播放控制
const fillEl=document.getElementById('fill');
const tcEl=document.getElementById('tc');
const ppEl=document.getElementById('pp');
const rpEl=document.getElementById('rp');
const fmt=s=>String(Math.floor(s/60)).padStart(2,'0')+':'+String(Math.floor(s%60)).padStart(2,'0');
let t0=performance.now(),paused=false,pausedAt=0;
function tick(now){
  const t=paused?pausedAt:((now-t0)/1000)%DUR;
  drawFrame(t);
  fillEl.style.width=(t/DUR*100)+'%';
  tcEl.textContent=fmt(t)+' / '+fmt(DUR);
  requestAnimationFrame(tick);
}
ppEl.onclick=()=>{if(paused){t0=performance.now()-pausedAt*1000;paused=false;ppEl.textContent='暂停';}else{pausedAt=Math.max(0,((performance.now()-t0)/1000)%DUR);paused=true;ppEl.textContent='播放';}};
rpEl.onclick=()=>{t0=performance.now();paused=false;ppEl.textContent='暂停';};
document.getElementById('prog').onclick=e=>{const r=e.currentTarget.getBoundingClientRect();const p=clamp((e.clientX-r.left)/r.width,0,1);t0=performance.now()-p*DUR*1000;if(paused)pausedAt=p*DUR;};
tcEl.textContent='00:00 / '+fmt(DUR);

const QS=new URLSearchParams(location.search);
if(QS.has('t')||QS.has('scene')){
  const at=QS.has('t')?parseFloat(QS.get('t')||'3'):3;
  paused=true;pausedAt=at;drawFrame(at);
  fillEl.style.width=(at/DUR*100)+'%';tcEl.textContent=fmt(at)+' / '+fmt(DUR);
  ppEl.textContent='播放';requestAnimationFrame(()=>drawFrame(at));
}else{requestAnimationFrame(tick);}
window.renderAt=at=>drawFrame(clamp(at,0,DUR));
"""


# 风格 id -> 渲染 JS（先只做 HUD 这个"真动画"，验证方向后再铺开）
_STYLE_RENDERERS = {
    "hud": _HUD_RENDER,
    "line": _HUD_RENDER,
    "pixel": _HUD_RENDER,
    "geo": _HUD_RENDER,
}


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__TITLE__</title>
<style>
  :root{--bar-bg:#0d0d12;--bar-fg:#8a8a99;--bar-line:#232330}
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{height:100%;background:var(--bar-bg);overflow:hidden;
    font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  body{display:flex;flex-direction:column}
  #stage{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;
    padding:10px;overflow:hidden}
  canvas{display:block;max-width:100%;max-height:100%;
    box-shadow:0 20px 60px -30px rgba(0,0,0,.9)}
  #bar{flex:none;height:42px;display:flex;align-items:center;gap:14px;
    padding:0 18px;background:var(--bar-bg);border-top:1px solid var(--bar-line);
    color:var(--bar-fg);font-size:12px;font-family:ui-monospace,monospace}
  #bar button{background:#1a1a24;color:#cfcfe0;border:1px solid #2c2c3a;
    border-radius:6px;padding:5px 13px;font-size:11.5px;cursor:pointer;
    font-family:inherit;transition:.15s}
  #bar button:hover{background:#262633;color:#fff}
  #prog{flex:1;height:4px;background:#232330;border-radius:2px;cursor:pointer;
    position:relative;overflow:hidden}
  #fill{height:100%;width:0;background:linear-gradient(90deg,#22d3ee,#4ade80);border-radius:2px}
  #tc{min-width:110px;text-align:right;color:#5b5b70;font-size:11px}
  #styleTag{font-size:10px;letter-spacing:.08em;color:#8a8a99}
</style>
</head>
<body>
<div id="stage"><canvas id="c" width="1600" height="900"></canvas></div>
<div id="bar">
  <button id="pp">暂停</button>
  <button id="rp">重播</button>
  <span id="styleTag">__STYLE_NAME__</span>
  <div id="prog"><div id="fill"></div></div>
  <div id="tc">00:00 / 00:00</div>
</div>
<script>
const STYLE = __STYLE__;
const DATA = __DATA__;
const cv = document.getElementById('c');
const ctx = cv.getContext('2d');
const W = 1600, H = 900;

__RENDER_JS__
</script>
</body>
</html>
"""


def _style_css(s: Style) -> str:
    return json.dumps(
        {
            "bg": s.bg,
            "bg2": s.bg2,
            "ink": s.ink,
            "dim": s.dim,
            "accent": s.accent,
            "accent2": s.accent2,
            "font": s.font,
            "radius": s.radius,
            "transitions": list(s.transitions),
        },
        ensure_ascii=False,
    )


def render_video(
    brief: ContentBrief,
    body: str,
    style_id: str | None,
    meta_line: str,
) -> str:
    style = get_style(style_id)
    shots = storyboard(brief, body)
    data = shots_to_data(brief, shots)
    data["meta"] = meta_line

    render_js = _STYLE_RENDERERS.get(style.id, _HUD_RENDER)

    page = _TEMPLATE
    page = page.replace("__TITLE__", html.escape(brief.topic))
    page = page.replace("__STYLE_NAME__", style.name)
    page = page.replace("__STYLE__", _style_css(style))
    page = page.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    page = page.replace("__RENDER_JS__", render_js)
    return page


def save_video(
    brief: ContentBrief,
    body: str,
    artifact_id: str,
    data_dir: Path,
    style_id: str | None,
    meta_line: str,
) -> Path:
    out_dir = data_dir / "videos"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{artifact_id}.html"
    from .textutil import clean_text

    path.write_text(clean_text(render_video(brief, body, style_id, meta_line)), encoding="utf-8")
    return path


def styles_meta() -> list[dict[str, object]]:
    return list_styles()
