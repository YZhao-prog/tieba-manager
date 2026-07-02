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
import os
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
            # with_comments=True 让每页楼层连同前若干条楼中楼一次返回，
            # 避免对每个楼层再逐页请求（大帖会因此卡住）。
            posts = _check(
                await client.get_posts(int(tid), pn, rn=30, with_comments=True, comment_rn=10),
                "获取楼层",
            )
            if pn == 1:
                t = posts.thread
                meta = {"fname": t.fname, "title": t.title, "author": _name(t.user), "reply_num": t.reply_num}
            for post in posts.objs:
                comments = [
                    {"user": _name(c.user), "text": c.text, "time": _fmt_time(c.create_time)}
                    for c in post.comments
                ]
                floors.append({
                    "floor": post.floor,
                    "user": _name(post.user),
                    "text": post.text,
                    "time": _fmt_time(post.create_time),
                    "comments": comments,
                    "more_comments": max(post.reply_num - len(comments), 0),
                })
            if not posts.has_more:
                break
            pn += 1
        if meta is None:
            raise ServiceError("未获取到该主题帖，请确认 tid 是否正确。")
        return {"thread": meta, "floors": floors}


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
                        "tid": r.tid,
                        "pid": r.pid,
                        "link": f"https://tieba.baidu.com/p/{r.tid}?pid={r.pid}&cid=0#{r.pid}",
                        "time": _fmt_time(r.create_time),
                        "text": r.text,
                        "is_comment": bool(getattr(r, "is_comment", False)),
                    })
            pn += 1
        return {
            "user": {"tieba_uid": int(tieba_uid), "show_name": _name(user)},
            "replies": replies,
        }


async def svc_logs(bduss="", stoken="", fname=None, tieba_uid=None, max_pages=30, **_):
    if not fname or tieba_uid is None:
        raise ServiceError("请填写贴吧名与被查询人主页 id。")
    if not stoken:
        raise ServiceError("查询被删帖记录需要 STOKEN，请在页面顶部填写。")
    async with _client(bduss, stoken) as client:
        user = _check(await client.tieba_uid2user_info(int(tieba_uid)), "解析用户")
        logs, pn = [], 1
        while pn <= max_pages:
            res = await client.get_bawu_postlogs(
                fname, pn, search_value=user.user_name, search_type=BawuSearchType.USER)
            if getattr(res, "err", None) is not None:
                if "302" in str(res.err):
                    raise ServiceError(
                        "吧务日志鉴权失败（302）。请检查：①STOKEN 要用 .tieba.baidu.com 域下的那个；"
                        "②贴吧名需带“吧”字（如「yy小说吧」）；③当前账号须是该吧吧务。"
                    )
                raise ServiceError(f"获取吧务日志失败：{res.err}")
            for x in res.objs:
                logs.append({
                    "title": x.title, "text": x.text, "op_user": x.op_user_name,
                    "op_type": x.op_type, "op_time": str(x.op_time),
                })
            if not res.has_more:
                break
            pn += 1
        return {"target": _name(user), "fname": fname, "logs": logs}


async def svc_locate(bduss="", stoken="", tid=None, pid=None, is_comment=False, max_pages=50, **_):
    """按需定位一条回复/楼中楼在第几楼、第几页，并给出可精确跳转的链接。

    楼层号不在搜索类接口的返回里，只能回帖里逐页扫描匹配 pid，故设扫描上限。
    """
    if tid is None or pid is None:
        raise ServiceError("缺少 tid/pid。")
    tid, pid = int(tid), int(pid)
    max_pages = min(int(max_pages), 50)  # 扫描上限，避免深帖跑太久
    async with _client(bduss, stoken) as client:
        pn = 1
        while pn <= max_pages:
            posts = _check(
                await client.get_posts(tid, pn, rn=30, with_comments=True, comment_rn=10),
                "定位楼层",
            )
            for post in posts.objs:
                if not is_comment and post.pid == pid:
                    return {"found": True, "floor": post.floor, "page": pn,
                            "url": f"https://tieba.baidu.com/p/{tid}?pid={pid}&cid=0#{pid}"}
                if is_comment:
                    for cm in post.comments:
                        if cm.pid == pid:
                            return {"found": True, "floor": post.floor, "page": pn,
                                    "url": f"https://tieba.baidu.com/p/{tid}?pid={post.pid}&cid={pid}#{pid}"}
            if not posts.has_more:
                break
            pn += 1
        return {"found": False, "scanned": pn}


def load_defaults() -> dict:
    """默认凭证来源（优先级：环境变量 > 本地 secret.py）。

    secret.py 已在 .gitignore 中，不会被提交，可安全存放真实 BDUSS/STOKEN。
    """
    bduss = os.environ.get("TIEBA_BDUSS", "")
    stoken = os.environ.get("TIEBA_STOKEN", "")
    try:
        import secret  # 本地、gitignore

        bduss = bduss or getattr(secret, "BDUSS", "")
        stoken = stoken or getattr(secret, "STOKEN", "")
    except ImportError:
        pass
    return {"bduss": bduss.strip(), "stoken": stoken.strip()}


DEFAULTS = load_defaults()


ROUTES = {
    "/api/me": svc_me,
    "/api/thread": svc_thread,
    "/api/user-posts": svc_user,
    "/api/postlogs": svc_logs,
    "/api/locate": svc_locate,
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
        elif self.path == "/api/defaults":
            self._send(200, json.dumps(DEFAULTS, ensure_ascii=False))
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
[hidden]{display:none!important}
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
.meta{font-size:12.5px;color:var(--muted);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.meta .spacer{flex:1 1 auto}
.meta a{color:var(--accent);text-decoration:none;white-space:nowrap}.meta a:hover{text-decoration:underline}
.chip{background:var(--surface2);border:1px solid var(--border);border-radius:5px;padding:1px 8px;font-size:12px;color:var(--text)}
.mini{background:none;border:1px solid var(--border);color:var(--muted);border-radius:5px;padding:1px 8px;font-size:11px;cursor:pointer;white-space:nowrap}
.mini:hover{border-color:var(--accent);color:var(--text)}
.mini:disabled{cursor:default;opacity:.7}
.mini.hit{border-color:var(--accent);color:#cfe0ff}
.tag{background:#1d3a63;color:#cfe0ff;border-radius:4px;padding:1px 6px;font-size:11px}
.rtext{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;margin-top:6px;font-size:14px}
.ltitle{font-weight:500;margin-top:6px}.ltext{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--muted);margin-top:4px;font-size:13.5px}
.optag{background:#3a2a12;color:#ffd7a8;border:1px solid #5a3f1c;border-radius:4px;padding:1px 6px;font-size:11px}
.box{margin-top:24px;padding:16px 18px;border-radius:var(--r);display:flex;align-items:center;gap:12px;font-size:14px}
.loading{background:var(--surface);border:1px solid var(--border);color:var(--muted)}
.error{background:#2a1416;border:1px solid #52262a;color:#ffb4b4;white-space:pre-wrap}
.spin{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:s .8s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
.search{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:7px;padding:7px 12px;font-size:13px;width:160px}
.search:focus{outline:none;border-color:var(--accent)}
.pager{display:flex;align-items:center;justify-content:center;gap:12px;padding:12px 18px;border-top:1px solid var(--border);background:var(--surface2);font-size:13px;color:var(--muted)}
.pager button:disabled{opacity:.4;cursor:not-allowed}
.pager select{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 8px;font-size:13px}
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
      <label>最多翻页<input name="max_pages" type="number" value="10" min="1"></label>
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
      <div class="ract"><input id="search" class="search" placeholder="搜索文本…"><button class="ghost" id="copy">复制文本</button><button class="ghost" id="dl">下载 .txt</button></div>
    </div>
    <div id="rbody"></div>
    <div class="pager" id="pager" hidden>
      <button class="ghost" id="prev">← 上一页</button>
      <span id="pageinfo"></span>
      <button class="ghost" id="next">下一页 →</button>
      <select id="per"><option value="50">50/页</option><option value="100">100/页</option><option value="200">200/页</option></select>
    </div>
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
    c.render(data);
  }catch(err){showErr(err.message);}
  finally{load(false);btn.disabled=false;}
});

// 渲染 —— 统一的“视图”模型，支持文本搜索 + 结果翻页
function setBody(h){$("#rbody").innerHTML=h}
let view=null, page=1, per=50, query="";

function showResult(v){view=v;query="";page=1;$("#search").value="";$("#results").hidden=false;applyView()}
function applyView(){
  if(!view)return;
  const q=query.trim().toLowerCase();
  const items=q?view.items.filter(it=>view.match(it).toLowerCase().includes(q)):view.items;
  const total=view.items.length, shown=items.length, pages=Math.max(1,Math.ceil(shown/per));
  if(page>pages)page=pages; if(page<1)page=1;
  const slice=items.slice((page-1)*per,page*per);
  $("#summary").innerHTML=view.head+` · 共 <b>${total}</b> 条`+(q?` · 匹配 <b>${shown}</b>`:"");
  setBody(slice.length?slice.map(view.itemHTML).join(""):`<div class="empty">${q?"没有匹配的内容":view.empty}</div>`);
  const pager=$("#pager");
  if(shown>per){pager.hidden=false;$("#pageinfo").textContent=`第 ${page} / ${pages} 页`;$("#prev").disabled=page<=1;$("#next").disabled=page>=pages;}
  else pager.hidden=true;
}
// 「查楼层」按需定位（事件委托，#rbody 常驻）
$("#rbody").addEventListener("click",async e=>{
  const b=e.target.closest(".locbtn"); if(!b||b.dataset.done)return;
  b.disabled=true; const old=b.textContent; b.textContent="查询中…";
  try{
    const res=await api("/api/locate",{tid:Number(b.dataset.tid),pid:Number(b.dataset.pid),is_comment:b.dataset.c==="1"});
    if(res.found){
      b.textContent=`第 ${res.floor} 楼 · 第 ${res.page} 页`; b.classList.add("hit"); b.dataset.done=1;
      const a=b.parentElement.querySelector("a.orig"); if(a)a.href=res.url;
    }else{ b.textContent="未定位到"; b.disabled=false; }
  }catch(err){ b.textContent="查询失败"; b.disabled=false; }
});
$("#search").oninput=e=>{query=e.target.value;page=1;applyView()};
$("#prev").onclick=()=>{if(page>1){page--;applyView();$("#rbody").scrollTop=0}};
$("#next").onclick=()=>{page++;applyView();$("#rbody").scrollTop=0};
$("#per").onchange=e=>{per=Number(e.target.value);page=1;applyView()};

function renderThread(d){
  const t=d.thread;
  showResult({
    head:`<b>${esc(t.fname)}</b> · ${esc(t.title)} · 楼主 ${esc(t.author)} · 回复 ${t.reply_num}`,
    items:d.floors, empty:"无楼层",
    match:f=>f.user+" "+f.text+" "+f.comments.map(c=>c.user+" "+c.text).join(" "),
    itemHTML:f=>{
      let c=f.comments.map(x=>`<div class="cmt"><span class="cu">${esc(x.user)}</span>${esc(x.text)}</div>`).join("");
      if(f.more_comments>0)c+=`<div class="cmt" style="color:var(--muted)">… 还有 ${f.more_comments} 条楼中楼未展开</div>`;
      return `<div class="floor"><div class="fhead"><span class="fno">#${f.floor}</span><span class="fuser">${esc(f.user)}</span><span class="ftime">${esc(f.time)}</span></div><div class="ftext">${esc(f.text)}</div>${c?`<div class="cmts">${c}</div>`:""}</div>`;
    },
  });
}
function renderUser(d){
  showResult({
    head:`<b>${esc(d.user.show_name)}</b> · 主页id ${d.user.tieba_uid}`,
    items:d.replies, empty:"无回复（可能对方未公开回复，或主页 id 有误）",
    match:r=>r.fname+" "+r.text,
    itemHTML:r=>`<div class="row"><div class="meta"><span class="chip">${esc(r.fname)}</span>${r.is_comment?'<span class="tag">楼中楼</span>':""}<span class="spacer"></span><span>${esc(r.time)}</span><button class="mini locbtn" data-tid="${r.tid}" data-pid="${r.pid}" data-c="${r.is_comment?1:0}">查楼层</button><a class="orig" href="${esc(r.link)}" target="_blank" rel="noopener">原帖 ↗</a></div><div class="rtext">${esc(r.text)}</div></div>`,
  });
}
function renderLogs(d){
  showResult({
    head:`被处理人 <b>${esc(d.target)}</b> · 吧 ${esc(d.fname)}`,
    items:d.logs, empty:"无被删帖记录",
    match:x=>x.title+" "+x.text+" "+x.op_user,
    itemHTML:x=>`<div class="row"><div class="meta"><span class="optag">${esc(x.op_type)}</span><span>操作人 ${esc(x.op_user)}</span><span class="spacer"></span><span>${esc(x.op_time)}</span></div><div class="ltitle">${esc(x.title)}</div><div class="ltext">${esc(x.text)}</div></div>`,
  });
}

// 导出文本
function threadText(d){
  const t=d.thread;let L=[`贴吧名: ${t.fname}`,"",`标题: ${t.title}`,"",`发帖人: ${t.author}`,"",`回复数： ${t.reply_num}`,"","======================"];
  d.floors.forEach(f=>{L.push(`楼层： ${f.floor}  用户名: ${f.user}  回复: ${f.text}`,"");
    f.comments.forEach(c=>L.push(`  楼中楼： 用户名: ${c.user}  回复: ${c.text}`,""));
    if(f.more_comments>0)L.push(`  … 还有 ${f.more_comments} 条楼中楼未展开`,"");
    L.push("======================");});
  return L.join("\n")+"\n";
}
function userText(d){
  let L=[`查询用户: ${d.user.show_name} (主页id=${d.user.tieba_uid})`,""];
  d.replies.forEach(r=>{L.push(`贴吧名: ${r.fname} 链接: ${r.link} 回复时间: ${r.time}${r.is_comment?"（楼中楼）":""}`,`   ${r.text}`);});
  L.push("",`共 ${d.replies.length} 条回复`);return L.join("\n")+"\n";
}
function logsText(d){
  if(!d.logs.length)return`未查询到 ${d.target} 在 ${d.fname} 的被删帖记录。\n`;
  let L=[];d.logs.forEach(x=>L.push(`被处理人：${d.target}`,x.title,x.text,`${x.op_user} ${x.op_type} ${x.op_time}`,"==============="));
  return L.join("\n")+"\n";
}

// 结果区
function hideRes(){$("#results").hidden=true;$("#error").hidden=true;$("#pager").hidden=true}
function load(on){$("#loading").hidden=!on}
function showErr(m){const e=$("#error");e.textContent="出错了："+m;e.hidden=false}
$("#copy").onclick=async()=>{await navigator.clipboard.writeText(out.text);const b=$("#copy"),o=b.textContent;b.textContent="已复制";setTimeout(()=>b.textContent=o,1200)};
$("#dl").onclick=()=>{const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([out.text],{type:"text/plain;charset=utf-8"}));a.download=out.name;a.click();URL.revokeObjectURL(a.href)};

async function init(){
  let def={bduss:"",stoken:""};       // 服务端默认（secret.py / 环境变量）
  try{ def=await fetch("/api/defaults").then(r=>r.json()); }catch(e){}
  cred.bduss=cred.bduss||def.bduss||"";      // localStorage 优先，缺的用默认补
  cred.stoken=cred.stoken||def.stoken||"";
  $("#bduss").value=cred.bduss;
  $("#stoken").value=cred.stoken;
  if(cred.bduss) login();
}
init();
</script></body></html>"""


if __name__ == "__main__":
    main()
