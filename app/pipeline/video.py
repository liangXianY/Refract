"""视频渲染器：把 brief + 分镜 + 风格，变成一个带完整质感的 canvas 动画网页。

这是「视频制作」主模块的核心，全功能实现。

架构：
- 每个风格是一段完整的 JS 渲染代码（含质感：像素的 buffer、线稿的手抖、HUD 的网格扫描线）
- 通用部分（播放控制、时间轴、分镜驱动、标题换行）抽成公共 JS
- 风格渲染函数接收 (ctx, W, H, DATA, shot, t)，从 DATA 动态取内容，不硬编码

风格渲染模型：纯函数式 drawFrame(t)。给定时间 t 算出唯一画面，无随机、无累积状态。
所以既能实时播放，也能被 headless 逐帧截图导出 mp4。

本模块只做「网页动画产物」，不调任何 LLM（分镜由 storyboard.py 规则生成），
因此全程不产生 API 费用。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from ..schemas import ContentBrief
from .storyboard import Shot, shots_to_data, storyboard
from .styles import Style, get_style, list_styles

# ============================================================
# 通用 JS：播放控制 + 时间轴 + 分镜驱动 + 文本工具
# ============================================================

_COMMON_JS = r"""
const clamp=(v,a,b)=>v<a?a:v>b?b:v;
const seg=(t,s,d)=>{const p=(t-s)/d;return p<0?0:p>1?1:p};
const easeOut=p=>1-Math.pow(1-p,3);
const easeInOut=p=>p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;
function rnd(i){const x=Math.sin(i*127.1+311.7)*43758.5453;return x-Math.floor(x);}

const SHOTS = DATA.shots;
const SHOT_TIMES = [];
let acc = 0;
SHOTS.forEach(s=>{SHOT_TIMES.push([acc, acc+s.duration]); acc+=s.duration;});
const DUR = acc;

function currentShot(t){
  let idx = 0;
  for(let i=0;i<SHOT_TIMES.length;i++){
    if(t>=SHOT_TIMES[i][0] && t<SHOT_TIMES[i][1]){idx=i;break;}
    if(t>=SHOT_TIMES[i][1]) idx=i;
  }
  return {idx, shot: SHOTS[idx], st: SHOT_TIMES[idx][0]};
}

// 长标题自动换行，逐字浮现
function drawTitle(str, cx, cy, size, weight, color, p, mono){
  if(p<=0) return;
  ctx.font=(weight||800)+' '+size+'px '+F;
  const maxW = W*0.78;
  const n = Math.ceil(str.length*p);
  const shown = str.slice(0,n);
  const lines=[]; let cur='';
  for(const ch of shown){
    const test=cur+ch;
    if(ctx.measureText(test).width>maxW && cur){lines.push(cur);cur=ch;}else cur=test;
  }
  if(cur)lines.push(cur);
  ctx.fillStyle=color||STYLE.ink;
  ctx.textAlign='center'; ctx.textBaseline='middle';
  const lh=size*1.3; const y0=cy-(lines.length-1)*lh/2;
  lines.forEach((ln,i)=>ctx.fillText(ln,cx,y0+i*lh));
}

// 播放控制
const fillEl=document.getElementById('fill');
const tcEl=document.getElementById('tc');
const ppEl=document.getElementById('pp');
const rpEl=document.getElementById('rp');
const fmt=s=>String(Math.floor(s/60)).padStart(2,'0')+':'+String(Math.floor(s%60)).padStart(2,'0');
let t0=performance.now(), paused=false, pausedAt=0;
function tick(now){
  const t=paused?pausedAt:((now-t0)/1000)%DUR;
  drawFrame(t);
  fillEl.style.width=(t/DUR*100)+'%';
  tcEl.textContent=fmt(t)+' / '+fmt(DUR);
  requestAnimationFrame(tick);
}
ppEl.onclick=()=>{
  if(paused){t0=performance.now()-pausedAt*1000;paused=false;ppEl.textContent='暂停';}
  else{pausedAt=Math.max(0,((performance.now()-t0)/1000)%DUR);paused=true;ppEl.textContent='播放';}
};
rpEl.onclick=()=>{t0=performance.now();paused=false;ppEl.textContent='暂停';};
document.getElementById('prog').onclick=e=>{
  const r=e.currentTarget.getBoundingClientRect();
  const p=clamp((e.clientX-r.left)/r.width,0,1);
  t0=performance.now()-p*DUR*1000;
  if(paused)pausedAt=p*DUR;
};
tcEl.textContent='00:00 / '+fmt(DUR);
window.renderAt=at=>{drawFrame(clamp(at,0,DUR));};
"""

# 启动逻辑放在 render JS 之后（因为 render JS 里可能有 const 声明，drawFrame 依赖它们）
_BOOT_JS = r"""
const QS=new URLSearchParams(location.search);
const HAS_T=QS.has('t');
const FIXED=QS.has('scene')?Math.max(0,parseInt(QS.get('scene'),10)||0):-1;
if(HAS_T||FIXED>=0){
  const at=FIXED>=0?SHOT_TIMES[FIXED][0]+1.5:parseFloat(QS.get('t')||'1.5');
  paused=true;pausedAt=at;
  drawFrame(at);
  fillEl.style.width=(at/DUR*100)+'%';
  tcEl.textContent=fmt(at)+' / '+fmt(DUR);
  ppEl.textContent='播放';
  requestAnimationFrame(()=>drawFrame(at));
}else{
  requestAnimationFrame(tick);
}
"""

# ============================================================
# 像素风渲染 JS（buffer 放大，真 8-bit 质感）
# ============================================================

_PIXEL_RENDER = r"""
// 像素风：低分辨率 buffer 放大 4 倍，每一笔都是方块
const BW=400, BH=225;
const buf=document.createElement('canvas');
buf.width=BW; buf.height=BH;
const b=buf.getContext('2d');
b.imageSmoothingEnabled=false;
function px(x,y,w,h,col){b.fillStyle=col;b.fillRect(Math.round(x),Math.round(y),Math.round(w),Math.round(h));}

function drawFrame(t){
  t=clamp(t,0,DUR);
  const {idx,shot,st}=currentShot(t);

  // 背景：按镜头任务给不同底色
  let bg=STYLE.bg, bg2=STYLE.bg2;
  if(shot.task==='cover'){bg='#1a1c2c';bg2='#232842';}
  if(shot.task==='section'){bg='#1a1c2c';bg2='#29366f';}
  if(shot.task==='facts'){bg='#1a1c2c';bg2='#2a3048';}
  if(shot.task==='ending'){bg='#10121c';bg2='#1a1c2c';}

  px(0,0,BW,BH,bg);
  // 天空渐变（横向色带）
  for(let i=0;i<12;i++){
    b.globalAlpha=(1-i/12)*.5;
    px(0,i*18,BW,18,bg2);
    b.globalAlpha=1;
  }
  // 星空
  for(let i=0;i<40;i++){
    const sx=Math.round(rnd(i*3)*BW), sy=Math.round(rnd(i*3+1)*120);
    const tw=.5+.5*Math.sin(t*2.2+rnd(i*3+2)*6.28);
    b.globalAlpha=tw;
    px(sx,sy,1,1,i%7===0?STYLE.accent:STYLE.ink);
    b.globalAlpha=1;
  }
  // 地面
  const gp=easeOut(seg(t,st,.8));
  if(gp>0){
    px(0,168,BW,58*gp,STYLE.accent2);
    px(0,168+32*gp,BW,26*gp,'#257179');
  }

  // 内容区：按任务画（在 buffer 上，先不画文字，文字放大后画避免模糊）
  // CRT 扫描线
  b.fillStyle='rgba(0,0,0,.16)';
  for(let y=0;y<BH;y+=3)b.fillRect(0,y,BW,1);

  // 放大到主画布
  ctx.imageSmoothingEnabled=false;
  ctx.clearRect(0,0,W,H);
  ctx.drawImage(buf,0,0,BW,BH,0,0,W,H);

  // 底部标题条：必须在 drawImage 之后画，否则会被 buffer 覆盖
  drawPixelContent(t, idx, shot, st);
}

function drawPixelContent(t, idx, shot, st){
  // 底部标题条（高分辨率画在放大之后，避免过粗）
  const p=easeOut(seg(t,st,.8));
  if(p<=0)return;

  ctx.fillStyle='rgba(10,11,18,.94)';
  ctx.fillRect(0,812,W,88);
  ctx.fillStyle=STYLE.accent2;
  ctx.fillRect(0,809,W,3);

  ctx.textAlign='center';ctx.textBaseline='middle';ctx.imageSmoothingEnabled=false;
  const txt=shot.screen_text;
  const n=Math.ceil(txt.length*p);
  ctx.font='700 52px ui-monospace,"Courier New",monospace';
  ctx.fillStyle=STYLE.accent;
  ctx.fillText(txt.slice(0,n),W/2,846);

  // 副标题：任务标签 + narration
  const p2=easeOut(seg(t,st+.4,.6));
  if(p2>0){
    ctx.font='500 18px ui-monospace,"Courier New",monospace';
    ctx.fillStyle=STYLE.ink;
    const sub=(shot.narration||'').slice(0, Math.ceil((shot.narration||'').length*p2));
    ctx.fillText(sub, W/2, 880);
  }
  // 镜头编号
  ctx.font='700 16px ui-monospace,monospace';
  ctx.fillStyle=STYLE.accent2;
  ctx.textAlign='left';
  ctx.fillText(shot.shot_id, 40, 60);
  ctx.textAlign='center';
}
"""

# ============================================================
# 线稿渲染 JS（手抖线条 + 纸纹）
# ============================================================

_LINE_RENDER = r"""
function wob(i,t,amp){return (Math.sin(i*2.13+t*7.3)+Math.sin(i*0.71-t*4.1)*.5)*amp;}

function drawFrame(t){
  t=clamp(t,0,DUR);
  const {idx,shot,st}=currentShot(t);

  // 纸底 + 纸纹（噪点加大，alpha 提高，让纸张质感可见）
  ctx.fillStyle=STYLE.bg;
  ctx.fillRect(0,0,W,H);
  const pp=easeOut(seg(t,st,1.0));
  ctx.fillStyle='rgba(120,110,95,'+(0.10*pp).toFixed(3)+')';
  for(let i=0;i<3200;i++){
    const nx=rnd(i*2)*W, ny=rnd(i*2+1)*H;
    const ns=(rnd(i*3)>0.7?1.8:1.1);
    ctx.fillRect(nx,ny,ns,ns);
  }
  // 四周暗角
  const vg=ctx.createRadialGradient(W/2,H/2,H*.35,W/2,H/2,H*1.05);
  vg.addColorStop(0,'rgba(0,0,0,0)');vg.addColorStop(1,'rgba(90,80,65,.03)');
  ctx.fillStyle=vg;ctx.fillRect(0,0,W,H);

  // 顶部标题（手绘感）
  const pt=easeOut(seg(t,st,.7));
  if(pt>0){
    drawTitle(shot.screen_text, W/2, H*0.32, 54, 800, STYLE.ink, pt, false);
  }
  // 任务编号
  const pn=easeOut(seg(t,st+.3,.5));
  if(pn>0){
    ctx.font='700 16px ui-monospace,monospace';
    ctx.fillStyle=STYLE.accent;
    ctx.textAlign='center';
    ctx.fillText(shot.shot_id, W/2, H*0.22);
    ctx.textAlign='left';
  }

  // 手绘下划线（生长 + 明显手抖）
  const pu=easeInOut(seg(t,st+.6,.8));
  if(pu>0){
    ctx.strokeStyle=STYLE.accent;
    ctx.lineWidth=3.2;
    ctx.lineCap='round';
    ctx.beginPath();
    const y=H*0.40, x0=W*0.28, x1=W*0.72;
    const cx=x0+(x1-x0)*pu;
    // 分段画，每段加手抖，模拟真实钢笔
    ctx.moveTo(x0+wob(0,t,2.5), y+wob(1,t,2.5));
    const segN=6;
    for(let k=1;k<=segN;k++){
      const q=k/segN*pu;
      const sx=x0+(x1-x0)*Math.min(1,q);
      ctx.lineTo(sx+wob(k*3,t,3.0), y+wob(k*3+1,t,3.0)+Math.sin(k)*1.2);
    }
    ctx.stroke();
  }

  // 内容：任务类型用不同图标/布局
  drawLineContent(t, idx, shot, st);
}

function drawLineContent(t, idx, shot, st){
  const p=easeOut(seg(t,st+1.0,.7));
  if(p<=0)return;

  ctx.textAlign='center';ctx.textBaseline='middle';
  const y0=H*0.55;

  if(shot.task==='cover'){
    // hook
    ctx.font='400 24px '+F;
    ctx.fillStyle=STYLE.dim;
    const hk=shot.narration.slice(0, Math.ceil(shot.narration.length*p));
    ctx.fillText(hk, W/2, y0+30);
  }else if(shot.task==='section'){
    // 要点
    ctx.font='400 22px '+F;
    ctx.fillStyle=STYLE.dim;
    const txt=(shot.narration||'').slice(0, Math.ceil((shot.narration||'').length*p));
    ctx.fillText(txt, W/2, y0);
  }else if(shot.task==='facts'){
    // 事实点列表
    DATA.facts.slice(0,3).forEach((f,i)=>{
      const pi=easeOut(seg(t,st+1.0+i*.25,.5));
      if(pi<=0)return;
      const y=y0-40+i*70;
      ctx.beginPath();
      ctx.fillStyle=f.source?STYLE.accent:STYLE.accent2;
      ctx.arc(W/2-200, y, 6, 0, 7);
      ctx.fill();
      ctx.font='500 22px '+F;
      ctx.fillStyle=STYLE.ink;
      ctx.textAlign='left';
      ctx.fillText(f.claim.slice(0,Math.ceil(f.claim.length*pi)), W/2-180, y);
      ctx.font='400 15px '+F;
      ctx.fillStyle=f.source?STYLE.dim:STYLE.accent2;
      ctx.fillText(f.source?('来源：'+f.source):'⚠ 无出处', W/2-180, y+28);
      ctx.textAlign='center';
    });
  }else{
    // ending: meta
    ctx.font='400 20px '+F;
    ctx.fillStyle=STYLE.dim;
    ctx.fillText((DATA.meta||'').slice(0,Math.ceil((DATA.meta||'').length*p)), W/2, y0);
  }
}
"""

# ============================================================
# HUD 渲染 JS（网格 + 扫描线 + 环形进度）
# ============================================================

_HUD_RENDER = r"""
function drawFrame(t){
  t=clamp(t,0,DUR);
  const {idx,shot,st}=currentShot(t);

  // 背景 + 网格
  ctx.fillStyle=STYLE.bg;
  ctx.fillRect(0,0,W,H);
  const g=ctx.createRadialGradient(W/2,H*.45,60,W/2,H*.45,W*.6);
  g.addColorStop(0,STYLE.bg2);g.addColorStop(1,STYLE.bg);
  ctx.fillStyle=g;ctx.fillRect(0,0,W,H);

  // 网格
  const gp=easeOut(seg(t,st,.8));
  ctx.strokeStyle='rgba(64,190,150,.08)';
  ctx.lineWidth=1;
  for(let x=0;x<=W;x+=40){
    const h=H*gp;
    ctx.beginPath();ctx.moveTo(x+.5,H/2-h/2);ctx.lineTo(x+.5,H/2+h/2);ctx.stroke();
  }
  for(let y=0;y<=H;y+=40){
    const w=W*gp;
    ctx.beginPath();ctx.moveTo(W/2-w/2,y+.5);ctx.lineTo(W/2+w/2,y+.5);ctx.stroke();
  }

  // 扫描线
  const sy=((t*190)%(H+200))-100;
  const sg=ctx.createLinearGradient(0,sy-90,0,sy+90);
  sg.addColorStop(0,'rgba(34,211,238,0)');sg.addColorStop(.5,'rgba(34,211,238,.06)');sg.addColorStop(1,'rgba(34,211,238,0)');
  ctx.fillStyle=sg;ctx.fillRect(0,sy-90,W,180);

  drawHudContent(t, idx, shot, st);
}

function drawHudContent(t, idx, shot, st){
  // 镜头编号
  const pn=easeOut(seg(t,st,.5));
  if(pn>0){
    ctx.font='600 14px ui-monospace,monospace';
    ctx.fillStyle=STYLE.accent;
    ctx.textAlign='left';
    ctx.fillText(shot.shot_id, 50, 60);
  }

  // 标题
  const pt=easeOut(seg(t,st,.8));
  if(pt>0){
    drawTitle(shot.screen_text, W/2, H*0.42, 50, 800, STYLE.ink, pt, true);
  }

  // 任务标签
  const pl=easeOut(seg(t,st+.3,.5));
  if(pl>0){
    ctx.font='600 14px ui-monospace,monospace';
    ctx.fillStyle=STYLE.accent;
    ctx.textAlign='center';
    ctx.fillText(shot.task.toUpperCase(), W/2, H*0.28);
  }

  drawHudBody(t, idx, shot, st);
}

function drawHudBody(t, idx, shot, st){
  const p=easeOut(seg(t,st+1.0,.7));
  if(p<=0)return;

  if(shot.task==='cover'){
    // hook
    ctx.font='400 22px '+F;
    ctx.fillStyle=STYLE.dim;
    ctx.textAlign='center';
    ctx.fillText(shot.narration.slice(0,Math.ceil(shot.narration.length*p)), W/2, H*0.56);
  }else if(shot.task==='section'){
    ctx.font='400 20px '+F;
    ctx.fillStyle=STYLE.dim;
    ctx.textAlign='center';
    ctx.fillText((shot.narration||'').slice(0,Math.ceil((shot.narration||'').length*p)), W/2, H*0.56);
  }else if(shot.task==='facts'){
    DATA.facts.slice(0,3).forEach((f,i)=>{
      const pi=easeOut(seg(t,st+1.0+i*.25,.5));
      if(pi<=0)return;
      const y=H*0.5+i*80;
      ctx.fillStyle=f.source?STYLE.accent:STYLE.accent2;
      ctx.beginPath();ctx.arc(W/2-300,y,6,0,7);ctx.fill();
      ctx.font='500 22px '+F;
      ctx.fillStyle=STYLE.ink;
      ctx.textAlign='left';
      ctx.fillText(f.claim.slice(0,Math.ceil(f.claim.length*pi)), W/2-280, y);
      ctx.font='400 14px ui-monospace,monospace';
      ctx.fillStyle=f.source?STYLE.dim:STYLE.accent2;
      ctx.fillText(f.source?('来源：'+f.source):'⚠ 无出处', W/2-280, y+28);
      ctx.textAlign='center';
    });
  }else{
    ctx.font='400 18px '+F;
    ctx.fillStyle=STYLE.dim;
    ctx.textAlign='center';
    ctx.fillText((DATA.meta||'').slice(0,Math.ceil((DATA.meta||'').length*p)), W/2, H*0.56);
  }
}
"""

# 风格 id -> 渲染 JS
_STYLE_RENDERERS = {
    "pixel": _PIXEL_RENDER,
    "line": _LINE_RENDER,
    "hud": _HUD_RENDER,
    "geo": _LINE_RENDER,  # geo 暂用线稿渲染（白纸黑线），后续可独立
}

# ============================================================
# HTML 模板
# ============================================================

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
  #fill{height:100%;width:0;background:linear-gradient(90deg,#6630f8,#9a7bff);border-radius:2px}
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
const QS0 = new URLSearchParams(location.search);
const STYLE = __STYLE__;
const DATA = __DATA__;
const cv = document.getElementById('c');
const ctx = cv.getContext('2d');
const W = 1600, H = 900;
const F = STYLE.font === 'mono' ? 'ui-monospace,SFMono-Regular,Menlo,monospace'
  : 'system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif';

__COMMON_JS__

__RENDER_JS__

__BOOT_JS__
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
    page = page.replace("__COMMON_JS__", _COMMON_JS)
    page = page.replace("__RENDER_JS__", render_js)
    page = page.replace("__BOOT_JS__", _BOOT_JS)
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
    path.write_text(render_video(brief, body, style_id, meta_line), encoding="utf-8")
    return path


def styles_meta() -> list[dict[str, object]]:
    return list_styles()
