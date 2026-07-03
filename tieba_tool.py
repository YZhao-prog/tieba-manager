#!/usr/bin/env python3
"""贴吧吧务小工具 —— 单文件版（后端 + 网页全在这一个文件里）。

用法：
    python3 tieba_tool.py
浏览器会自动打开 http://127.0.0.1:8000。

凭证：把 BDUSS/STOKEN 写进 secret.py（gitignore，见 secret.example.py）
可自动登录；也可以在页面右上角手动填。STOKEN 必须用 .tieba.baidu.com
域下的那个（.passport 域的会 302）。

功能：
    主题帖爬取     全楼层 + 楼中楼，大帖秒级返回
    用户发言查询   回复 / 主题帖 / 全部，附「查楼层」精确定位
    吧务处理记录   某用户被吧务处理（删贴等）的记录（需 STOKEN，吧名带“吧”字）
结果均支持文本搜索、翻页、复制、下载 txt。封禁功能暂不提供。
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


def _patch_aiotieba() -> None:
    """兼容补丁：aiotieba 4.7.1 解析吧务日志里的图片时，若图片 src 不匹配图床
    正则，会 `None.group(1)` 崩溃（报 'NoneType' object has no attribute 'group'）。
    这里改成不匹配时 hash 置空，避免整页查询失败。"""
    try:
        from aiotieba.api.get_bawu_postlogs import _classdef as m
    except Exception:
        return

    def _safe_media_from_xml(data_tag):
        img_item = data_tag.img
        if img_item is not None:
            src = img_item.get("original", "") or ""
            match = m._IMAGEHASH_EXP.search(src) if src else None
            hash_ = match.group(1) if match else ""
        else:
            src = ""
            hash_ = ""
        origin_src = data_tag.get("href", "") or ""
        return m.Media_postlog(src, origin_src, hash_)

    m.Media_postlog.from_xml = staticmethod(_safe_media_from_xml)

    _orig_page_from_xml = m.Page_postlog.from_xml

    def _safe_page_from_xml(data_soup):
        # 无记录时页面缺 breadcrumbs / 分页 div，原实现会崩；兜底返回空页。
        try:
            return _orig_page_from_xml(data_soup)
        except Exception:
            return m.Page_postlog()

    m.Page_postlog.from_xml = staticmethod(_safe_page_from_xml)

    def _safe_logs_from_xml(data_soup):
        # 无记录时页面没有 <tbody>，原实现会 None.find_all 崩溃；这里返回空。
        tbody = data_soup.find("tbody")
        rows = tbody.find_all("tr") if tbody is not None else []
        objs = [m.BawuPostLog.from_xml(t) for t in rows]
        return m.BawuPostLogs(objs, m.Page_postlog.from_xml(data_soup))

    m.BawuPostLogs.from_xml = staticmethod(_safe_logs_from_xml)


_patch_aiotieba()


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


PAGE_CAP = 200  # 单次请求的翻页硬上限


def _to_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ServiceError(f"{name} 应为数字。")


def _cap_pages(n) -> int:
    return max(1, min(_to_int(n, "翻页数"), PAGE_CAP))


async def _resolve_user(client, tieba_uid):
    """主页 id -> 用户信息，失败时给可读提示（上游报错是 int 解析噪音）。"""
    resp = await client.tieba_uid2user_info(_to_int(tieba_uid, "主页 id"))
    if getattr(resp, "err", None) is not None:
        raise ServiceError("未找到该主页 id 对应的用户，请确认填的是个人主页链接中的数字。")
    return resp


async def svc_me(bduss="", stoken="", **_):
    async with _client(bduss, stoken) as client:
        me = _check(await client.get_self_info(), "获取账号信息")
        return {"user_id": me.user_id, "show_name": _name(me), "user_name": me.user_name}


# --- 流式接口（NDJSON）：逐页 yield，前端边收边渲染 ---
# 每个 chunk 形如 {"type": "head"|"items"|"done", ...}

async def stream_thread(bduss="", stoken="", tid=None, max_pages=30, **_):
    if tid is None:
        raise ServiceError("请填写主题帖 tid。")
    tid = _to_int(tid, "tid")
    max_pages = _cap_pages(max_pages)
    async with _client(bduss, stoken) as client:
        pn = 1
        while pn <= max_pages:
            posts = _check(
                await client.get_posts(tid, pn, rn=30, with_comments=True, comment_rn=10),
                "获取楼层",
            )
            if pn == 1:
                t = posts.thread
                if not posts.objs and not t.tid:
                    raise ServiceError("未获取到该主题帖，请确认 tid 是否正确。")
                yield {"type": "head", "head": {
                    "fname": t.fname, "title": t.title,
                    "author": _name(t.user), "reply_num": t.reply_num}}
            floors = [{
                "floor": post.floor,
                "user": _name(post.user),
                "text": post.text,
                "time": _fmt_time(post.create_time),
                "comments": [
                    {"user": _name(c.user), "text": c.text, "time": _fmt_time(c.create_time)}
                    for c in post.comments
                ],
                "more_comments": max(post.reply_num - len(post.comments), 0),
            } for post in posts.objs]
            if floors:
                yield {"type": "items", "items": floors}
            if not posts.has_more:
                break
            pn += 1
        yield {"type": "done"}


async def stream_user(bduss="", stoken="", tieba_uid=None, max_pages=30, kind="posts", **_):
    """kind: posts=回复 / threads=主题帖 / all=两者。"""
    if tieba_uid is None:
        raise ServiceError("请填写用户贴吧主页 id。")
    if kind not in ("posts", "threads", "all"):
        raise ServiceError("内容类型无效。")
    max_pages = _cap_pages(max_pages)
    async with _client(bduss, stoken) as client:
        user = await _resolve_user(client, tieba_uid)
        uid = user.user_id or user.portrait
        if not uid:
            raise ServiceError("未找到该用户，请确认主页 id。")
        yield {"type": "head", "head": {
            "tieba_uid": _to_int(tieba_uid, "主页 id"), "show_name": _name(user)}}
        if kind in ("posts", "all"):
            async for chunk in _stream_posts(client, uid, max_pages):
                yield chunk
        if kind in ("threads", "all"):
            async for chunk in _stream_threads(client, uid, max_pages):
                yield chunk
        yield {"type": "done"}


async def _stream_posts(client, uid, max_pages):
    fcache, pn = {}, 1
    while pn <= max_pages:
        up = _check(await client.get_user_posts(uid, pn), "获取用户回复")
        if not up.objs:
            break
        items = []
        for group in up.objs:
            fid = group.fid
            if fid not in fcache:
                fcache[fid] = str(_check(await client.get_fname(fid), "解析贴吧名"))
            for r in group.objs:
                items.append({
                    "kind": "reply",
                    "fname": fcache[fid],
                    "tid": r.tid,
                    "pid": r.pid,
                    "link": f"https://tieba.baidu.com/p/{r.tid}?pid={r.pid}&cid=0#{r.pid}",
                    "time": _fmt_time(r.create_time),
                    "ts": r.create_time,
                    "text": r.text,
                    "is_comment": bool(getattr(r, "is_comment", False)),
                })
        if items:
            yield {"type": "items", "items": items}
        pn += 1


async def _stream_threads(client, uid, max_pages):
    pn = 1
    while pn <= max_pages:
        ut = _check(await client.get_user_threads(uid, pn), "获取用户主题帖")
        if not ut.objs:
            break
        items = [{
            "kind": "thread",
            "fname": t.fname,
            "tid": t.tid,
            "pid": t.pid,
            "link": f"https://tieba.baidu.com/p/{t.tid}",
            "time": _fmt_time(t.create_time),
            "ts": t.create_time,
            "title": t.title,
            "text": t.text,
            "reply_num": t.reply_num,
            "view_num": t.view_num,
            "is_comment": False,
        } for t in ut.objs]
        if items:
            yield {"type": "items", "items": items}
        if not getattr(ut, "has_more", False):
            break
        pn += 1


async def stream_logs(bduss="", stoken="", fname=None, tieba_uid=None, max_pages=30, **_):
    if not fname or tieba_uid is None:
        raise ServiceError("请填写贴吧名与被查询人主页 id。")
    if not stoken:
        raise ServiceError("查询吧务处理记录需要 STOKEN，请在页面顶部填写。")
    max_pages = _cap_pages(max_pages)
    async with _client(bduss, stoken) as client:
        user = await _resolve_user(client, tieba_uid)
        if not user.user_name:
            # 吧务后台按用户名检索；没有用户名时 search_value 为空，
            # 会返回全吧日志并被误标成此人，必须拦下。
            raise ServiceError("该用户没有用户名（仅有昵称），吧务日志按用户名检索，无法查询此用户。")
        yield {"type": "head", "head": {"target": _name(user), "fname": fname}}
        pn = 1
        while pn <= max_pages:
            res = await client.get_bawu_postlogs(
                fname, pn, search_value=user.user_name, search_type=BawuSearchType.USER)
            if getattr(res, "err", None) is not None:
                if "302" in str(res.err):
                    raise ServiceError(
                        "吧务处理记录鉴权失败（302）。请检查：①STOKEN 要用 .tieba.baidu.com 域下的那个；"
                        "②贴吧名需带“吧”字（如「yy小说吧」）；③当前账号须是该吧吧务。"
                    )
                raise ServiceError(f"获取吧务处理记录失败：{res.err}")
            items = [{
                "title": x.title, "text": x.text, "op_user": x.op_user_name,
                "op_type": x.op_type, "op_time": str(x.op_time),
            } for x in res.objs]
            if items:
                yield {"type": "items", "items": items}
            if not res.has_more:
                break
            pn += 1
        yield {"type": "done"}


async def stream_search(bduss="", stoken="", fname=None, query=None, max_pages=30, only_thread=False, **_):
    """吧内关键字搜索。按内容检索，能查到隐藏用户的公开发言。"""
    if not fname or not query:
        raise ServiceError("请填写贴吧名和关键字。")
    max_pages = _cap_pages(max_pages)
    only_thread = bool(only_thread)
    async with _client(bduss, stoken) as client:
        yield {"type": "head", "head": {"fname": fname, "query": query}}
        pn = 1
        while pn <= max_pages:
            res = _check(
                await client.search_exact(fname, query, pn, only_thread=only_thread),
                "搜索",
            )
            items = [{
                "kind": "search",
                "fname": x.fname or fname,
                "tid": x.tid,
                "pid": x.pid,
                "link": f"https://tieba.baidu.com/p/{x.tid}?pid={x.pid}&cid=0#{x.pid}",
                "time": _fmt_time(x.create_time),
                "ts": x.create_time,
                "title": x.title,
                "text": x.text,
                "user": x.show_name,
                "is_comment": bool(x.is_comment),
            } for x in res.objs]
            if items:
                yield {"type": "items", "items": items}
            if not getattr(res, "has_more", False):
                break
            pn += 1
        yield {"type": "done"}


async def svc_locate(bduss="", stoken="", tid=None, pid=None, is_comment=False, max_pages=50, **_):
    """按需定位一条回复/楼中楼在第几楼、第几页，并给出可精确跳转的链接。

    楼层号不在搜索类接口的返回里，只能回帖里逐页扫描匹配 pid，故设扫描上限。
    """
    if tid is None or pid is None:
        raise ServiceError("缺少 tid/pid。")
    tid, pid = _to_int(tid, "tid"), _to_int(pid, "pid")
    max_pages = min(_to_int(max_pages, "翻页数"), 50)  # 楼层页扫描上限，避免深帖跑太久
    async with _client(bduss, stoken) as client:
        deep_floors = []  # 楼中楼数超过内联的楼层，第一遍没命中时再深挖
        pn = 1
        while pn <= max_pages:
            # 内联抓每层前 50 条楼中楼，覆盖绝大多数情况
            posts = _check(
                await client.get_posts(tid, pn, rn=30, with_comments=True, comment_rn=50),
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
                    if post.reply_num > len(post.comments):
                        deep_floors.append((pn, post.pid, post.floor))
            if not posts.has_more:
                break
            pn += 1

        # 第二遍（仅楼中楼）：对楼中楼很多的楼层逐页翻 get_comments 深挖，带请求预算
        if is_comment:
            budget = 40
            for fpage, ppid, floor in deep_floors:
                cpn = 1
                while cpn <= 200 and budget > 0:
                    budget -= 1
                    cs = _check(await client.get_comments(tid, ppid, cpn), "定位楼中楼")
                    for cm in cs.objs:
                        if cm.pid == pid:
                            return {"found": True, "floor": floor, "page": fpage,
                                    "url": f"https://tieba.baidu.com/p/{tid}?pid={ppid}&cid={pid}#{pid}"}
                    if not cs.has_more:
                        break
                    cpn += 1
                if budget <= 0:
                    break
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


# 普通接口：返回完整 JSON {"data": ...}
ROUTES = {
    "/api/me": svc_me,
    "/api/locate": svc_locate,
}

# 流式接口：NDJSON，逐页 yield chunk
STREAM_ROUTES = {
    "/api/thread": stream_thread,
    "/api/user-posts": stream_user,
    "/api/postlogs": stream_logs,
    "/api/search": stream_search,
}


# ======================================================================
# HTTP 服务
# ======================================================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # 静默
        pass

    def _host_ok(self) -> bool:
        # 只接受本机 Host，防止恶意网页借 DNS rebinding 读取 /api/defaults 里的凭证
        host = self.headers.get("Host", "")
        return host.startswith("127.0.0.1") or host.startswith("localhost")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/defaults":
            self._send(200, json.dumps(DEFAULTS, ensure_ascii=False))
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, json.dumps({"error": "forbidden"}))
        stream_fn = STREAM_ROUTES.get(self.path)
        fn = ROUTES.get(self.path)
        if stream_fn is None and fn is None:
            return self._send(404, json.dumps({"error": "接口不存在"}))
        try:
            length = int(self.headers.get("Content-Length", 0))
            params = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "请求体解析失败"}))
        if stream_fn is not None:
            return self._stream(stream_fn, params)
        try:
            data = asyncio.run(fn(**params))
            self._send(200, json.dumps({"data": data}, ensure_ascii=False))
        except ServiceError as e:
            self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False))
        except TypeError:
            self._send(400, json.dumps({"error": "参数不完整"}, ensure_ascii=False))
        except Exception as e:
            self._send(502, json.dumps({"error": f"贴吧接口异常：{e}"}, ensure_ascii=False))

    def _stream(self, gen_fn, params):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def write(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        async def pump():
            try:
                async for chunk in gen_fn(**params):
                    write(chunk)
            except ServiceError as e:
                write({"type": "error", "error": str(e)})
            except TypeError:
                write({"type": "error", "error": "参数不完整"})
            except Exception as e:
                write({"type": "error", "error": f"贴吧接口异常：{e}"})

        try:
            asyncio.run(pump())
        except (BrokenPipeError, ConnectionResetError):
            pass


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
.tag2{background:#123a1d;color:#b6f0c0;border:1px solid #1c5a2c;border-radius:4px;padding:1px 6px;font-size:11px}
.stats{font-size:12px;color:var(--muted);margin-top:4px}
.form select{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px 12px;font-size:14px}
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
.barsel{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:7px;padding:7px 10px;font-size:13px;max-width:180px}
.barsel:focus{outline:none;border-color:var(--accent)}
.form .chk{flex-direction:row;align-items:center;gap:7px;flex:0 0 auto;cursor:pointer}
.form .chk input{width:auto}
.pager{display:flex;align-items:center;justify-content:center;gap:12px;padding:12px 18px;border-top:1px solid var(--border);background:var(--surface2);font-size:13px;color:var(--muted)}
.pager button:disabled{opacity:.4;cursor:not-allowed}
.pager select{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 8px;font-size:13px}
.jump{width:56px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 8px;font-size:13px}
.jump:focus{outline:none;border-color:var(--accent)}
.muted{color:var(--muted)}
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
  <button class="tab" data-tab="user">用户发言查询</button>
  <button class="tab" data-tab="search">关键字搜索</button>
  <button class="tab" data-tab="logs">吧务处理记录</button>
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
      <label>用户贴吧主页 id<input name="tieba_uid" type="number" required placeholder="个人主页链接中的数字"></label>
      <label>内容<select name="kind"><option value="posts">回复</option><option value="threads">主题帖</option><option value="all">全部</option></select></label>
      <label>最多翻页<input name="max_pages" type="number" value="30" min="1"></label>
      <button type="submit">查询</button>
    </form>
    <p class="hint">回复=TA 的跟帖与楼中楼；主题帖=TA 自己发的帖；全部=两者合并按时间排序。</p>
  </section>
  <section class="panel" id="p-search">
    <form class="form" data-form="search">
      <label>贴吧名（带“吧”字）<input name="fname" type="text" required placeholder="如 yy小说吧"></label>
      <label>关键字<input name="query" type="text" required placeholder="要搜索的词"></label>
      <label>最多翻页<input name="max_pages" type="number" value="10" min="1"></label>
      <label class="chk"><input name="only_thread" type="checkbox"> 只看主题帖</label>
      <button type="submit">搜索</button>
    </form>
    <p class="hint">在指定吧内按内容搜索帖子/回复。能查到隐藏用户在该吧的公开发言。</p>
  </section>
  <section class="panel" id="p-logs">
    <form class="form" data-form="logs">
      <label>贴吧名（带“吧”字）<input name="fname" type="text" required placeholder="如 yy小说吧"></label>
      <label>被查询人主页 id<input name="tieba_uid" type="number" required placeholder="个人主页链接中的数字"></label>
      <label>最多翻页<input name="max_pages" type="number" value="30" min="1"></label>
      <button type="submit">查询记录</button>
    </form>
    <p class="hint">查该用户在本吧被吧务处理（删贴等）的记录。需 STOKEN（.tieba.baidu.com 域）；吧名带“吧”字；账号须为该吧吧务。</p>
  </section>
  <section class="results" id="results" hidden>
    <div class="rhead">
      <div class="summary" id="summary"></div>
      <div class="ract"><select id="barfilter" class="barsel" hidden></select><input id="search" class="search" placeholder="搜索文本…"><button class="ghost" id="copy">复制文本</button><button class="ghost" id="dl">下载 .txt</button></div>
    </div>
    <div id="rbody"></div>
    <div class="pager" id="pager" hidden>
      <button class="ghost" id="prev">← 上一页</button>
      <span id="pageinfo"></span>
      <button class="ghost" id="next">下一页 →</button>
      <span>跳至</span><input id="jump" class="jump" type="number" min="1" title="输入页码回车跳转">
      <select id="per"><option value="50">50/页</option><option value="100">100/页</option><option value="200">200/页</option></select>
    </div>
  </section>
  <div class="box loading" id="loading" hidden><span class="spin"></span><span id="loadmsg">加载中…</span></div>
  <div class="box error" id="error" hidden></div>
</main>
<script>
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const cred={bduss:localStorage.bduss||"",stoken:localStorage.stoken||""};

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
    t.textContent=`已登录：${me.show_name} (uid ${me.user_id})`+(cred.stoken?"":" · 未填 STOKEN，吧务处理记录不可用");
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
  tab.classList.add("active");$("#p-"+tab.dataset.tab).classList.add("active");
  streamToken++;view=null;barFilter="";hideRes();   // 使进行中的流失效
});

// ---- 渲染配置（流式累积；itemHTML/match/head/empty 与数据分离）----
function setBody(h){$("#rbody").innerHTML=h}
let view=null, page=1, per=50, query="", barFilter="", streamToken=0;
const cache=new Map();  // 结果缓存：同一查询秒开

const RENDER={
  thread:{
    empty:"无楼层",
    head:m=>`<b>${esc(m.fname)}</b> · ${esc(m.title)} · 楼主 ${esc(m.author)} · 回复 ${m.reply_num}`,
    match:f=>f.user+" "+f.text+" "+f.comments.map(c=>c.user+" "+c.text).join(" "),
    itemHTML:f=>{
      let c=f.comments.map(x=>`<div class="cmt"><span class="cu">${esc(x.user)}</span>${esc(x.text)}</div>`).join("");
      if(f.more_comments>0)c+=`<div class="cmt muted">… 还有 ${f.more_comments} 条楼中楼未展开</div>`;
      return `<div class="floor"><div class="fhead"><span class="fno">#${f.floor}</span><span class="fuser">${esc(f.user)}</span><span class="ftime">${esc(f.time)}</span></div><div class="ftext">${esc(f.text)}</div>${c?`<div class="cmts">${c}</div>`:""}</div>`;
    },
  },
  user:{
    empty:"无内容：查“回复”为空可改选“主题帖”或“全部”；也可能对方未公开发言，或主页 id 有误。",
    head:m=>`<b>${esc(m.show_name)}</b> · 主页id ${m.tieba_uid}`,
    match:r=>r.fname+" "+(r.text||"")+" "+(r.title||""),
    itemHTML:r=> r.kind==="thread"
      ? `<div class="row"><div class="meta"><span class="chip">${esc(r.fname)}</span><span class="tag2">主题帖</span><span class="spacer"></span><span>${esc(r.time)}</span><a class="orig" href="${esc(r.link)}" target="_blank" rel="noopener">帖子 ↗</a></div><div class="rtext"><b>${esc(r.title)}</b></div><div class="stats">回复 ${r.reply_num} · 浏览 ${r.view_num}</div></div>`
      : `<div class="row"><div class="meta"><span class="chip">${esc(r.fname)}</span>${r.is_comment?'<span class="tag">楼中楼</span>':""}<span class="spacer"></span><span>${esc(r.time)}</span><button class="mini locbtn" data-tid="${r.tid}" data-pid="${r.pid}" data-c="${r.is_comment?1:0}">查楼层</button><a class="orig" href="${esc(r.link)}" target="_blank" rel="noopener">原帖 ↗</a></div><div class="rtext">${esc(r.text)}</div></div>`,
  },
  logs:{
    empty:"无吧务处理记录",
    head:m=>`被处理人 <b>${esc(m.target)}</b> · 吧 ${esc(m.fname)}`,
    match:x=>x.title+" "+x.text+" "+x.op_user,
    itemHTML:x=>`<div class="row"><div class="meta"><span class="optag">${esc(x.op_type)}</span><span>操作人 ${esc(x.op_user)}</span><span class="spacer"></span><span>${esc(x.op_time)}</span></div><div class="ltitle">${esc(x.title)}</div><div class="ltext">${esc(x.text)}</div></div>`,
  },
  search:{
    empty:"没有搜到内容",
    head:m=>`吧 <b>${esc(m.fname)}</b> · 关键字 “${esc(m.query)}”`,
    match:x=>x.fname+" "+(x.title||"")+" "+(x.text||"")+" "+(x.user||""),
    itemHTML:x=>`<div class="row"><div class="meta"><span class="chip">${esc(x.fname)}</span>${x.is_comment?'<span class="tag">楼中楼</span>':""}${x.user?`<span>${esc(x.user)}</span>`:""}<span class="spacer"></span><span>${esc(x.time)}</span><button class="mini locbtn" data-tid="${x.tid}" data-pid="${x.pid}" data-c="${x.is_comment?1:0}">查楼层</button><a class="orig" href="${esc(x.link)}" target="_blank" rel="noopener">原帖 ↗</a></div>${x.title?`<div class="ltitle">${esc(x.title)}</div>`:""}<div class="rtext">${esc(x.text)}</div></div>`,
  },
};
const FLOW={
  thread:{url:"/api/thread", rc:"thread", name:"thread.txt", sort:false},
  user:  {url:"/api/user-posts", rc:"user", name:"user_posts.txt", sort:true},
  search:{url:"/api/search", rc:"search", name:"search.txt", sort:false},
  logs:  {url:"/api/postlogs", rc:"logs", name:"records.txt", sort:false},
};

function updateBarFilter(){
  // 贴吧分类：仅“用户发言”视图，且结果含多个吧时显示
  const bf=$("#barfilter");
  const names=[...new Set(view.items.map(it=>it.fname))];
  if(view.formKind!=="user"||names.length<2){ bf.hidden=true; barFilter=""; return; }
  const counts={}; view.items.forEach(it=>counts[it.fname]=(counts[it.fname]||0)+1);
  names.sort((a,b)=>counts[b]-counts[a]);
  bf.innerHTML=`<option value="">全部吧 (${view.items.length})</option>`+
    names.map(n=>`<option value="${esc(n)}">${esc(n)} (${counts[n]})</option>`).join("");
  if(!counts[barFilter]) barFilter="";      // 选中的吧在增量中还没出现/消失
  bf.value=barFilter; bf.hidden=false;
}
function applyView(){
  if(!view)return;
  const rc=RENDER[view.rc];
  updateBarFilter();
  let base=barFilter?view.items.filter(it=>it.fname===barFilter):view.items;
  const q=query.trim().toLowerCase();
  const items=q?base.filter(it=>rc.match(it).toLowerCase().includes(q)):base;
  const total=base.length, shown=items.length, pages=Math.max(1,Math.ceil(shown/per));
  if(page>pages)page=pages; if(page<1)page=1;
  const slice=items.slice((page-1)*per,page*per);
  const head=view.meta?rc.head(view.meta):"结果";
  $("#summary").innerHTML=head+` · 共 <b>${total}</b> 条`+(q?` · 匹配 <b>${shown}</b>`:"")+(view.done?"":` · <span class="muted">加载中…</span>`);
  setBody(slice.length?slice.map(rc.itemHTML).join(""):`<div class="empty">${q?"没有匹配的内容":(view.done?rc.empty:"加载中…")}</div>`);
  const pager=$("#pager");
  if(shown>per){pager.hidden=false;$("#pageinfo").textContent=`第 ${page} / ${pages} 页`;$("#prev").disabled=page<=1;$("#next").disabled=page>=pages;}
  else pager.hidden=true;
}

// 逐行读取 NDJSON 流
async function streamNDJSON(url,body,onChunk){
  const res=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({bduss:cred.bduss,stoken:cred.stoken,...body})});
  if(!res.ok){let m;try{m=(await res.json()).error;}catch(_){}throw new Error(m||("HTTP "+res.status));}
  const reader=res.body.getReader(), dec=new TextDecoder(); let buf="";
  for(;;){
    const {value,done}=await reader.read(); if(done)break;
    buf+=dec.decode(value,{stream:true});
    let nl;
    while((nl=buf.indexOf("\n"))>=0){const line=buf.slice(0,nl).trim();buf=buf.slice(nl+1);if(line)onChunk(JSON.parse(line));}
  }
  buf=buf.trim(); if(buf)onChunk(JSON.parse(buf));
}

// 提交查询：命中缓存则秒开，否则流式边收边渲染
async function submit(formKind, body){
  const f=FLOW[formKind], key=formKind+"|"+JSON.stringify(body);
  $("#error").hidden=true;
  const token=++streamToken;
  if(cache.has(key)){
    const snap=cache.get(key);
    view={rc:f.rc, formKind, meta:snap.meta, items:snap.items.slice(), done:true};
    query="";barFilter="";page=1;$("#search").value=""; $("#results").hidden=false; load(false); applyView();
    return;
  }
  view={rc:f.rc, formKind, meta:null, items:[], done:false};
  query="";barFilter="";page=1;$("#search").value=""; $("#results").hidden=false; applyView();
  setLoad(0);
  let errMsg=null;
  try{
    await streamNDJSON(f.url, body, chunk=>{
      if(token!==streamToken)return;
      if(chunk.type==="head") view.meta=chunk.head;
      else if(chunk.type==="items"){ view.items.push(...chunk.items); if(f.sort)view.items.sort((a,b)=>b.ts-a.ts); applyView(); setLoad(view.items.length); }
      else if(chunk.type==="error") errMsg=chunk.error;
    });
  }catch(e){ if(token===streamToken) errMsg=e.message; }
  if(token!==streamToken)return;   // 已被新查询取代
  load(false); view.done=true; applyView();
  if(errMsg){ showErr(errMsg); if(!view.items.length)$("#results").hidden=true; }
  else cache.set(key,{meta:view.meta, items:view.items.slice()});
}

$$(".form").forEach(form=>form.onsubmit=async e=>{
  e.preventDefault();
  if(!cred.bduss){showErr("请先在右上角填写 BDUSS 并登录");return;}
  const body={};
  new FormData(form).forEach((v,k)=>body[k]=form.elements[k].type==="number"?Number(v):v);
  const btn=$("button[type=submit]",form); btn.disabled=true;
  try{ await submit(form.dataset.form, body); } finally{ btn.disabled=false; }
});

// 「查楼层」按需定位（事件委托，#rbody 常驻）
$("#rbody").addEventListener("click",async e=>{
  const b=e.target.closest(".locbtn"); if(!b||b.dataset.done)return;
  b.disabled=true; b.textContent="查询中…";
  try{
    const res=await api("/api/locate",{tid:Number(b.dataset.tid),pid:Number(b.dataset.pid),is_comment:b.dataset.c==="1"});
    if(res.found){
      b.textContent=`第 ${res.floor} 楼 · 第 ${res.page} 页`; b.classList.add("hit"); b.dataset.done=1;
      const a=b.parentElement.querySelector("a.orig"); if(a)a.href=res.url;
    }else{ b.textContent="未定位到"; b.title="帖子里找不到这条：可能已被删除/折叠，或在扫描范围外（超 50 页的深帖）"; b.disabled=false; }
  }catch(err){ b.textContent="查询失败"; b.disabled=false; }
});
$("#search").oninput=e=>{query=e.target.value;page=1;applyView()};
$("#barfilter").onchange=e=>{barFilter=e.target.value;page=1;applyView()};
$("#prev").onclick=()=>{if(page>1){page--;applyView();$("#rbody").scrollTop=0}};
$("#next").onclick=()=>{page++;applyView();$("#rbody").scrollTop=0};
$("#per").onchange=e=>{per=Number(e.target.value);page=1;applyView()};
$("#jump").onchange=e=>{const n=Number(e.target.value); if(n>=1){page=n;applyView();$("#rbody").scrollTop=0;} e.target.value="";};

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
  d.replies.forEach(r=>{
    if(r.kind==="thread"){
      L.push(`[主题帖] 贴吧: ${r.fname} 时间: ${r.time} 回复:${r.reply_num} 浏览:${r.view_num}`,`   ${r.title}`,`   ${r.link}`);
    }else{
      L.push(`贴吧名: ${r.fname} 链接: ${r.link} 回复时间: ${r.time}${r.is_comment?"（楼中楼）":""}`,`   ${r.text}`);
    }
  });
  L.push("",`共 ${d.replies.length} 条`);return L.join("\n")+"\n";
}
function logsText(d){
  if(!d.logs.length)return`未查询到 ${d.target} 在 ${d.fname} 的吧务处理记录。\n`;
  let L=[];d.logs.forEach(x=>L.push(`被处理人：${d.target}`,x.title,x.text,`${x.op_user} ${x.op_type} ${x.op_time}`,"==============="));
  return L.join("\n")+"\n";
}
function searchText(d){
  let L=[`吧: ${d.fname}  关键字: ${d.query}`,""];
  d.items.forEach(x=>{
    L.push(`[${x.is_comment?"楼中楼":"帖"}] ${x.user||""} ${x.time}  ${x.link}`);
    if(x.title)L.push(`   ${x.title}`);
    L.push(`   ${x.text}`);
  });
  L.push("",`共 ${d.items.length} 条`);return L.join("\n")+"\n";
}

// 由当前累积结果重建导出文本
function currentText(){
  if(!view||!view.meta)return "";
  const k=view.formKind, items=view.items, m=view.meta;
  const d = k==="thread"?{thread:m,floors:items}
          : k==="user"?{user:m,replies:items}
          : k==="search"?{fname:m.fname,query:m.query,items:items}
          : {target:m.target,fname:m.fname,logs:items};
  return ({thread:threadText,user:userText,search:searchText,logs:logsText})[k](d);
}

// 结果区
function hideRes(){$("#results").hidden=true;$("#error").hidden=true;$("#pager").hidden=true;$("#loading").hidden=true}
function load(on){$("#loading").hidden=!on}
function setLoad(n){$("#loading").hidden=false;$("#loadmsg").textContent=n?`加载中… 已 ${n} 条`:"加载中…";}
function showErr(m){const e=$("#error");e.textContent="出错了："+m;e.hidden=false}
$("#copy").onclick=async()=>{
  const txt=currentText(), b=$("#copy"),o=b.textContent;
  try{ await navigator.clipboard.writeText(txt); b.textContent="已复制"; }
  catch(e){  // 剪贴板权限被拒时的降级方案
    const ta=document.createElement("textarea"); ta.value=txt;
    document.body.appendChild(ta); ta.select();
    b.textContent=document.execCommand("copy")?"已复制":"复制失败";
    ta.remove();
  }
  setTimeout(()=>b.textContent=o,1200);
};
$("#dl").onclick=()=>{
  if(!view)return;
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([currentText()],{type:"text/plain;charset=utf-8"}));
  a.download=FLOW[view.formKind].name; a.click(); URL.revokeObjectURL(a.href);
};

async function init(){
  let def={bduss:"",stoken:""};       // 服务端默认（secret.py / 环境变量）
  try{ def=await fetch("/api/defaults").then(r=>r.json()); }catch(e){}
  cred.bduss=def.bduss||cred.bduss||"";      // secret.py 优先，覆盖浏览器里残留的旧凭证
  cred.stoken=def.stoken||cred.stoken||"";
  localStorage.bduss=cred.bduss; localStorage.stoken=cred.stoken;   // 同步，清掉旧值
  $("#bduss").value=cred.bduss;
  $("#stoken").value=cred.stoken;
  if(cred.bduss) login();
}
init();
</script></body></html>"""


if __name__ == "__main__":
    main()
