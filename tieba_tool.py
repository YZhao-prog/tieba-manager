#!/usr/bin/env python3
"""贴吧吧务小工具 —— 单文件版。

用法：
    python3 tieba_tool.py
然后浏览器会自动打开 http://127.0.0.1:8000
在页面顶部填一次 BDUSS（被删帖记录还需 STOKEN），即可使用。

功能：主题帖爬取 / 用户回复查询 / 被删帖记录。
封禁功能暂不提供。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import aiotieba as tb
    from aiotieba.enums import BawuSearchType
except ImportError:
    raise SystemExit(
        "缺少依赖 aiotieba，请先安装：\n\n    pip3 install aiotieba\n"
    )


# ======================================================================
# 贴吧接口封装
# ======================================================================

class ServiceError(Exception):
    """对外可读的业务错误。"""


def _fmt_time(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


def _name(user) -> str:
    return getattr(user, "show_name", "") or getattr(user, "user_name", "") or str(getattr(user, "user_id", ""))


def _client(bduss: str, stoken: str) -> tb.Client:
    if not bduss:
        raise ServiceError("请先在页面顶部填写 BDUSS。")
    return tb.Client(account=tb.Account(bduss, stoken or ""))


def _check(resp, action: str):
    err = getattr(resp, "err", None)
    if err is not None:
        raise ServiceError(f"{action}失败：{err}")
    return resp


async def svc_me(bduss="", stoken="", **_):
    async with _client(bduss, stoken) as client:
        me = _check(await client.get_self_info(), "获取账号信息")
        return {"user_id": me.user_id, "show_name": _name(me), "user_name": me.user_name}


async def svc_thread(bduss="", stoken="", tid=None, max_pages=30, **_):
    if tid is None:
        raise ServiceError("请填写主题帖 tid。")
    async with _client(bduss, stoken) as client:
        meta = None
        floors = []
        pn = 1
        while pn <= max_pages:
            posts = _check(await client.get_posts(int(tid), pn, rn=30), "获取楼层")
            if pn == 1:
                t = posts.thread
                meta = {"fname": t.fname, "title": t.title, "author": _name(t.user), "reply_num": t.reply_num}
            for post in posts.objs:
                floors.append({
                    "floor": post.floor,
                    "user": _name(post.user),
                    "text": post.text,
                    "time": _fmt_time(post.create_time),
                    "comments": await _all_comments(client, int(tid), post),
                })
            if not posts.has_more:
                break
            pn += 1
        if meta is None:
            raise ServiceError("未获取到该主题帖，请确认 tid 是否正确。")
        return {"thread": meta, "floors": floors}


async def _all_comments(client, tid, post):
    if not post.reply_num:
        return []
    out, cpn = [], 1
    while cpn <= 200:
        cs = _check(await client.get_comments(tid, post.pid, cpn), "获取楼中楼")
        out += [{"user": _name(c.user), "text": c.text, "time": _fmt_time(c.create_time)} for c in cs.objs]
        if not cs.has_more:
            break
        cpn += 1
    return out


async def svc_user(bduss="", stoken="", tieba_uid=None, max_pages=30, **_):
    if tieba_uid is None:
        raise ServiceError("请填写用户贴吧主页 id。")
    async with _client(bduss, stoken) as client:
        user = _check(await client.tieba_uid2user_info(int(tieba_uid)), "解析用户")
        uid = user.user_id or user.portrait
        replies, fcache, pn = [], {}, 1
        while pn <= max_pages:
            up = _check(await client.get_user_posts(uid, pn), "获取用户回复")
            if not up.objs:
                break
            for group in up.objs:
                fid = group.fid
                if fid not in fcache:
                    fcache[fid] = str(_check(await client.get_fname(fid), "解析贴吧名"))
                for r in group.objs:
                    replies.append({
                        "fname": fcache[fid],
                        "link": f"https://tieba.baidu.com/p/{r.tid}?pid={r.pid}&cid=0#{r.pid}",
                        "time": _fmt_time(r.create_time),
                        "text": r.text,
                        "is_comment": bool(getattr(r, "is_comment", False)),
                    })
            pn += 1
        return {"user": {"user_id": user.user_id, "show_name": _name(user)}, "replies": replies}


async def svc_logs(bduss="", stoken="", fname=None, tieba_uid=None, max_pages=30, **_):
    if not fname or tieba_uid is None:
        raise ServiceError("请填写贴吧名与被查询人主页 id。")
    if not stoken:
        raise ServiceError("查询被删帖记录需要 STOKEN，请在页面顶部填写。")
    async with _client(bduss, stoken) as client:
        user = _check(await client.tieba_uid2user_info(int(tieba_uid)), "解析用户")
        logs, pn = [], 1
        while pn <= max_pages:
            res = _check(await client.get_bawu_postlogs(
                fname, pn, search_value=user.user_name, search_type=BawuSearchType.USER), "获取吧务日志")
            for x in res.objs:
                logs.append({
                    "title": x.title, "text": x.text, "op_user": x.op_user_name,
                    "op_type": x.op_type, "op_time": str(x.op_time),
                })
            if not res.has_more:
                break
            pn += 1
        return {"target": _name(user), "fname": fname, "logs": logs}


ROUTES = {
    "/api/me": svc_me,
    "/api/thread": svc_thread,
    "/api/user-posts": svc_user,
    "/api/postlogs": svc_logs,
}


# ======================================================================
# HTTP 服务
# ======================================================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # 静默
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        fn = ROUTES.get(self.path)
        if fn is None:
            return self._send(404, json.dumps({"error": "接口不存在"}))
        try:
            length = int(self.headers.get("Content-Length", 0))
            params = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "请求体解析失败"}))
        try:
            data = asyncio.run(fn(**params))
            self._send(200, json.dumps({"data": data}, ensure_ascii=False))
        except ServiceError as e:
            self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False))
        except TypeError:
            self._send(400, json.dumps({"error": "参数不完整"}, ensure_ascii=False))
        except Exception as e:
            self._send(502, json.dumps({"error": f"贴吧接口异常：{e}"}, ensure_ascii=False))


def _free_port(start=8000):
    for p in range(start, start + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def main():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(f"\n  贴吧吧务小工具已启动 →  {url}")
    print("  按 Ctrl+C 停止\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
        server.shutdown()


# ======================================================================
# 前端页面（HTML + CSS + JS 全部内联）
# ======================================================================

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>贴吧吧务小工具</title>
<style>
:root{--bg:#0f1216;--surface:#171b21;--surface2:#1e242c;--border:#2a323c;--text:#e6eaef;
--muted:#9aa4b2;--accent:#3b82f6;--ok:#22c55e;--err:#ef4444;--warn:#f59e0b;--r:10px}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--text);line-height:1.5}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;
padding:14px 24px;border-bottom:1px solid var(--border);background:var(--surface)}
.brand{display:flex;align-items:center;gap:12px}
.brand h1{font-size:18px;margin:0;font-weight:600}
.logo{display:grid;place-items:center;width:32px;height:32px;border-radius:8px;background:var(--accent);color:#fff;font-weight:700}
.cred{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cred input{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:13px;width:150px}
.cred input:focus{outline:none;border-color:var(--accent)}
.cred button{background:var(--accent);color:#fff;border:none;padding:8px 14px;border-radius:8px;font-size:13px;cursor:pointer}
.status{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.err{background:var(--err)}.dot.warn{background:var(--warn)}
.tabs{display:flex;gap:4px;padding:12px 24px 0;border-bottom:1px solid var(--border);background:var(--surface)}
.tab{background:none;border:none;color:var(--muted);padding:10px 16px;font-size:14px;cursor:pointer;border-bottom:2px solid transparent;border-radius:6px 6px 0 0}
.tab:hover{color:var(--text);background:var(--surface2)}
.tab.active{color:var(--text);border-bottom-color:var(--accent)}
main{max-width:960px;margin:0 auto;padding:24px}
.panel{display:none}.panel.active{display:block}
.form{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:20px}
.form label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:var(--muted);flex:1 1 200px}
.form input{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px 12px;font-size:14px}
.form input:focus{outline:none;border-color:var(--accent)}
.form button{background:var(--accent);color:#fff;border:none;padding:10px 22px;font-size:14px;border-radius:8px;cursor:pointer;font-weight:500}
.form button:hover{filter:brightness(1.08)}.form button:disabled{opacity:.5;cursor:not-allowed}
.hint{color:var(--muted);font-size:13px;margin:10px 2px 0}
.results{margin-top:24px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.rhead{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:14px 18px;border-bottom:1px solid var(--border);background:var(--surface2)}
.summary{font-size:14px}.summary b{color:var(--accent)}
.ract{display:flex;gap:8px}
.ghost{background:none;border:1px solid var(--border);color:var(--text);padding:7px 14px;border-radius:7px;font-size:13px;cursor:pointer}
.ghost:hover{background:var(--surface);border-color:var(--accent)}
#rbody{padding:6px 18px 18px;max-height:60vh;overflow:auto}
.floor{border-bottom:1px solid var(--border);padding:14px 0}.floor:last-child{border-bottom:none}
.fhead{display:flex;gap:12px;align-items:baseline;margin-bottom:6px}
.fno{color:var(--accent);font-weight:600;font-size:13px}.fuser{font-weight:500}
.ftime{color:var(--muted);font-size:12px;margin-left:auto}
.ftext{white-space:pre-wrap;word-break:break-word}
.cmts{margin:10px 0 0 16px;padding-left:14px;border-left:2px solid var(--border)}
.cmt{padding:6px 0;font-size:13.5px}.cmt .cu{color:var(--muted);margin-right:8px}
.row{padding:12px 0;border-bottom:1px solid var(--border)}.row:last-child{border-bottom:none}
.meta{font-size:12.5px;color:var(--muted);display:flex;gap:10px;flex-wrap:wrap}
.meta a{color:var(--accent);text-decoration:none}.meta a:hover{text-decoration:underline}
.tag{background:#1d3a63;color:#cfe0ff;border-radius:4px;padding:0 6px;font-size:11px}
.rtext{white-space:pre-wrap;word-break:break-word;margin-top:4px}
.ltitle{font-weight:500}.ltext{white-space:pre-wrap;margin:4px 0}.lop{font-size:12.5px;color:var(--warn)}
.box{margin-top:24px;padding:16px 18px;border-radius:var(--r);display:flex;align-items:center;gap:12px;font-size:14px}
.loading{background:var(--surface);border:1px solid var(--border);color:var(--muted)}
.error{background:#2a1416;border:1px solid #52262a;color:#ffb4b4;white-space:pre-wrap}
.spin{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:s .8s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
.empty{color:var(--muted);padding:24px 0;text-align:center}
</style></head><body>
<header class="top">
  <div class="brand"><span class="logo">吧</span><h1>贴吧吧务小工具</h1></div>
  <div class="cred">
    <input id="bduss" type="password" placeholder="BDUSS（必填）">
    <input id="stoken" type="password" placeholder="STOKEN（选填）">
    <button id="saveCred">保存并登录</button>
    <span class="status"><span class="dot" id="dot"></span><span id="stext">未登录</span></span>
  </div>
</header>
<nav class="tabs">
  <button class="tab active" data-tab="thread">主题帖爬取</button>
  <button class="tab" data-tab="user">用户回复查询</button>
  <button class="tab" data-tab="logs">被删帖记录</button>
</nav>
<main>
  <section class="panel active" id="p-thread">
    <form class="form" data-form="thread">
      <label>主题帖 tid<input name="tid" type="number" required placeholder="帖子链接中的数字"></label>
      <label>最多翻页<input name="max_pages" type="number" value="30" min="1"></label>
      <button type="submit">开始爬取</button>
    </form>
    <p class="hint">抓取该主题帖的所有楼层与楼中楼。</p>
  </section>
  <section class="panel" id="p-user">
    <form class="form" data-form="user">
      <label>用户贴吧主页 id<input name="tieba_uid" type="number" required placeholder="tieba_uid"></label>
      <label>最多翻页<input name="max_pages" type="number" value="30" min="1"></label>
      <button type="submit">查询回复</button>
    </form>
    <p class="hint">列出该用户的全部公开回复及原帖链接。</p>
  </section>
  <section class="panel" id="p-logs">
    <form class="form" data-form="logs">
      <label>贴吧名<input name="fname" type="text" required placeholder="如 yy小说"></label>
      <label>被查询人主页 id<input name="tieba_uid" type="number" required placeholder="tieba_uid"></label>
      <label>最多翻页<input name="max_pages" type="number" value="30" min="1"></label>
      <button type="submit">查询记录</button>
    </form>
    <p class="hint">需要 STOKEN，且当前账号为该吧吧务。</p>
  </section>
  <section class="results" id="results" hidden>
    <div class="rhead">
      <div class="summary" id="summary"></div>
      <div class="ract"><button class="ghost" id="copy">复制文本</button><button class="ghost" id="dl">下载 .txt</button></div>
    </div>
    <div id="rbody"></div>
  </section>
  <div class="box loading" id="loading" hidden><span class="spin"></span><span>请求中，翻页较多请耐心等待…</span></div>
  <div class="box error" id="error" hidden></div>
</main>
<script>
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const cred={bduss:localStorage.bduss||"",stoken:localStorage.stoken||""};
let out={text:"",name:"output.txt"};

function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}

// 凭证
$("#bduss").value=cred.bduss; $("#stoken").value=cred.stoken;
$("#saveCred").onclick=async()=>{
  cred.bduss=$("#bduss").value.trim(); cred.stoken=$("#stoken").value.trim();
  localStorage.bduss=cred.bduss; localStorage.stoken=cred.stoken;
  await login();
};
async function login(){
  const dot=$("#dot"),t=$("#stext");
  if(!cred.bduss){dot.className="dot err";t.textContent="未填写 BDUSS";return;}
  dot.className="dot warn";t.textContent="登录中…";
  try{
    const me=await api("/api/me",{});
    dot.className=cred.stoken?"dot ok":"dot warn";
    t.textContent=`已登录：${me.show_name} (uid ${me.user_id})`+(cred.stoken?"":" · 未填 STOKEN，被删帖记录不可用");
  }catch(e){dot.className="dot err";t.textContent="凭证无效："+e.message;}
}

async function api(url,body){
  const res=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({bduss:cred.bduss,stoken:cred.stoken,...body})});
  const p=await res.json();
  if(!res.ok) throw new Error(p.error||res.statusText);
  return p.data;
}

// tab
$$(".tab").forEach(tab=>tab.onclick=()=>{
  $$(".tab").forEach(t=>t.classList.remove("active"));
  $$(".panel").forEach(p=>p.classList.remove("active"));
  tab.classList.add("active");$("#p-"+tab.dataset.tab).classList.add("active");hideRes();
});

const CONF={
  thread:{url:"/api/thread",render:renderThread,name:"thread.txt",toText:threadText},
  user:{url:"/api/user-posts",render:renderUser,name:"user_posts.txt",toText:userText},
  logs:{url:"/api/postlogs",render:renderLogs,name:"postlogs.txt",toText:logsText},
};
$$(".form").forEach(form=>form.onsubmit=async e=>{
  e.preventDefault();
  if(!cred.bduss){showErr("请先在右上角填写 BDUSS 并登录");return;}
  const c=CONF[form.dataset.form],body={};
  new FormData(form).forEach((v,k)=>body[k]=form.elements[k].type==="number"?Number(v):v);
  hideRes();load(true);const btn=$("button",form);btn.disabled=true;
  try{
    const data=await api(c.url,body);
    out={text:c.toText(data),name:c.name};
    c.render(data);showRes();
  }catch(err){showErr(err.message);}
  finally{load(false);btn.disabled=false;}
});

// 渲染
function setBody(h){$("#rbody").innerHTML=h}
function renderThread(d){
  const t=d.thread;
  $("#summary").innerHTML=`<b>${esc(t.fname)}</b> · ${esc(t.title)} · 楼主 ${esc(t.author)} · 回复 ${t.reply_num} · 已取 <b>${d.floors.length}</b> 楼`;
  if(!d.floors.length)return setBody('<div class="empty">无楼层</div>');
  setBody(d.floors.map(f=>{
    const c=f.comments.map(x=>`<div class="cmt"><span class="cu">${esc(x.user)}</span>${esc(x.text)}</div>`).join("");
    return `<div class="floor"><div class="fhead"><span class="fno">#${f.floor}</span><span class="fuser">${esc(f.user)}</span><span class="ftime">${esc(f.time)}</span></div><div class="ftext">${esc(f.text)}</div>${c?`<div class="cmts">${c}</div>`:""}</div>`;
  }).join(""));
}
function renderUser(d){
  $("#summary").innerHTML=`<b>${esc(d.user.show_name)}</b> (uid ${d.user.user_id}) · 共 <b>${d.replies.length}</b> 条回复`;
  if(!d.replies.length)return setBody('<div class="empty">无回复</div>');
  setBody(d.replies.map(r=>`<div class="row"><div class="meta"><span>贴吧 <b>${esc(r.fname)}</b></span><span>${esc(r.time)}</span>${r.is_comment?'<span class="tag">楼中楼</span>':""}<a href="${esc(r.link)}" target="_blank" rel="noopener">原帖</a></div><div class="rtext">${esc(r.text)}</div></div>`).join(""));
}
function renderLogs(d){
  $("#summary").innerHTML=`被处理人 <b>${esc(d.target)}</b> · 吧 ${esc(d.fname)} · 共 <b>${d.logs.length}</b> 条记录`;
  if(!d.logs.length)return setBody('<div class="empty">无被删帖记录</div>');
  setBody(d.logs.map(x=>`<div class="row"><div class="ltitle">${esc(x.title)}</div><div class="ltext">${esc(x.text)}</div><div class="lop">${esc(x.op_user)} · ${esc(x.op_type)} · ${esc(x.op_time)}</div></div>`).join(""));
}

// 导出文本
function threadText(d){
  const t=d.thread;let L=[`贴吧名: ${t.fname}`,"",`标题: ${t.title}`,"",`发帖人: ${t.author}`,"",`回复数： ${t.reply_num}`,"","======================"];
  d.floors.forEach(f=>{L.push(`楼层： ${f.floor}  用户名: ${f.user}  回复: ${f.text}`,"");
    f.comments.forEach(c=>L.push(`  楼中楼： 用户名: ${c.user}  回复: ${c.text}`,""));L.push("======================");});
  return L.join("\n")+"\n";
}
function userText(d){
  let L=[`查询用户: ${d.user.show_name} (uid=${d.user.user_id})`,""];
  d.replies.forEach(r=>{L.push(`贴吧名: ${r.fname} 链接: ${r.link} 回复时间: ${r.time}${r.is_comment?"（楼中楼）":""}`,`   ${r.text}`);});
  L.push("",`共 ${d.replies.length} 条回复`);return L.join("\n")+"\n";
}
function logsText(d){
  if(!d.logs.length)return`未查询到 ${d.target} 在 ${d.fname} 的被删帖记录。\n`;
  let L=[];d.logs.forEach(x=>L.push(`被处理人：${d.target}`,x.title,x.text,`${x.op_user} ${x.op_type} ${x.op_time}`,"==============="));
  return L.join("\n")+"\n";
}

// 结果区
function showRes(){$("#results").hidden=false}
function hideRes(){$("#results").hidden=true;$("#error").hidden=true}
function load(on){$("#loading").hidden=!on}
function showErr(m){const e=$("#error");e.textContent="出错了："+m;e.hidden=false}
$("#copy").onclick=async()=>{await navigator.clipboard.writeText(out.text);const b=$("#copy"),o=b.textContent;b.textContent="已复制";setTimeout(()=>b.textContent=o,1200)};
$("#dl").onclick=()=>{const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([out.text],{type:"text/plain;charset=utf-8"}));a.download=out.name;a.click();URL.revokeObjectURL(a.href)};

if(cred.bduss) login();
</script></body></html>"""


if __name__ == "__main__":
    main()
