"""生成单文件追踪网页:site/index.html(数据内嵌,无外部依赖,可离线打开)。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 领域配色:10 个低饱和度色相,只作为左侧 4px 色键,不铺大面积
DOMAIN_COLORS = {
    "macro_finance":   "#2F5D8C",
    "dev_trade":       "#1E6F5C",
    "labor_social":    "#8A5A2B",
    "health":          "#9B3A4B",
    "agri_food":       "#5B7A2E",
    "energy_climate":  "#186B72",
    "governance":      "#5E4B8B",
    "geopolitics":     "#8C3B1E",
    "sci_tech":        "#33557A",
    "inequality_data": "#7A5C15",
}

CSS = """
:root{
  --ink:#16181d; --paper:#fff; --rule:#e3e5e8; --rule-soft:#f0f1f3;
  --muted:#6b7076; --link:#1f3a6e; --field:#fafafa;
  --serif: Georgia,"Source Serif 4","Noto Serif","Songti SC","Hiragino Mincho ProN",serif;
  --sans: system-ui,-apple-system,"Segoe UI","Noto Sans SC","PingFang SC",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
     font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:var(--link)}
:focus-visible{outline:2px solid var(--link);outline-offset:2px}

.wrap{max-width:1180px;margin:0 auto;padding:0 24px 80px}

/* 报头 */
header{padding:44px 0 18px;border-bottom:1px solid var(--ink)}
h1{font-family:var(--serif);font-weight:400;font-size:40px;line-height:1.1;margin:0 0 10px;
   letter-spacing:-.01em}
.stamp{font-size:13px;color:var(--muted);display:flex;flex-wrap:wrap;gap:20px}
.stamp b{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}

/* 控制条 */
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;
          padding:14px 0;border-bottom:1px solid var(--rule)}
input[type=search],select{font-family:var(--sans);font-size:14px;color:var(--ink);
  background:var(--field);border:1px solid var(--rule);border-radius:3px;padding:7px 10px}
input[type=search]{flex:1;min-width:200px}
.range{display:flex;border:1px solid var(--rule);border-radius:3px;overflow:hidden}
.range button{font-family:var(--sans);font-size:13px;padding:7px 13px;border:0;
  border-left:1px solid var(--rule);background:var(--field);color:var(--muted);cursor:pointer}
.range button:first-child{border-left:0}
.range button[aria-pressed=true]{background:var(--ink);color:#fff}

/* 主体两栏 */
.cols{display:grid;grid-template-columns:214px 1fr;gap:44px;align-items:start}

/* 左侧领域索引 */
nav{position:sticky;top:20px;padding-top:26px;font-size:14px}
nav h2{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 10px}
nav button{display:flex;width:100%;align-items:baseline;gap:8px;background:none;border:0;
  padding:5px 0;cursor:pointer;font:inherit;color:var(--ink);text-align:left;
  border-bottom:1px solid var(--rule-soft)}
nav button .key{width:3px;align-self:stretch;background:var(--c,var(--rule));flex:0 0 3px}
nav button .nm{flex:1}
nav button .ct{color:var(--muted);font-variant-numeric:tabular-nums;font-size:13px}
nav button[aria-pressed=true] .nm{font-weight:600}
nav button[aria-pressed=false]{opacity:.5}
nav .reset{margin-top:12px;color:var(--link);border:0;padding:0}

/* 条目流 */
.feed{padding-top:26px;min-width:0}
.row{display:grid;grid-template-columns:3px 92px 1fr;gap:0 16px;
     padding:15px 0;border-bottom:1px solid var(--rule-soft)}
.row .key{background:var(--c);grid-row:1}
.when{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums;line-height:1.45}
.when .d{display:block;color:var(--ink)}
.when .t{display:block}
.when .approx{font-size:11px}
.row>div:last-child{min-width:0}
.row h3{font-family:var(--serif);font-weight:400;font-size:18px;line-height:1.35;margin:0 0 5px}
.row h3 a{text-decoration:none} .row h3 a:hover{text-decoration:underline}
.orig{font-size:12.5px;color:#8a8f95;margin:0 0 5px;line-height:1.4;max-width:72ch}
.who{font-size:13px;color:var(--muted);margin:0 0 6px}
.who .inst{color:var(--ink);font-weight:600}
.sum{margin:0;font-size:14px;color:#3a3e44;max-width:72ch}
.pts{margin:8px 0 0;padding:0;list-style:none;max-width:72ch}
.pts li{position:relative;padding-left:15px;margin:3px 0;font-size:13.5px;color:#40454b}
.pts li::before{content:"";position:absolute;left:2px;top:9px;width:4px;height:4px;
  border-radius:50%;background:var(--c,#bbb)}
.empty{padding:60px 0;color:var(--muted);font-size:15px}
.empty b{display:block;color:var(--ink);font-size:17px;font-family:var(--serif);
  font-weight:400;margin-bottom:6px}

@media(max-width:820px){
  .wrap{padding:0 16px 60px} h1{font-size:30px}
  .cols{grid-template-columns:1fr;gap:0}
  nav{position:static;padding:18px 0 0;border-bottom:1px solid var(--rule)}
  nav h2{display:none}
  nav .list{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px}
  nav button{width:auto;flex:0 0 auto;border:1px solid var(--rule);border-radius:3px;
    padding:5px 10px;white-space:nowrap}
  nav button .key{display:none}
  .row{grid-template-columns:3px 1fr;gap:0 12px}
  .row .key{grid-row:1/span 2}
  .when{grid-column:2;margin-bottom:3px}
  .row>div:last-child{grid-column:2}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

JS = """
const $=s=>document.querySelector(s);
let fDomain=null, fRegion="", fRange=0, fQuery="";

function fmtWhen(it){
  const p=n=>String(n).padStart(2,'0');
  const fmt=s=>{const d=new Date(s);
    return [`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`,
            `${p(d.getHours())}:${p(d.getMinutes())}`];};
  if(it.published_utc){
    const [day,clock]=fmt(it.published_utc);
    return it.has_clock_time
      ? `<span class="d">${day}</span><span class="t">${clock}</span>`
      : `<span class="d">${day}</span><span class="t approx">无时分</span>`;
  }
  if(it.first_seen){
    const [day,clock]=fmt(it.first_seen);
    return `<span class="d">${day}</span><span class="t approx">${clock} 抓到</span>`;
  }
  return '<span class="t approx">时间未知</span>';
}
const zhTitle=it=>it.title_zh||it.title;
const zhSum=it=>it.summary_zh||it.summary||"";
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function pass(it){
  if(fDomain && it.domain!==fDomain) return false;
  if(fRegion && it.region!==fRegion) return false;
  if(fRange){
    const ts=it.published_utc||it.first_seen;
    if(!ts) return false;
    if(Date.now()-new Date(ts) > fRange*3600e3) return false;
  }
  if(fQuery){
    const hay=(it.title+" "+(it.title_zh||"")+" "+it.summary+" "+
               (it.summary_zh||"")+" "+((it.points_zh||[]).join(" "))+" "+
               it.institution).toLowerCase();
    if(!fQuery.split(/\\s+/).every(w=>hay.includes(w))) return false;
  }
  return true;
}

function render(){
  const shown=ITEMS.filter(pass);
  // 左栏计数在除领域外的其他筛选条件下重算
  const base=ITEMS.filter(it=>{const s=fDomain;fDomain=null;const r=pass(it);fDomain=s;return r;});
  document.querySelectorAll('nav button[data-d]').forEach(b=>{
    const d=b.dataset.d;
    b.querySelector('.ct').textContent=base.filter(i=>i.domain===d).length;
    b.setAttribute('aria-pressed', String(!fDomain || fDomain===d));
  });
  $('#count').textContent=shown.length;
  $('#feed').innerHTML = shown.length ? shown.map(it=>`
    <article class="row" style="--c:${DCOLORS[it.domain]||'#ccc'}">
      <div class="key"></div>
      <div class="when">${fmtWhen(it)}</div>
      <div>
        <h3><a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(zhTitle(it))}</a></h3>
        ${zhTitle(it)!==it.title?`<p class="orig">${esc(it.title)}</p>`:''}
        <p class="who"><span class="inst">${esc(it.institution)}</span>${it.region?'，'+esc(it.region):''}　${esc(DLABELS[it.domain]||'')}</p>
        <p class="sum">${esc(zhSum(it))||'源站未提供摘要，点标题看原文。'}</p>
        ${(it.points_zh&&it.points_zh.length)?
          `<ul class="pts">${it.points_zh.map(p=>`<li>${esc(p)}</li>`).join('')}</ul>`:''}
      </div>
    </article>`).join('') :
    `<div class="empty"><b>这个范围内没有条目</b>放宽时间范围，或清除筛选再看一次。</div>`;
}

document.addEventListener('DOMContentLoaded',()=>{
  document.querySelectorAll('nav button[data-d]').forEach(b=>b.onclick=()=>{
    fDomain = fDomain===b.dataset.d ? null : b.dataset.d; render();});
  $('#reset').onclick=()=>{fDomain=null;fRegion="";fRange=0;fQuery="";
    $('#q').value="";$('#region').value="";
    document.querySelectorAll('.range button').forEach(x=>
      x.setAttribute('aria-pressed',String(x.dataset.h==="0")));render();};
  $('#q').oninput=e=>{fQuery=e.target.value.trim().toLowerCase();render();};
  $('#region').onchange=e=>{fRegion=e.target.value;render();};
  document.querySelectorAll('.range button').forEach(b=>b.onclick=()=>{
    fRange=+b.dataset.h;
    document.querySelectorAll('.range button').forEach(x=>
      x.setAttribute('aria-pressed',String(x===b)));render();});
  render();
});
"""


def build_site(items: list[dict], topics: dict, out_dir: Path,
               last_run: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    items = sorted(items, key=lambda x: (x.get("published_utc")
                                         or x.get("first_seen") or ""), reverse=True)

    labels = {c: cfg["label"] for c, cfg in topics.items()}
    regions = sorted({i["region"] for i in items if i.get("region")})

    nav = []
    for code, cfg in topics.items():
        n = sum(1 for i in items if i.get("domain") == code)
        nav.append(
            f'<button data-d="{code}" aria-pressed="true" '
            f'style="--c:{DOMAIN_COLORS.get(code, "#ccc")}">'
            f'<span class="key"></span><span class="nm">{cfg["label"]}</span>'
            f'<span class="ct">{n}</span></button>')

    stamp = (datetime.fromisoformat(last_run).astimezone().strftime("%Y-%m-%d %H:%M")
             if last_run else datetime.now().strftime("%Y-%m-%d %H:%M"))
    insts = len({i["institution"] for i in items})

    html = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>智库动态追踪</title>
<style>{CSS}</style></head><body>
<div class="wrap">
<header>
  <h1>智库动态追踪</h1>
  <p class="stamp">
    <span>上次更新 <b>{stamp}</b></span>
    <span>每 6 小时抓取一次</span>
    <span>在库 <b>{len(items)}</b> 条，来自 <b>{insts}</b> 家机构</span>
  </p>
</header>

<div class="controls">
  <input type="search" id="q" placeholder="搜索标题、摘要或机构名">
  <select id="region"><option value="">全部地区</option>
    {"".join(f'<option value="{r}">{r}</option>' for r in regions)}</select>
  <div class="range" role="group" aria-label="时间范围">
    <button data-h="6" aria-pressed="false">6 小时</button>
    <button data-h="24" aria-pressed="false">24 小时</button>
    <button data-h="168" aria-pressed="false">7 天</button>
    <button data-h="0" aria-pressed="true">全部</button>
  </div>
</div>

<div class="cols">
  <nav>
    <h2>领域（<span id="count">0</span> 条符合条件）</h2>
    <div class="list">{"".join(nav)}</div>
    <button class="reset" id="reset">清除全部筛选</button>
  </nav>
  <main class="feed" id="feed"></main>
</div>
</div>
<script>
const ITEMS={json.dumps(items, ensure_ascii=False)};
const DCOLORS={json.dumps(DOMAIN_COLORS)};
const DLABELS={json.dumps(labels, ensure_ascii=False)};
{JS}
</script></body></html>"""

    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path
