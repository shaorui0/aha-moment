"""Single-page web UI (inline HTML, no build step, no external JS)."""

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aha</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--mut:#777;--card:#f4f4f6;--acc:#d9480f}
@media(prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#999;--card:#1d1d20}}
body{margin:0;padding:12px 16px 80px;background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,system-ui,sans-serif;max-width:720px;margin-inline:auto}
h1{font-size:20px;margin:4px 0 10px}
input,button{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--mut);background:var(--card);color:var(--fg)}
button{cursor:pointer}button.rec{background:var(--acc);color:#fff;border-color:var(--acc)}
.row{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}
.item{background:var(--card);border-radius:10px;padding:10px 12px;margin:8px 0}
.meta{font-size:13px;color:var(--mut);display:flex;gap:8px;flex-wrap:wrap}
.txt{white-space:pre-wrap;margin:6px 0 4px}
.st-failed,.st-failed_permanent{color:#c92a2a}.st-pending,.st-processing{color:#e67700}
audio{width:100%;height:36px;margin-top:4px}
#msg{color:var(--mut);font-size:14px;min-height:1em}
</style></head><body>
<h1>Aha Moment</h1>
<div class="row" id="tokrow"><input id="tok" type="password" placeholder="token" style="flex:1;min-width:0"><button id="savetok">Save</button></div>
<div class="row"><input id="date" type="date"><button id="reload">Reload</button><button id="rec" class="rec">Quick record</button></div>
<div id="msg"></div>
<div id="list"></div>
<script>
(function(){
var $=function(i){return document.getElementById(i)};
function tok(){try{return localStorage.getItem("aha_token")||""}catch(e){return ""}}
function setmsg(s){$("msg").textContent=s}
function today(){var d=new Date();return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0")}
$("date").value=today();$("tok").value=tok();
$("savetok").onclick=function(){try{localStorage.setItem("aha_token",$("tok").value.trim())}catch(e){setmsg("localStorage unavailable")}load()};
$("reload").onclick=load;$("date").onchange=load;
function api(path,opt){opt=opt||{};opt.headers=Object.assign({Authorization:"Bearer "+tok()},opt.headers||{});return fetch(path,opt)}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function load(){
  if(!tok()){setmsg("Enter token first");return}
  api("/v1/items?date="+$("date").value).then(function(r){
    if(r.status===401){setmsg("Invalid token");return null}
    return r.json()}).then(function(j){
    if(!j)return;var L=$("list");L.textContent="";
    setmsg(j.items.length+" item(s) on "+j.date);
    j.items.forEach(function(it){
      var d=el("div","item"),m=el("div","meta");
      m.appendChild(el("span",null,it.recorded_at.substr(11,8)));
      m.appendChild(el("span",null,it.source));
      m.appendChild(el("span","st-"+it.status,it.status));
      if(it.duration_ms)m.appendChild(el("span",null,Math.round(it.duration_ms/1000)+"s"));
      d.appendChild(m);
      d.appendChild(el("div","txt",it.transcript||(it.error?("error: "+it.error):"")));
      var b=el("button",null,"Play");b.onclick=function(){
        var a=document.createElement("audio");a.controls=true;a.autoplay=true;a.preload="none";
        a.src="/v1/items/"+encodeURIComponent(it.id)+"/audio?t="+encodeURIComponent(tok());
        b.replaceWith(a)};
      d.appendChild(b);L.appendChild(d)});
  }).catch(function(e){setmsg("load failed: "+e)});
}
var mr=null,chunks=[],stream=null;
$("rec").onclick=function(){
  if(mr){mr.stop();return}
  if(!navigator.mediaDevices||!window.MediaRecorder){setmsg("Mic needs HTTPS (tailscale serve) or localhost, and MediaRecorder support");return}
  navigator.mediaDevices.getUserMedia({audio:true}).then(function(s){
    stream=s;chunks=[];mr=new MediaRecorder(s);
    mr.ondataavailable=function(e){if(e.data.size)chunks.push(e.data)};
    mr.onstop=function(){
      stream.getTracks().forEach(function(t){t.stop()});
      var type=mr.mimeType||"audio/mp4",blob=new Blob(chunks,{type:type});mr=null;
      $("rec").textContent="Quick record";setmsg("Uploading + transcribing...");
      api("/v1/quick?wait=1",{method:"POST",body:blob,headers:{"Content-Type":type.split(";")[0],"X-Aha-Recorded-At":new Date().toISOString()}})
        .then(function(r){return r.json()}).then(function(j){setmsg(j.status+": "+(j.text||""));load()})
        .catch(function(e){setmsg("upload failed: "+e)});
    };
    mr.start();$("rec").textContent="Stop";setmsg("Recording...");
  }).catch(function(e){setmsg("Mic error: "+e.message+" (iOS needs HTTPS)")});
};
if(tok())load();else $("tok").focus();
})();
</script></body></html>
"""
