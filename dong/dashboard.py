"""
│ 冬 · 实时状态仪表盘
│ 端口 8899，浏览器打开 http://localhost:8899
"""
import gzip
import hashlib
import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

STATUS_FILE = os.path.join(os.path.dirname(__file__), "dong_status.json")
DASHBOARD_PORT = 8899
_start_time = time.time()

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>  · 实时状态</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Microsoft YaHei',sans-serif;background:#0d1117;color:#c9d1d9;padding:16px;min-height:100vh}
h1{font-size:16px;color:#58a6ff;margin-bottom:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dot{width:8px;height:8px;border-radius:50%;background:#3fb950;animation:pulse 1.5s infinite}
.dot.stale{background:#d29922}
.dot.dead{background:#da3633}
@keyframes pulse{50%{opacity:0.5}}
.stats-bar{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px;font-size:11px;color:#8b949e}
.stats-bar span{background:#161b22;border:1px solid #30363d;padding:4px 10px;border-radius:12px}
.stats-bar b{color:#e6edf3}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:10px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}
.card h2{font-size:12px;color:#8b949e;margin-bottom:8px;letter-spacing:1px;display:flex;align-items:center;gap:6px}
.bar-wrap{background:#21262d;border-radius:4px;height:18px;overflow:hidden;margin:3px 0}
.bar{height:100%;border-radius:4px;transition:width .4s,background .4s;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:bold;min-width:24px}
.row{display:flex;justify-content:space-between;align-items:center;margin:2px 0;font-size:12px}
.label{color:#8b949e;font-size:11px}
.value{color:#e6edf3;font-weight:bold;font-size:12px}
.grudges{max-height:150px;overflow-y:auto}
.grudge-item{background:#1c2128;border-left:3px solid #da3633;padding:5px 8px;margin:3px 0;border-radius:0 4px 4px 0;font-size:11px}
.grudge-item .uid{color:#58a6ff;font-weight:bold}
.grudge-item .ctx{color:#8b949e;margin-top:1px}
.msg-item{font-size:10px;padding:3px 6px;border-radius:3px;margin:2px 0;background:#1c2128}
.msg-item .q{color:#7ee787}
.msg-item .a{color:#ffa657}
.msg-item .meta{color:#484f58;font-size:9px}
.empty{color:#484f58;font-style:italic;font-size:11px}
.status-bar{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.badge{padding:1px 7px;border-radius:9px;font-size:10px;font-weight:bold;white-space:nowrap}
.badge.awake{background:#1b3a1b;color:#3fb950;border:1px solid #3fb950}
.badge.sleep{background:#3a1b1b;color:#da3633;border:1px solid #da3633}
.badge.info{background:#1b2d3a;color:#58a6ff;border:1px solid #58a6ff}
.badge.warn{background:#2d221b;color:#d29922;border:1px solid #d29922}
.badge.danger{background:#3a1b1b;color:#ff6b6b;border:1px solid #ff6b6b}
.badge.good{background:#1b3a1b;color:#3fb950;border:1px solid #3fb950}
.summary{font-size:11px;color:#d2a8ff;font-style:italic;padding:4px 0}
.delta-up{color:#3fb950;font-size:9px}
.delta-down{color:#da3633;font-size:9px}
.delta-flat{color:#484f58;font-size:9px}
#updateMs{color:#484f58;font-size:10px;margin-left:auto}

/* 关联词标签 */
.tag-cloud{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.tag{padding:1px 6px;border-radius:8px;font-size:10px;background:#1c2128;border:1px solid #30363d}
.tag.threat{border-color:#da3633;color:#ff7b72}
.tag.reward{border-color:#3fb950;color:#7ee787}
.tag.neutral{border-color:#484f58;color:#8b949e}

/* 激素交互迷你图 */
.interact-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;margin-top:6px;font-size:9px;text-align:center}
.interact-grid .cell{padding:2px;border-radius:2px}
.interact-grid .inh{background:rgba(218,54,51,0.15);color:#ff7b72}
.interact-grid .enh{background:rgba(63,185,80,0.15);color:#7ee787}
.interact-grid .self{background:rgba(88,166,255,0.1);color:#58a6ff}

/* 响应时间轴 */
.timeline{max-height:120px;overflow-y:auto}
.timeline-item{display:flex;align-items:center;gap:6px;padding:2px 0;font-size:10px}
.timeline-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.timeline-dot.threat{background:#da3633}
.timeline-dot.reward{background:#3fb950}
.timeline-dot.social{background:#ff77aa}
.timeline-dot.surprise{background:#d29922}
.timeline-dot.neutral{background:#484f58}
</style>
</head>
<body>
<h1>
  <div class="dot" id="connDot"></div>
   · 实时状态
  <span id="botStateBadge" style="font-size:12px;padding:2px 10px;border-radius:10px;font-weight:bold"></span>
  <span id="updateMs"></span>
</h1>
<div class="stats-bar" id="statsBar">
  <span>运行: <b id="statUptime">--</b></span>
  <span>今日消息: <b id="statMsgs">0</b></span>
  <span>最后消息: <b id="statLast">--</b></span>
  <span>版本: <b id="statVer">--</b></span>
  <span>数据刷新: <b id="statFresh">--</b></span>
  <span>更新: <b id="statPoll">--</b>ms</span>
</div>
<div class="grid">
  <div class="card">
    <h2>情绪 / 疲劳</h2>
    <div class="row"><span class="label">情绪</span><span class="value" id="moodV">--</span></div>
    <div class="bar-wrap"><div class="bar" id="moodBar">--</div></div>
    <div class="row" style="margin-top:6px"><span class="label">疲劳</span><span class="value" id="fatV">--</span></div>
    <div class="bar-wrap"><div class="bar" style="background:#8957e5" id="fatBar">--</div></div>
    <div class="status-bar" id="statusBar"></div>
    <div class="tag-cloud" id="hormEffect"></div>
  </div>
  <div class="card">
    <h2>激素状态 <span id="hormDominant" style="font-weight:normal;font-size:10px;color:#d2a8ff"></span></h2>
    <div id="hormones" style="font-size:11px"></div>
    <div class="interact-grid" id="interactGrid" style="display:none">
      <div class="cell self">DA</div><div class="cell">--</div><div class="cell">--</div><div class="cell">--</div><div class="cell">--</div>
    </div>
  </div>
  <div class="card">
    <h2>杏仁核 <span id="amyAlert" style="font-weight:normal;font-size:10px"></span></h2>
    <div id="amygdalaInfo" style="font-size:11px"><span class="empty">等待数据...</span></div>
  </div>
  <div class="card" id="overwhelmCard" style="display:none">
    <h2>超限状态</h2>
    <div id="overwhelmInfo" style="font-size:11px"></div>
  </div>
  <div class="card">
    <h2>亲密度</h2>
    <div id="intimacyInfo" style="font-size:11px"><span class="empty">等待数据...</span></div>
  </div>
  <div class="card">
    <h2>今日日程 <span style="font-size:10px;color:#484f58" id="schedDate"></span></h2>
    <div id="scheduleNow" style="font-size:11px;color:#e6edf3;margin-bottom:6px"><span class="empty">--</span></div>
    <div id="scheduleTimeline" style="max-height:200px;overflow-y:auto;font-size:10px"></div>
    <div id="scheduleMeals" style="margin-top:4px;font-size:10px;color:#8b949e"></div>
    <div id="scheduleMicro" style="font-size:10px;color:#484f58;margin-top:2px"></div>
  </div>
  <div class="card">
    <h2>天气 / 小结</h2>
    <div class="row"><span class="label">温度</span><span class="value" id="weather">--</span></div>
    <div class="summary" id="weatherMood"></div>
    <div class="summary" id="todaySummary" style="color:#d2a8ff"></div>
  </div>
  <div class="card" id="grudgeCard">
    <h2>记仇表</h2>
    <div class="grudges" id="grudges"><span class="empty">暂时没人惹她</span></div>
  </div>
  <div class="card">
    <h2>最近对话 <span style="font-size:10px;color:#484f58" id="rcCount"></span></h2>
    <div id="recent"><span class="empty">等待中...</span></div>
  </div>
  <div class="card">
    <h2>更新日志 <span style="font-size:10px;color:#484f58" id="updateCount"></span></h2>
    <div id="updateLog"><span class="empty">--</span></div>
  </div>
  <div class="card" id="optimizerCard" style="display:none">
    <h2>优化代理</h2>
    <div id="optimizerStatus"><span class="empty">--</span></div>
  </div>
</div>
<script>
var _prev = {};
var _stale = 0;

function h(s){return s?s.replace(/</g,'&lt;').replace(/>/g,'&gt;'):''}

function mkel(id,tag,attrs,html){
  var el = document.getElementById(id);
  if(!el) return;
  var hash = id + '|' + tag + '|' + JSON.stringify(attrs) + '|' + html;
  if(_prev[id]===hash) return;
  _prev[id]=hash;
  el.innerHTML = html;
  if(tag==='bar'){
    var b = el.children[0];
    if(b) b.textContent=attrs.text||'';
  }
}

function fmtT(sec){
  if(sec<60)return sec+'s';
  if(sec<3600)return Math.floor(sec/60)+'m';
  var h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60);
  return h+'h'+m+'m';
}

async function refresh(){
  var t0=Date.now();
  try{
    var r = await fetch('/status.json?t='+Date.now());
    if(r.status===304){ _stale++; if(_stale>30) document.getElementById('connDot').className='dot stale'; return; }
    _stale=0;
    document.getElementById('connDot').className='dot';
    var d = await r.json();
    var t1=Date.now();
    document.getElementById('statPoll').textContent = (t1-t0);

    // time & stats
    document.getElementById('statUptime').textContent = fmtT(d.uptime||0);
    document.getElementById('statMsgs').textContent = d.msg_count||0;
    document.getElementById('statLast').textContent = d.last_msg_time||'--';
    var ui = d.update_info||{};
    document.getElementById('statVer').textContent = '#'+(ui.version||0);
    document.getElementById('statVer').title = (ui.last_desc||'') + '\n' + (ui.last_update||'');
    document.getElementById('statVer').style.color = '#58a6ff';

    // 数据新鲜度：如果最后更新时间超过60秒，标记为过期
    var nowTs = Date.now()/1000;
    var lastUp = d._last_update_ts || (d.uptime ? nowTs : 0);
    var ageSec = Math.floor(nowTs - lastUp);
    if(ageSec < 5) document.getElementById('statFresh').textContent = '实时';
    else if(ageSec < 60) document.getElementById('statFresh').textContent = ageSec+'s前';
    else if(ageSec < 3600) document.getElementById('statFresh').textContent = Math.floor(ageSec/60)+'m前';
    else document.getElementById('statFresh').textContent = '很久';
    if(ageSec>120) document.getElementById('connDot').className='dot dead';
    else if(ageSec>60) document.getElementById('connDot').className='dot stale';

    // 机器人整体状态标签
    var bs = d.bot_state || (d.sleeping?'休眠':'清醒');
    var bsBadge = document.getElementById('botStateBadge');
    var bsColor = bs==='休眠'?'#3a1b1b;color:#da3633;border:1px solid #da3633':
                  bs==='低落'?'#2d221b;color:#d29922;border:1px solid #d29922':
                  bs==='兴奋'?'#1b3a1b;color:#3fb950;border:1px solid #3fb950':
                  bs==='疲倦'?'#2d221b;color:#d29922;border:1px solid #d29922':
                  '#1b3a1b;color:#3fb950;border:1px solid #3fb950';
    bsBadge.style.cssText='font-size:12px;padding:2px 10px;border-radius:10px;font-weight:bold;background:'+bsColor;
    bsBadge.textContent=bs;

    // mood
    var mood = d.mood||50;
    var mc = mood<35?'mood-low':mood<65?'mood-mid':'mood-high';
    var mbar = document.getElementById('moodBar');
    mbar.style.width=mood+'%'; mbar.textContent=mood; mbar.className='bar '+mc;
    document.getElementById('moodV').textContent=mood+'/100';
    // fatigue
    var fat = d.fatigue||50;
    var fbar = document.getElementById('fatBar');
    fbar.style.width=fat+'%'; fbar.textContent=fat;
    document.getElementById('fatV').textContent=fat+'/100';
    // status badges
    var sbHtml = (d.sleeping?'<span class="badge sleep">休眠</span>':'<span class="badge awake">清醒</span>');
    if(d.last_uid) sbHtml+='<span class="badge info">QQ'+d.last_uid+'</span>';
    if(d.mood<40) sbHtml+='<span class="badge warn">低落</span>';
    // 周期信息
    var ci = d.cycle||{};
    if(ci.prompt) sbHtml+='<span class="badge" style="background:#1b2d3a;color:#d2a8ff;border:1px solid #d2a8ff">'+h(ci.prompt.substring(0,20))+'</span>';
    mkel('statusBar','div',{}, sbHtml);

    // hormone effects from last event
    var he = d.hormone_event||'';
    document.getElementById('hormEffect').innerHTML = he ? '<span class="tag">'+h(he)+'</span>' : '';

    // hormones
    var horm = d.hormones||{};
    var hDiv = document.getElementById('hormones');
    if(Object.keys(horm).length>0){
      var hNames=[
        {k:'dopamine',l:'',c:'#f0883e'},
        {k:'adrenaline',l:'',c:'#ff6b6b'},
        {k:'cortisol',l:'',c:'#da3633'},
        {k:'oxytocin',l:'',c:'#ff77aa'},
        {k:'serotonin',l:'',c:'#3fb950'}
      ];
      var prevH = _prev._horm||{};
      _prev._horm = horm;
      hDiv.innerHTML = hNames.map(function(h){
        var v = horm[h.k]||50;
        var pv = prevH[h.k];
        var delta = (pv!==undefined) ? (v-pv).toFixed(0) : '';
        var dhtml = '';
        if(delta!==''){
          var dn = parseFloat(delta);
          if(dn>0.5) dhtml=' <span class="delta-up">'+delta+'</span>';
          else if(dn<-0.5) dhtml=' <span class="delta-down">'+delta+'</span>';
          else dhtml=' <span class="delta-flat">·</span>';
        }
        return '<div class="row"><span class="label">'+h.l+'</span><span class="value">'+v+dhtml+'</span></div>'+
          '<div class="bar-wrap"><div class="bar" style="width:'+v+'%;background:'+h.c+'">'+v+'</div></div>';
      }).join('');
      document.getElementById('hormDominant').textContent = (horm.dominant||'');
      // interaction mini grid
      var ig = document.getElementById('interactGrid');
      ig.style.display='';
      var inh = d.hormone_interactions||{};
      var hk = ['dopamine','adrenaline','cortisol','oxytocin','serotonin'];
      var hl = ['DA','AD','CL','OT','5HT'];
      var rows = [];
      for(var ri=0;ri<hk.length;ri++){
        var row = '<div class="cell self">'+hl[ri]+'</div>';
        for(var ci=0;ci<hk.length;ci++){
          if(ri===ci){ row+='<div class="cell self">-</div>'; continue; }
          var key = hk[ri]+'>'+hk[ci];
          var val = inh[key];
          if(val===undefined) row+='<div class="cell">·</div>';
          else if(val>0) row+='<div class="cell enh">+</div>';
          else row+='<div class="cell inh">-</div>';
        }
        rows.push(row);
      }
      var headRow = '<div class="cell" style="color:#484f58"></div>'+hl.map(function(l){return '<div class="cell" style="color:#484f58;font-size:8px">'+l+'</div>';}).join('');
      ig.innerHTML = headRow + rows.join('');
    } else {
      hDiv.innerHTML='<span class="empty"></span>';
    }

    // amygdala
    var amy = d.amygdala||{};
    var amyDiv = document.getElementById('amygdalaInfo');
    var alDiv = document.getElementById('amyAlert');
    if(Object.keys(amy).length>0){
      var val=amy.last_valence||0, aro=amy.last_arousal||0;
      var vc=val>=0?'#3fb950':'#da3633', vp=Math.abs(val)*100;
      var ab='';
      if(amy.hijack)ab='<span class="badge danger"></span>';
      else if(aro>0.7)ab='<span class="badge warn"></span>';
      else if(aro>0.3)ab='<span class="badge info"></span>';
      else ab='<span class="badge good"></span>';
      alDiv.innerHTML=ab;

      var tlabel={threat:'L'+amy.threat_level,reward:''+(amy.reward_type||''),social_bond:'',surprise:'',neutral:''}[amy.last_type]||amy.last_type||'';
      var html =
        '<div class="row"><span class="label"></span><span class="value" style="color:'+vc+'">'+(val>=0?'+':'')+val.toFixed(1)+'</span></div>'+
        '<div class="bar-wrap"><div class="bar" style="width:'+vp+'%;background:'+vc+'">'+(val>=0?'+':'')+val.toFixed(1)+'</div></div>'+
        '<div class="row" style="margin-top:5px"><span class="label"></span><span class="value">'+(aro*100).toFixed(0)+'%</span></div>'+
        '<div class="bar-wrap"><div class="bar" style="width:'+(aro*100)+'%;background:#8957e5">'+(aro*100).toFixed(0)+'%</div></div>'+
        '<div class="status-bar">'+
          '<span class="badge info">'+amy.total_threats+'</span>'+
          '<span class="badge good">'+amy.total_rewards+'</span>'+
          '<span class="badge info">'+(amy.association_count||0)+'</span>';
      if(tlabel) html+='<span class="badge" style="background:#1b2d3a;color:#d2a8ff;border:1px solid #d2a8ff">'+tlabel+'</span>';
      html+='</div>';

      // recent amygdala timeline
      if(amy.recent&&amy.recent.length>0){
        html+='<div class="timeline" style="margin-top:6px">';
        amy.recent.forEach(function(r){
          var rc=r.valence>=0?'reward':(r.arousal>0.7?'threat':'neutral');
          var rlabel={threat:'',reward:'',social_bond:'',surprise:'',neutral:''}[r.type]||r.type||'';
          html+='<div class="timeline-item"><div class="timeline-dot '+rc+'"></div><span style="color:'+(r.valence>=0?'#7ee787':'#ff7b72')+'">'+(r.valence>=0?'+':'')+r.valence.toFixed(1)+'</span> <span style="color:#8b949e">A:'+r.arousal.toFixed(1)+'</span> <span>'+h(rlabel)+(r.text?' <span style="color:#484f58">'+h(r.text)+'</span>':'')+'</div>';
        });
        html+='</div>';
      }

      // top associations
      var assoc = amy.top_associations;
      if(assoc&&assoc.length>0){
        html+='<div class="tag-cloud">';
        assoc.forEach(function(a){
          var cls = a.valence>0.2?'reward':(a.valence<-0.2?'threat':'neutral');
          html+='<span class="tag '+cls+'">'+h(a.word)+'('+(a.count||0)+')</span>';
        });
        html+='</div>';
      }

      amyDiv.innerHTML=html;
    } else {
      alDiv.innerHTML='';
      amyDiv.innerHTML='<span class="empty"></span>';
    }

    // overwhelm
    var ow=d.overwhelm||{};
    var owCard=document.getElementById('overwhelmCard');
    if(ow.active){
      owCard.style.display='';
      var ph={building:'',peak:'',recovery:'',aftermath:''};
      document.getElementById('overwhelmInfo').innerHTML=
        '<div class="row"><span class="label"></span><span class="value" style="color:#ff6b6b">'+(ph[ow.phase]||ow.phase)+'</span></div>'+
        (ow.conflict?'<div class="row"><span class="label"></span><span class="value">'+h(ow.conflict)+'</span></div>':'')+
        '<div style="font-size:10px;color:#8b949e;margin-top:3px">: '+h(ow.care||'')+' · : '+h(ow.fear||'')+'</div>'+
        (ow.breakthrough?'<div class="badge good" style="margin-top:4px;display:inline-block"></div>':'')+
        '<div style="font-size:10px;color:#484f58;margin-top:2px">'+(ow.trigger_count||1)+'</div>';
    }else{owCard.style.display='none';}

    // intimacy
    var intim = d.intimacy||{};
    var intDiv = document.getElementById('intimacyInfo');
    var intKeys = Object.keys(intim);
    if(intKeys.length>0){
      intDiv.innerHTML = intKeys.map(function(u){
        var lv = intim[u]||0;
        var label = lv>=80?'挚友':lv>=60?'密友':lv>=40?'朋友':lv>=20?'熟人':'路人';
        var ic = lv>=60?'#3fb950':lv>=40?'#58a6ff':lv>=20?'#d29922':'#8b949e';
        return '<div class="row"><span class="label">QQ'+u+'</span><span class="value" style="color:'+ic+'">Lv'+lv+' '+label+'</span></div>';
      }).join('');
    } else {
      intDiv.innerHTML='<span class="empty">--</span>';
    }

    // schedule
    var sc = d.schedule||{};
    if(sc.active){
      document.getElementById('schedDate').textContent=sc.date||'';
      var nowHtml = (sc.current||'--');
      if(sc.current_detail) nowHtml += ' <span style=\"color:#8b949e;font-size:10px\">'+h(sc.current_detail)+'</span>';
      document.getElementById('scheduleNow').innerHTML = nowHtml;
      var tl = sc.timeline||[];
      var nowTime = d.time||'00:00';
      document.getElementById('scheduleTimeline').innerHTML = tl.map(function(a,i){
        var isNow = a.time <= nowTime && (i+1>=tl.length || tl[i+1].time > nowTime);
        var icon = {wake:'',sleep:'',class:'',meal:'',free:'',transit:''}[a.type]||'';
        var tc = a.type==='class'?'#58a6ff':a.type==='meal'?'#d29922':a.type==='sleep'?'#da3633':a.type==='wake'?'#3fb950':'#8b949e';
        return '<div class="timeline-item" style="'+(isNow?'background:#1b2d3a;border-radius:3px;padding:2px 4px;margin:1px 0':'')+'"><div class="timeline-dot" style="background:'+tc+'"></div><span style="color:'+tc+'">'+a.time+'</span> <span>'+icon+h(a.name)+'</span>'+(a.detail?' <span style="color:#484f58">'+h(a.detail)+'</span>':'')+'</div>';
      }).join('');
      // meals
      var meals = sc.meals||{};
      document.getElementById('scheduleMeals').innerHTML = Object.keys(meals).map(function(k){
        return '<span>'+k+':'+h(meals[k])+'</span> ';
      }).join('· ');
      // micro events
      var me = sc.micro_events||[];
      if(me.length>0) document.getElementById('scheduleMicro').innerHTML = me.map(function(e){return '<span>'+h(e)+'</span>';}).join(' · ');
      else document.getElementById('scheduleMicro').innerHTML = '';
    }
    // weather

    // grudges
    var gDiv=document.getElementById('grudges');
    var gKeys=Object.keys(d.grudges||{});
    if(gKeys.length===0){gDiv.innerHTML='<span class="empty"></span>';}
    else{
      gDiv.innerHTML=gKeys.map(function(uid){
        var gs=d.grudges[uid];
        if(!Array.isArray(gs))gs=[gs];
        return gs.map(function(g){return '<div class="grudge-item"><span class="uid">QQ'+uid+'</span> · '+h(g.reason||'')+' · '+(g.days_left||'?')+'<div class="ctx">'+h(g.context||'')+'</div></div>';}).join('');
      }).join('');
    }

    // recent
    var rDiv=document.getElementById('recent');
    var rc=d.recent||[];
    document.getElementById('rcCount').textContent=rc.length+'';
    if(rc.length===0){rDiv.innerHTML='<span class="empty"></span>';}
    else{
      rDiv.innerHTML=rc.map(function(m){
        return '<div class="msg-item"><div class="meta">'+h(m.t||'')+' · QQ'+h(String(m.uid||'?'))+'</div><div class="q">'+h(m.q||'')+'</div><div class="a"> '+h(m.a||'')+'</div></div>';
      }).join('');
    }

    // update log
    var ui = d.update_info||{};
    var ulDiv = document.getElementById('updateLog');
    document.getElementById('updateCount').textContent = ui.total_updates+'次';
    var urec = ui.recent||[];
    if(urec.length===0){ulDiv.innerHTML='<span class="empty">--</span>';}
    else{
      var typeBadge = {update:'功能',hotfix:'修复',startup:'启动',note:'备注'};
      ulDiv.innerHTML = urec.map(function(u){
        var tbadge = typeBadge[u.type]||u.type||'';
        return '<div class="msg-item"><div class="meta">v'+u.version+' · '+h(u.time||'')+' · <span style="color:#d2a8ff">'+tbadge+'</span></div><div>'+h(u.description||'')+'</div></div>';
      }).join('');
    }

    // optimizer card
    var opt = d.optimizer || {};
    var optCard = document.getElementById('optimizerCard');
    if (opt.enabled) {
      optCard.style.display = '';
      var optDiv = document.getElementById('optimizerStatus');
      var stageMap = {starting:'启动中',backup:'备份中',analyze:'分析中',fingerprint:'指纹提取',modify:'代码修改',test:'群聊测试',evaluate:'评估中',cleanup:'清理中',idle:'待命'};
      var stageName = stageMap[opt.current_stage] || opt.current_stage || '--';
      var resultStyle = opt.last_result && opt.last_result.indexOf('上线')>=0 ? 'color:#3fb950' :
                         opt.last_result && opt.last_result.indexOf('回滚')>=0 ? 'color:#f85149' : '';
      optDiv.innerHTML =
        '<div class="row"><span class="label">上次运行</span><span class="value">' + h(opt.last_run || '--') + '</span></div>' +
        '<div class="row"><span class="label">运行次数</span><span class="value">' + (opt.total_runs||0) + '次 (上线' + (opt.successful_deploys||0) + '次)</span></div>' +
        '<div class="row"><span class="label">当前阶段</span><span class="value">' + stageName + '</span></div>' +
        '<div class="row"><span class="label">上次结果</span><span class="value" style="' + resultStyle + '">' + h(opt.last_result || '--') + '</span></div>';
    } else {
      optCard.style.display = 'none';
    }

    // update time
    document.getElementById('updateMs').textContent = ''+(t1-t0)+'ms';
  }catch(e){
    _stale++;
    if(_stale>3) document.getElementById('connDot').className='dot dead';
    else if(_stale>1) document.getElementById('connDot').className='dot stale';
  }
}

// adaptive polling: normal 3s, hover 1s
var _interval=3000;
document.addEventListener('mouseenter',function(){_interval=1000; restartPoll()},true);
document.addEventListener('mouseleave',function(){_interval=3000; restartPoll()},true);
var _timer;
function restartPoll(){clearInterval(_timer); _timer=setInterval(refresh,_interval);}
window.onload=function(){refresh(); restartPoll();};
</script>
</body>
</html>"""

# 缓存控制
_etag_cache = {}
_ETAG_TTL = 2  # 同一数据2秒内不重发


def _gzip_bytes(data: bytes, accept_encoding: str) -> tuple:
    """按需 gzip 压缩，返回 (body, content_encoding)"""
    if "gzip" in accept_encoding:
        return gzip.compress(data), "gzip"
    return data, ""


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/status.json":
            self._serve_status()
            return

        if path == "/monitor" or path == "/monitor/":
            self._serve_monitor()
            return

        # 首页
        self._serve_html()

    def _serve_monitor(self):
        monitor_path = os.path.join(os.path.dirname(__file__), "desktop_monitor.html")
        try:
            with open(monitor_path, "r", encoding="utf-8") as f:
                body = f.read().encode("utf-8")
        except Exception:
            self.send_response(404)
            self.end_headers()
            return
        enc = self.headers.get("Accept-Encoding", "")
        body, ce = _gzip_bytes(body, enc)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        if ce:
            self.send_header("Content-Encoding", ce)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        body = HTML.encode("utf-8")
        enc = self.headers.get("Accept-Encoding", "")
        body, ce = _gzip_bytes(body, enc)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        if ce:
            self.send_header("Content-Encoding", ce)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_status(self):
        global _start_time
        try:
            if os.path.exists(STATUS_FILE):
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    raw = f.read()
                data = json.loads(raw)
            else:
                data = {
                    "time": "--", "bot_state": "离线", "mood": 50, "fatigue": 50, "sleeping": False,
                    "weather": "?", "weather_mood": "", "grudges": {}, "recent": [],
                    "today_summary": "", "last_uid": None, "hormones": {},
                    "overwhelm": {}, "amygdala": {}, "intimacy": {},
                    "cycle": {}, "_msg_count": 0, "_last_msg_time": "--"
                }
                raw = json.dumps(data, ensure_ascii=False)

            # 注入运行时统计
            data["uptime"] = int(time.time() - _start_time)
            data["msg_count"] = data.get("_msg_count", 0)
            data["last_msg_time"] = data.get("_last_msg_time", "--")

            # 注入健康检查快照
            try:
                from .core.health_registry import registry as _hr
                data["health"] = _hr.snapshot()
            except Exception:
                pass

            # ETag: 用文件原始内容hash，不含秒级变化的uptime
            raw_bytes = raw.encode("utf-8")
            etag = hashlib.md5(raw_bytes).hexdigest()
            if_none = self.headers.get("If-None-Match", "")
            if if_none == etag:
                self.send_response(304)
                self.send_header("Connection", "keep-alive")
                self.send_header("ETag", etag)
                self.end_headers()
                return

            body_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            body = body_str.encode("utf-8")

            # 压缩
            enc = self.headers.get("Accept-Encoding", "")
            body, ce = _gzip_bytes(body, enc)

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("ETag", etag)
            if ce:
                self.send_header("Content-Encoding", ce)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start():
    server = ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), DashboardHandler)
    print(f"  → http://localhost:{DASHBOARD_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n")
        server.shutdown()


if __name__ == "__main__":
    start()
