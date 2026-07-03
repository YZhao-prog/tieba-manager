#!/usr/bin/env python3
"""贴吧吧务管理台 —— 单文件版（后端 + 网页全在这一个文件里）。

用法：
    python3 tieba_tool.py
浏览器会自动打开 http://127.0.0.1:8000。

凭证：把 BDUSS/STOKEN 写进 secret.py（gitignore，见 secret.example.py）
可自动登录；也可以在页面右上角手动填。STOKEN 必须用 .tieba.baidu.com
域下的那个（.passport 域的会 302）。

工作流：顶部输入要管理的贴吧名并「进入」，然后：
    概览        吧信息 + 吧务列表（点某个吧务查 TA 的处理记录）
    处理记录    全吧最近或指定用户，合并删贴+封禁，
                可按操作类型/吧务/被处理人分类（需 STOKEN）
    用户发言    查某用户跨吧的回复/主题帖，可按贴吧分类（独立功能）
结果支持流式加载、文本搜索、排序、翻页、复制、下载 txt、导出网页。
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


# 吧务角色 -> 中文标签（按重要性排序）
BAWU_ROLES = [
    ("admin", "大吧主"), ("manager", "小吧主"),
    ("profess_admin", "专业吧主"), ("fourth_admin", "第四吧主"),
    ("voice_editor", "语音小编"), ("image_editor", "图片小编"),
    ("video_editor", "视频小编"), ("broadcast_editor", "广播小编"),
    ("journal_chief_editor", "期刊主编"), ("journal_editor", "期刊编辑"),
]


async def svc_overview(bduss="", stoken="", fname=None, **_):
    """进入某个吧的管理台：吧信息 + 吧务列表。"""
    if not fname:
        raise ServiceError("请填写要管理的贴吧名。")
    async with _client(bduss, stoken) as client:
        forum = _check(await client.get_forum(fname), "获取吧信息")
        bawu = await client.get_bawu_info(fname)
        if getattr(bawu, "err", None) is not None:
            raise ServiceError(f"获取吧务列表失败：{bawu.err}（确认吧名正确、账号已登录）")
        roles = []
        for attr, label in BAWU_ROLES:
            users = getattr(bawu, attr, []) or []
            if users:
                roles.append({"role": label, "users": [
                    {"name": _name(u), "user_name": u.user_name,
                     "user_id": u.user_id, "level": getattr(u, "level", 0)}
                    for u in users]})
        return {
            "forum": {
                "fname": forum.fname, "slogan": forum.slogan,
                "member_num": forum.member_num, "post_num": forum.post_num,
                "thread_num": forum.thread_num,
            },
            "bawu": roles,
            "bawu_total": len(getattr(bawu, "all", []) or []),
        }


# --- 流式接口（NDJSON）：逐页 yield，前端边收边渲染 ---
# 每个 chunk 形如 {"type": "head"|"items"|"done", ...}

async def stream_user(bduss="", stoken="", tieba_uid=None, max_pages=30, kind="posts", **_):
    """查某用户跨吧的发言。kind: posts=回复 / threads=主题帖 / all=两者。"""
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


async def _resolve_names(client, portraits):
    """并发解析 portrait -> 用户名（限流，失败留空）。"""
    sem = asyncio.Semaphore(5)

    async def one(p):
        async with sem:
            try:
                return p, _name(await client.get_user_info(p))
            except Exception:
                return p, ""

    return dict(await asyncio.gather(*[one(p) for p in portraits]))


def _has_uid(tieba_uid) -> bool:
    return tieba_uid not in (None, "", 0, "0")


def _op_ts(dt) -> int:
    try:
        return int(dt.timestamp())
    except Exception:
        return 0


async def _fill_targets(client, portraits, pcache):
    need = list(dict.fromkeys(p for p in portraits if p and p not in pcache))
    if need:
        pcache.update(await _resolve_names(client, need))


async def stream_logs(bduss="", stoken="", fname=None, tieba_uid=None, op_user=None, max_pages=30, **_):
    """吧务处理记录：合并帖子操作（删贴）与用户操作（封禁等）。三种模式：
    op_user  = 只看该吧务的操作（服务端按操作者精确检索，用于「锁定吧务」）；
    tieba_uid= 只看该被处理人；
    都留空   = 全吧最近记录。"""
    if not fname:
        raise ServiceError("请填写贴吧名。")
    if not stoken:
        raise ServiceError("查询吧务处理记录需要 STOKEN，请在页面顶部填写。")
    max_pages = _cap_pages(max_pages)
    op_user = (op_user or "").strip()
    async with _client(bduss, stoken) as client:
        if op_user:                              # 锁定某吧务：按操作者检索
            mode, who = "op", op_user
            stype, sval, fixed_target = BawuSearchType.OP, op_user, None
        elif _has_uid(tieba_uid):                # 锁定某被处理人：按用户检索
            user = await _resolve_user(client, tieba_uid)
            if not user.user_name:
                raise ServiceError("该用户没有用户名（仅有昵称），无法按用户名检索；可留空查全吧最近记录。")
            mode, who = "target", _name(user)
            stype, sval, fixed_target = BawuSearchType.USER, user.user_name, _name(user)
        else:                                    # 全吧最近
            mode, who = "whole", ""
            stype, sval, fixed_target = BawuSearchType.USER, "", None
        yield {"type": "head", "head": {"mode": mode, "who": who, "fname": fname}}
        pcache = {}

        def err_302(err):
            if "302" in str(err):
                raise ServiceError(
                    "吧务处理记录鉴权失败（302）。请检查：①STOKEN 要用 .tieba.baidu.com 域下的那个；"
                    "②贴吧名要填完整（部分吧名本身带“吧”字）；③当前账号须是该吧吧务。")
            raise ServiceError(f"获取吧务处理记录失败：{err}")

        # 1) 帖子操作（删贴等）
        pn = 1
        while pn <= max_pages:
            res = await client.get_bawu_postlogs(fname, pn, search_value=sval, search_type=stype)
            if getattr(res, "err", None) is not None:
                err_302(res.err)
            if fixed_target is None:
                await _fill_targets(client, [x.post_portrait for x in res.objs], pcache)
            items = [{
                "op_type": x.op_type, "op_user": x.op_user_name,
                "target": fixed_target if fixed_target is not None else pcache.get(x.post_portrait, ""),
                "op_time": str(x.op_time), "ts": _op_ts(x.op_time),
                "title": x.title, "text": x.text,
            } for x in res.objs]
            if items:
                yield {"type": "items", "items": items}
            if not res.has_more:
                break
            pn += 1

        # 2) 用户操作（封禁等）——权限或接口异常时静默跳过，不影响已取到的删贴
        pn = 1
        while pn <= max_pages:
            try:
                res = await client.get_bawu_userlogs(fname, pn, search_value=sval, search_type=stype)
            except Exception:
                break
            if getattr(res, "err", None) is not None:
                break
            if fixed_target is None:
                await _fill_targets(client, [x.user_portrait for x in res.objs], pcache)
            items = [{
                "op_type": x.op_type, "op_user": x.op_user_name,
                "target": fixed_target if fixed_target is not None else pcache.get(x.user_portrait, ""),
                "op_time": str(x.op_time), "ts": _op_ts(x.op_time),
                "duration": getattr(x, "op_duration", 0),
            } for x in res.objs]
            if items:
                yield {"type": "items", "items": items}
            if not getattr(res, "has_more", False):
                break
            pn += 1

        yield {"type": "done"}


def load_defaults() -> dict:
    """个人默认配置来源（环境变量 > 本地 secret.py）。

    secret.py 已在 .gitignore 中，不会被提交，可安全存放你的私人信息：
        BDUSS / STOKEN  登录凭证
        FNAME           你管理的贴吧名（预填「管理贴吧」）
        WATCH           常查的发言对象，[{"label": 备注, "uid": 主页id}, ...]
    """
    bduss = os.environ.get("TIEBA_BDUSS", "")
    stoken = os.environ.get("TIEBA_STOKEN", "")
    fname, watch = "", []
    try:
        import secret  # 本地、gitignore

        bduss = bduss or getattr(secret, "BDUSS", "")
        stoken = stoken or getattr(secret, "STOKEN", "")
        fname = getattr(secret, "FNAME", "") or ""
        watch = getattr(secret, "WATCH", []) or []
        bawu = getattr(secret, "BAWU", []) or []
    except ImportError:
        bawu = []
    fname = os.environ.get("TIEBA_FNAME", "") or fname
    # 规范化 watch 列表，容错
    clean = []
    for w in watch if isinstance(watch, (list, tuple)) else []:
        if isinstance(w, dict) and w.get("uid"):
            clean.append({"label": str(w.get("label") or w["uid"]), "uid": w["uid"]})
    bawu = [str(x).strip() for x in bawu if str(x).strip()] if isinstance(bawu, (list, tuple)) else []
    return {"bduss": bduss.strip(), "stoken": stoken.strip(),
            "fname": fname.strip(), "watch": clean, "bawu": bawu}


DEFAULTS = load_defaults()


# 普通接口：返回完整 JSON {"data": ...}
ROUTES = {
    "/api/me": svc_me,
    "/api/overview": svc_overview,
}

# 流式接口：NDJSON，逐页 yield chunk
STREAM_ROUTES = {
    "/api/user-posts": stream_user,
    "/api/postlogs": stream_logs,
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
        self.send_header("Cache-Control", "no-store")  # 避免浏览器缓存旧页面
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
.ws{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 24px;border-bottom:1px solid var(--border);background:var(--surface2)}
.wslabel{font-size:13px;color:var(--muted)}
.ws input{flex:1 1 260px;max-width:360px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 12px;font-size:14px}
.ws input:focus{outline:none;border-color:var(--accent)}
.ws button{background:var(--accent);color:#fff;border:none;padding:8px 18px;border-radius:8px;font-size:14px;cursor:pointer}
.overview{padding:8px 4px}
.fcard{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:18px 20px;margin-bottom:16px}
.fcard h2{margin:0 0 6px;font-size:18px}
.fstat{display:flex;gap:20px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin-top:8px}
.fstat b{color:var(--text)}
.brole{margin-bottom:14px}
.brole .rt{font-size:13px;color:var(--muted);margin-bottom:6px}
.bwrap{display:flex;flex-wrap:wrap;gap:8px}
.bchip{display:inline-flex;align-items:center;gap:6px;background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:4px 12px;font-size:13px;cursor:pointer}
.bchip:hover{border-color:var(--accent);color:var(--accent)}
.bchip .lv{font-size:11px;color:var(--muted)}
.bchip.hist{border-style:dashed;color:var(--muted)}
.bchip.hist:hover{color:var(--accent)}
.watch{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.wbtn{background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:20px;padding:5px 14px;font-size:13px;cursor:pointer}
.wbtn:hover{border-color:var(--accent);color:var(--accent)}
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
.no-spin::-webkit-outer-spin-button,.no-spin::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.no-spin{-moz-appearance:textfield;appearance:textfield}
.rtext{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;margin-top:6px;font-size:14px}
.ltitle{font-weight:500;margin-top:6px}.ltext{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--muted);margin-top:4px;font-size:13.5px}
.optag{background:#3a2a12;color:#ffd7a8;border:1px solid #5a3f1c;border-radius:4px;padding:1px 6px;font-size:11px}
.optag.ban{background:#3a1418;color:#ffb4b4;border-color:#5a2228}
.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 18px;border-bottom:1px solid var(--border);background:var(--surface2)}
.tlabel{font-size:12px;color:var(--muted)}
.box{margin-top:24px;padding:16px 18px;border-radius:var(--r);display:flex;align-items:center;gap:12px;font-size:14px}
.loading{background:var(--surface);border:1px solid var(--border);color:var(--muted)}
.error{background:#2a1416;border:1px solid #52262a;color:#ffb4b4;white-space:pre-wrap}
.spin{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:s .8s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
.search{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:7px;padding:7px 12px;font-size:13px;width:160px}
.search:focus{outline:none;border-color:var(--accent)}
.barsel{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:7px;padding:7px 10px;font-size:13px;max-width:180px}
.barsel:focus{outline:none;border-color:var(--accent)}
.catbox{position:relative;display:inline-block}
.catmenu{position:absolute;top:100%;left:0;z-index:30;margin-top:4px;min-width:200px;max-height:260px;overflow:auto;background:var(--surface2);border:1px solid var(--border);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.45)}
.catmenu .opt{padding:7px 12px;font-size:13px;cursor:pointer;display:flex;justify-content:space-between;gap:16px;white-space:nowrap}
.catmenu .opt:hover{background:var(--surface)}
.catmenu .opt.sel{color:var(--accent)}
.catmenu .cnt{color:var(--muted)}
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
<div class="ws">
  <span class="wslabel">管理贴吧</span>
  <input id="wsbar" placeholder="输入你管理的吧名后回车/进入" autocomplete="off">
  <button id="wsgo">进入</button>
  <span id="wsinfo" class="muted"></span>
</div>
<nav class="tabs">
  <button class="tab active" data-tab="overview">概览</button>
  <button class="tab" data-tab="logs">处理记录</button>
  <button class="tab" data-tab="user">用户发言</button>
</nav>
<main>
  <section class="panel active" id="p-overview">
    <div id="overview" class="overview"><p class="hint">在上方输入你管理的贴吧名并「进入」，这里会显示吧信息和吧务列表。</p></div>
  </section>
  <section class="panel" id="p-logs">
    <form class="form" data-form="logs">
      <label>查询方式<select name="scope" id="logscope">
        <option value="whole">全吧最近</option>
        <option value="op">锁定吧务</option>
        <option value="target">锁定被处理人</option>
      </select></label>
      <label class="sc-op" hidden>吧务名<input name="op_user" list="bawulist" placeholder="输入/选择（含已撤职吧务）"><datalist id="bawulist"></datalist></label>
      <label class="sc-target" hidden>被处理人主页 id<input name="tieba_uid" type="number" class="no-spin" placeholder="个人主页链接中的数字"></label>
      <label>抓取页数<input name="max_pages" type="number" value="10" min="1" title="从贴吧最多抓取多少页数据；越大越慢。与下方结果“每页显示”无关"></label>
      <button type="submit">查询记录</button>
    </form>
    <p class="hint">当前吧的<b>删贴+封禁</b>记录。锁定后可在结果区按操作类型/吧务/被处理人再细分。也可在「概览」点吧务名直接锁定。</p>
  </section>
  <section class="panel" id="p-user">
    <form class="form" data-form="user">
      <label>用户贴吧主页 id<input name="tieba_uid" type="number" class="no-spin" required placeholder="个人主页链接中的数字"></label>
      <label>内容<select name="kind"><option value="all">全部</option><option value="posts">回复</option><option value="threads">主题帖</option></select></label>
      <label>抓取页数<input name="max_pages" type="number" value="30" min="1" title="从贴吧最多抓取多少页数据；与下方结果“每页显示”无关"></label>
      <button type="submit">查询</button>
    </form>
    <div id="watch" class="watch" hidden></div>
    <p class="hint">查某用户<b>跨吧</b>的发言（回复+主题帖），不限于当前管理的吧；结果可按贴吧分类。常查对象可写进 secret.py 的 WATCH，显示为上方快捷按钮。</p>
  </section>
  <section class="results" id="results" hidden>
    <div class="rhead">
      <div class="summary" id="summary"></div>
      <div class="ract"><button class="ghost" id="copy">复制</button><button class="ghost" id="dl">下载txt</button><button class="ghost" id="dlhtml">导出网页</button></div>
    </div>
    <div class="toolbar" id="toolbar" hidden>
      <span class="tlabel" id="catbyLabel" hidden>分类</span>
      <select id="catby" class="barsel" hidden><option value="op_type">按操作类型</option><option value="op_user">按吧务</option><option value="target">按被处理人</option></select>
      <span class="catbox" id="catbox" hidden><input id="catfilter" class="barsel" placeholder="筛选，可搜" autocomplete="off"><div class="catmenu" id="catmenu" hidden></div></span>
      <span class="tlabel" id="sortLabel" hidden>排序</span>
      <select id="sortsel" class="barsel" hidden><option value="new">时间新→旧</option><option value="old">时间旧→新</option></select>
      <input id="search" class="search" placeholder="搜索文本…">
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

// ---- 工作台：先选定要管理的吧 ----
let currentFname=localStorage.fname||"";   // 记住上次管理的吧（仅存本地浏览器）
function switchTab(name){
  $$(".tab").forEach(t=>t.classList.toggle("active", t.dataset.tab===name));
  $$(".panel").forEach(p=>p.classList.remove("active"));
  $("#p-"+name).classList.add("active");
  streamToken++;view=null;catFilter="";sortMode="new";hideRes();
}
// 点标签：处理记录直接出默认（全吧最近），不用再点搜索
$$(".tab").forEach(tab=>tab.onclick=()=>{
  const name=tab.dataset.tab;
  switchTab(name);
  if(name==="logs" && currentFname){ $("#logscope").value="whole"; syncScope(); runLogs({}); }
});

async function enterBar(){
  const f=$("#wsbar").value.trim();
  if(!f){$("#wsinfo").textContent="请输入吧名";return;}
  if(!cred.bduss){$("#wsinfo").textContent="请先在右上角登录";return;}
  if(f!==currentFname){ seenOps.clear(); cache.clear(); lastOverview=null; }  // 换吧：清运行时候选与缓存
  currentFname=f; localStorage.fname=f;
  switchTab("overview");
  // 先用本地缓存秒出，再联网刷新
  const cached=loadForumData(f);
  if(cached){
    seenOps=new Set(cached.ops||[]);
    if(cached.ov) renderOverview(cached.ov);
    $("#wsinfo").textContent="缓存 · 刷新中…";
  }else{
    $("#wsinfo").textContent="加载中…"; $("#overview").innerHTML='<p class="hint">加载中…</p>';
  }
  try{
    renderOverview(await api("/api/overview",{fname:f}));
    saveForumData();
    $("#wsinfo").textContent="✓ "+f;
  }catch(e){
    if(!cached){ $("#wsinfo").textContent=""; $("#overview").innerHTML=`<p class="hint" style="color:var(--err)">出错了：${esc(e.message)}</p>`; }
    else $("#wsinfo").textContent="✓ "+f+"（用缓存，刷新失败）";
  }
}
// 「锁定吧务」候选 = secret.BAWU（你固定的名单，含已撤职）∪ 当前吧务 ∪ 记录里出现过的操作者
let bawuNames=[], seenOps=new Set(), pinnedBawu=[], lastOverview=null;
function historicalOps(){   // 历史/撤职：固定名单 ∪ 搜过的操作者，减去当前在册
  const cur=new Set(bawuNames);
  return [...new Set([...pinnedBawu,...seenOps])].filter(n=>n&&!cur.has(n)).sort((a,b)=>a.localeCompare(b,"zh"));
}
function refreshBawuList(){
  const cur=new Set(bawuNames);
  const all=[...new Set([...pinnedBawu,...bawuNames,...seenOps])].filter(Boolean).sort((a,b)=>a.localeCompare(b,"zh"));
  // 只给历史/撤职的加标注；在册的不标
  $("#bawulist").innerHTML=all.map(u=>`<option value="${esc(u)}"${cur.has(u)?"":' label="历史/撤职"'}>`).join("");
}
// 按吧持久化：概览数据 + 累计操作者（含撤职吧务），存 localStorage，下次秒出
function fdKey(fn){ return "fd:"+(fn||currentFname); }
function saveForumData(){
  if(!currentFname)return;
  try{ localStorage.setItem(fdKey(), JSON.stringify({ov:lastOverview, ops:[...seenOps], ts:Date.now()})); }catch(e){}
}
function loadForumData(fn){
  try{ return JSON.parse(localStorage.getItem(fdKey(fn))||"null"); }catch(e){ return null; }
}
function harvestOps(){   // 从当前处理记录结果里收集操作者名，补进候选并持久化（撤职吧务靠这个留存）
  if(!view||view.formKind!=="logs")return;
  const before=seenOps.size;
  view.items.forEach(x=>{ if(x.op_user)seenOps.add(x.op_user); });
  refreshBawuList();
  if(seenOps.size!==before) saveForumData();
}
// 常查发言对象 → 快捷按钮（数据来自 secret.py 的 WATCH，私密）
function renderWatch(list){
  const box=$("#watch");
  if(!list||!list.length){ box.hidden=true; box.innerHTML=""; return; }
  box.hidden=false;
  box.innerHTML='<span class="tlabel">常查：</span>'+
    list.map(w=>`<button class="wbtn" data-uid="${esc(String(w.uid))}">${esc(w.label)}</button>`).join("");
}
$("#watch").addEventListener("click",e=>{
  const b=e.target.closest(".wbtn"); if(!b)return;
  const form=$("#p-user form");
  form.elements.tieba_uid.value=b.dataset.uid;
  form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit",{cancelable:true}));
});
function renderOverview(d){
  lastOverview=d;
  const f=d.forum, n=x=>Number(x||0).toLocaleString();
  bawuNames=[...new Set(d.bawu.flatMap(r=>r.users.map(u=>u.user_name||u.name)))].filter(Boolean);
  refreshBawuList();
  const roles=d.bawu.map(r=>`<div class="brole"><div class="rt">${esc(r.role)}（${r.users.length}）</div><div class="bwrap">`+
    r.users.map(u=>`<span class="bchip" data-uname="${esc(u.user_name||u.name)}">${esc(u.name)}${u.level?`<span class="lv">Lv${u.level}</span>`:""}</span>`).join("")+`</div></div>`).join("");
  const hist=historicalOps();
  const histCard=
    `<div class="fcard"><h2 style="font-size:15px;margin-bottom:12px">历史/撤职吧务（${hist.length}）· 点击查记录</h2>`+
    `<div class="bwrap">`+
    (hist.length ? hist.map(u=>`<span class="bchip hist" data-uname="${esc(u)}">${esc(u)}</span>`).join("")
                 : `<span class="muted">暂无。写进 secret.py 的 BAWU、或搜过的老吧务会出现在这里。</span>`)+
    `</div></div>`;
  $("#overview").innerHTML=
    `<div class="fcard"><h2>${esc(f.fname)}</h2><div class="muted">${esc(f.slogan||"")}</div>`+
    `<div class="fstat"><span>关注 <b>${n(f.member_num)}</b></span><span>主题帖 <b>${n(f.thread_num)}</b></span><span>回复 <b>${n(f.post_num)}</b></span><span>吧务 <b>${d.bawu_total}</b></span></div></div>`+
    `<div class="fcard"><h2 style="font-size:15px;margin-bottom:12px">吧务列表 · 点击查看 TA 的处理记录</h2>${roles||'<p class="hint">无</p>'}</div>`+
    histCard;
}
$("#wsgo").onclick=enterBar;
$("#wsbar").onkeydown=e=>{ if(e.key==="Enter")enterBar(); };
// 点概览里的吧务名字 → 锁定该吧务（同步表单为「锁定吧务」并查询）
$("#overview").addEventListener("click",e=>{
  const c=e.target.closest(".bchip"); if(!c)return;
  switchTab("logs");
  $("#logscope").value="op"; $("#p-logs [name=op_user]").value=c.dataset.uname; syncScope();
  runLogs({op_user:c.dataset.uname});
});
// 查询方式切换：显隐对应输入
function syncScope(){
  const s=$("#logscope").value;
  $(".sc-op").hidden = s!=="op";
  $(".sc-target").hidden = s!=="target";
}
$("#logscope").onchange=syncScope;
// 统一入口：处理记录查询（extra 可带 op_user 或 tieba_uid）
function runLogs(extra){
  if(!currentFname){ showErr("请先在上方输入并「进入」一个贴吧"); $("#results").hidden=false; return; }
  // 一旦锁定某吧务（尤其是手动输入的老吧务），立即记进候选缓存，下次直接可选
  if(extra && extra.op_user){ const n=String(extra.op_user).trim();
    if(n && !seenOps.has(n)){ seenOps.add(n); refreshBawuList(); saveForumData();
      if(lastOverview) renderOverview(lastOverview); } }   // 同步刷新概览的历史吧务区
  const mp=Number($("#p-logs [name=max_pages]").value)||30;
  submit("logs", {fname:currentFname, max_pages:mp, ...extra});
}

// ---- 渲染配置（流式累积；itemHTML/match/head/empty 与数据分离）----
function setBody(h){$("#rbody").innerHTML=h}
let view=null, page=1, per=50, query="", catFilter="", sortMode="new", logCatBy="op_type", streamToken=0;
const cache=new Map();  // 结果缓存：同一查询秒开
const SORTABLE={logs:1, user:1};             // 可按时间排序的视图
function catField(){return view.formKind==="logs"?logCatBy:view.formKind==="user"?"fname":null;}
function catLabelText(){return view.formKind==="user"?"全部吧"
  :logCatBy==="op_type"?"全部操作":logCatBy==="op_user"?"全部吧务":"全部被处理人";}

const RENDER={
  logs:{
    empty:"无吧务处理记录",
    head:m=> m.mode==="op" ? `🔒 吧务 <b>${esc(m.who)}</b> 的操作 · ${esc(m.fname)}`
           : m.mode==="target" ? `被处理人 <b>${esc(m.who)}</b> · ${esc(m.fname)}`
           : `最近处理记录 · <b>${esc(m.fname)}</b>`,
    match:x=>(x.op_type||"")+" "+(x.op_user||"")+" "+(x.target||"")+" "+(x.title||"")+" "+(x.text||""),
    itemHTML:x=>{
      const ban=x.duration!==undefined;
      const meta=`<div class="meta"><span class="optag${ban?" ban":""}">${esc(x.op_type)}</span><span>吧务 ${esc(x.op_user)}</span>${x.target?`<span class="chip">被处理 ${esc(x.target)}</span>`:""}${ban&&x.duration?`<span class="tag">${x.duration}天</span>`:""}<span class="spacer"></span><span>${esc(x.op_time)}</span></div>`;
      return ban
        ? `<div class="row">${meta}</div>`
        : `<div class="row">${meta}<div class="ltitle">${esc(x.title||"")}</div><div class="ltext">${esc(x.text||"")}</div></div>`;
    },
  },
  user:{
    empty:"无内容：查“回复”为空可改选“主题帖”或“全部”；也可能对方未公开发言，或主页 id 有误。",
    head:m=>`<b>${esc(m.show_name)}</b> · 主页id ${m.tieba_uid}`,
    match:r=>r.fname+" "+(r.text||"")+" "+(r.title||""),
    itemHTML:r=> r.kind==="thread"
      ? `<div class="row"><div class="meta"><span class="chip">${esc(r.fname)}</span><span class="tag2">主题帖</span><span class="spacer"></span><span>${esc(r.time)}</span><a href="${esc(r.link)}" target="_blank" rel="noopener">帖子 ↗</a></div><div class="rtext"><b>${esc(r.title)}</b></div><div class="stats">回复 ${r.reply_num} · 浏览 ${r.view_num}</div></div>`
      : `<div class="row"><div class="meta"><span class="chip">${esc(r.fname)}</span>${r.is_comment?'<span class="tag">楼中楼</span>':""}<span class="spacer"></span><span>${esc(r.time)}</span><a href="${esc(r.link)}" target="_blank" rel="noopener">原帖 ↗</a></div><div class="rtext">${esc(r.text)}</div></div>`,
  },
};
const FLOW={
  logs:  {url:"/api/postlogs", rc:"logs", name:"records.txt"},
  user:  {url:"/api/user-posts", rc:"user", name:"user_posts.txt"},
};

function catVal(it,field){ return it[field]||"(空)"; }
let catOptions=[], catTotal=0;   // 当前分类选项 [{name,count}]
function updateCatFilter(){
  // 分类可搜索下拉：用户发言→按吧名（含回复/主题帖分计），搜索→按发帖人，吧务记录→按吧务/被处理人
  $("#catby").hidden = view.formKind!=="logs";   // 处理记录才显示“按操作/吧务/被处理人”切换
  const box=$("#catbox"), field=catField();
  const names=field?[...new Set(view.items.map(it=>catVal(it,field)))]:[];
  if(!field||names.length<2){ box.hidden=true; $("#catmenu").hidden=true; catOptions=[]; return; }
  const agg={};
  view.items.forEach(it=>{ const k=catVal(it,field); const a=agg[k]||(agg[k]={count:0,reply:0,thread:0});
    a.count++; if(it.kind==="thread")a.thread++; else a.reply++; });
  // 按类别名排序（不按数量），便于稳定查找
  catOptions=Object.keys(agg).sort((a,b)=>a.localeCompare(b,"zh")).map(n=>({name:n,...agg[n]}));
  catTotal=view.items.length;
  $("#catfilter").placeholder=`${catLabelText()}（${catTotal}），可搜`;
  box.hidden=false;
  if(!$("#catmenu").hidden) renderCatMenu($("#catfilter").value);  // 菜单开着就刷新
}
function catCnt(o){ return view.formKind==="user" ? `${o.reply}回·${o.thread}帖` : `${o.count}`; }
function renderCatMenu(text){
  const t=(text||"").trim().toLowerCase();
  const opts=catOptions.filter(o=>o.name.toLowerCase().includes(t));
  const menu=$("#catmenu");
  menu.innerHTML=`<div class="opt${catFilter?"":" sel"}" data-v="">全部（${catTotal}）</div>`+
    opts.map(o=>`<div class="opt${o.name===catFilter?" sel":""}" data-v="${esc(o.name)}"><span>${esc(o.name)}</span><span class="cnt">${catCnt(o)}</span></div>`).join("");
  menu.hidden=false;
}
function pickCat(v){
  catFilter=v; $("#catfilter").value=v; $("#catmenu").hidden=true; page=1; applyView();
}
function applyView(){
  if(!view)return;
  const rc=RENDER[view.rc];
  $("#toolbar").hidden=false;
  updateCatFilter();
  // 排序（仅可排序视图）
  let base=view.items.slice();
  if(SORTABLE[view.formKind]){ base.sort((a,b)=> sortMode==="old" ? a.ts-b.ts : b.ts-a.ts); $("#sortsel").hidden=false; }
  else $("#sortsel").hidden=true;
  // 工具条标签随控件显隐
  $("#catbyLabel").hidden = $("#catby").hidden && $("#catbox").hidden;
  $("#sortLabel").hidden = $("#sortsel").hidden;
  // 分类筛选
  const field=catField();
  if(field && catFilter){const cf=catFilter.toLowerCase(); base=base.filter(it=>catVal(it,field).toLowerCase().includes(cf));}
  // 文本搜索
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

function resetControls(){ query="";catFilter="";sortMode="new";logCatBy="op_type";page=1;$("#search").value="";$("#sortsel").value="new";$("#catby").value="op_type";$("#catfilter").value="";$("#catmenu").hidden=true; }
// 锁定后调整“分类维度”下拉：去掉无意义的维度，并设合理默认
//   锁吧务(op)   → 可按 操作类型/被处理人（不含“按吧务”，只有一个人）
//   锁被处理人   → 可按 操作类型/吧务（不含“按被处理人”）
//   全吧         → 三者都有
function defaultCatBy(meta){
  if(!meta||view.formKind!=="logs")return;
  const opts=[["op_type","按操作类型"]];
  if(meta.mode!=="op") opts.push(["op_user","按吧务"]);
  if(meta.mode!=="target") opts.push(["target","按被处理人"]);
  $("#catby").innerHTML=opts.map(([v,l])=>`<option value="${v}">${l}</option>`).join("");
  logCatBy = meta.mode==="target" ? "op_user" : "op_type";
  $("#catby").value=logCatBy;
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
    resetControls(); defaultCatBy(snap.meta); $("#results").hidden=false; load(false); applyView(); harvestOps();
    return;
  }
  view={rc:f.rc, formKind, meta:null, items:[], done:false};
  resetControls(); $("#results").hidden=false; applyView();
  setLoad(0);
  let errMsg=null;
  try{
    await streamNDJSON(f.url, body, chunk=>{
      if(token!==streamToken)return;
      if(chunk.type==="head"){ view.meta=chunk.head; defaultCatBy(chunk.head); }
      else if(chunk.type==="items"){ view.items.push(...chunk.items); applyView(); harvestOps(); setLoad(view.items.length); }
      else if(chunk.type==="error") errMsg=chunk.error;
    });
  }catch(e){ if(token===streamToken) errMsg=e.message; }
  if(token!==streamToken)return;   // 已被新查询取代
  load(false); view.done=true; applyView(); harvestOps();
  if(errMsg){ showErr(errMsg); if(!view.items.length)$("#results").hidden=true; }
  else cache.set(key,{meta:view.meta, items:view.items.slice()});
}

$$(".form").forEach(form=>form.onsubmit=async e=>{
  e.preventDefault();
  if(!cred.bduss){showErr("请先在右上角填写 BDUSS 并登录");return;}
  const kind=form.dataset.form, body={};
  new FormData(form).forEach((v,k)=>body[k]=form.elements[k].type==="number"?Number(v):v);
  const btn=$("button[type=submit]",form); btn.disabled=true;
  try{
    if(kind==="logs"){                       // 处理记录：按查询方式锁定
      const s=body.scope;
      if(s==="op"){ const op=(body.op_user||"").trim(); if(!op){showErr("请输入或选择要锁定的吧务名");return;} runLogs({op_user:op}); }
      else if(s==="target"){ if(!body.tieba_uid){showErr("请输入被处理人主页 id");return;} runLogs({tieba_uid:body.tieba_uid}); }
      else runLogs({});
    } else await submit(kind, body);
  } finally{ btn.disabled=false; }
});
$("#search").oninput=e=>{query=e.target.value;page=1;applyView()};
// 分类可搜索下拉
$("#catfilter").oninput=e=>{catFilter=e.target.value.trim();page=1;applyView();renderCatMenu(e.target.value)};
$("#catfilter").onfocus=e=>{e.target.select();renderCatMenu("");};  // 点开显示全部选项，便于换选
$("#catby").onchange=e=>{logCatBy=e.target.value;catFilter="";$("#catfilter").value="";page=1;applyView()};
$("#catmenu").onclick=e=>{const o=e.target.closest(".opt"); if(o)pickCat(o.dataset.v)};
document.addEventListener("click",e=>{ if(!e.target.closest("#catbox")) $("#catmenu").hidden=true; });
$("#sortsel").onchange=e=>{sortMode=e.target.value;page=1;applyView()};
$("#prev").onclick=()=>{if(page>1){page--;applyView();$("#rbody").scrollTop=0}};
$("#next").onclick=()=>{page++;applyView();$("#rbody").scrollTop=0};
$("#per").onchange=e=>{per=Number(e.target.value);page=1;applyView()};
$("#jump").onchange=e=>{const n=Number(e.target.value); if(n>=1){page=n;applyView();$("#rbody").scrollTop=0;} e.target.value="";};

// 导出文本
function logsText(d){
  const cap=d.mode==="op"?` · 吧务 ${d.who} 的操作`:d.mode==="target"?` · 被处理人 ${d.who}`:"（全吧最近）";
  let L=[`吧务处理记录 · 吧: ${d.fname}${cap}`,""];
  if(!d.logs.length)L.push("（无记录）");
  d.logs.forEach(x=>{
    L.push(`【${x.op_type}】吧务 ${x.op_user}${x.target?` · 被处理 ${x.target}`:""}${x.duration!==undefined&&x.duration?` · ${x.duration}天`:""} · ${x.op_time}`);
    if(x.title!==undefined){ L.push(`   ${x.title}`, `   ${x.text}`); }
    L.push("===============");
  });
  L.push("",`共 ${d.logs.length} 条`);return L.join("\n")+"\n";
}
function userText(d){
  let L=[`用户发言: ${d.show_name} (主页id=${d.tieba_uid})`,""];
  d.items.forEach(r=>{
    if(r.kind==="thread") L.push(`[主题帖] 贴吧: ${r.fname} 时间: ${r.time} 回复:${r.reply_num} 浏览:${r.view_num}`,`   ${r.title}`,`   ${r.link}`);
    else L.push(`贴吧: ${r.fname} 链接: ${r.link} 时间: ${r.time}${r.is_comment?"（楼中楼）":""}`,`   ${r.text}`);
  });
  L.push("",`共 ${d.items.length} 条`);return L.join("\n")+"\n";
}
// 由当前累积结果重建导出文本
function currentText(){
  if(!view||!view.meta)return "";
  const m=view.meta;
  if(view.formKind==="user") return userText({show_name:m.show_name,tieba_uid:m.tieba_uid,items:view.items});
  return logsText({mode:m.mode,who:m.who,fname:m.fname,logs:view.items});
}

// 导出为独立网页（自包含，可直接发给别人用浏览器打开）
const EXPORT_CSS=`body{font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:820px;margin:0 auto;padding:24px;color:#1a1a1a;background:#fff}
h1{font-size:20px;margin:0 0 4px}.sub{color:#888;font-size:13px;margin-bottom:18px;border-bottom:1px solid #eee;padding-bottom:12px}
.card{border-bottom:1px solid #eee;padding:12px 0}.hd{margin-bottom:4px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.no{color:#2563eb;font-weight:600}.t{color:#999;font-size:12px}.tx{white-space:pre-wrap;overflow-wrap:anywhere}
.cm{margin:6px 0 0 16px;padding-left:12px;border-left:2px solid #eee;color:#444;font-size:13.5px}
.chip{background:#f1f1f1;border-radius:4px;padding:1px 7px;font-size:12px}
.tag{background:#dbeafe;color:#1d4ed8;border-radius:4px;padding:1px 6px;font-size:11px}
.tag2{background:#dcfce7;color:#15803d;border-radius:4px;padding:1px 6px;font-size:11px}
.optag{background:#fef3c7;color:#b45309;border-radius:4px;padding:1px 6px;font-size:11px}
.ttl{font-weight:600;margin:2px 0}.st{color:#999;font-size:12px}a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}`;
function buildHTML(){
  if(!view||!view.meta)return "";
  const m=view.meta, items=view.items;
  const A=(u,t)=>`<a href="${esc(u)}" target="_blank" rel="noopener">${t}</a>`;
  let title, sub, rows;
  if(view.formKind==="user"){
    title=`${esc(m.show_name)} 的发言`;
    sub=`主页id ${m.tieba_uid} · 共 ${items.length} 条`;
    rows=items.map(r=> r.kind==="thread"
      ? `<div class="card"><div class="hd"><span class="chip">${esc(r.fname)}</span><span class="tag2">主题帖</span><span class="t">${esc(r.time)}</span>${A(r.link,"帖子↗")}</div><div class="ttl">${esc(r.title)}</div><div class="st">回复 ${r.reply_num} · 浏览 ${r.view_num}</div></div>`
      : `<div class="card"><div class="hd"><span class="chip">${esc(r.fname)}</span>${r.is_comment?'<span class="tag">楼中楼</span>':""}<span class="t">${esc(r.time)}</span>${A(r.link,"原帖↗")}</div><div class="tx">${esc(r.text)}</div></div>`).join("");
  }else{
    const cap=m.mode==="op"?`吧务 ${esc(m.who)} 的操作记录`:m.mode==="target"?`${esc(m.who)} 被处理记录`:`${esc(m.fname)} 吧务处理记录`;
    title=cap;
    sub=`吧 ${esc(m.fname)}${m.mode==="whole"?"（全吧最近）":""} · 共 ${items.length} 条`;
    rows=items.map(x=>{
      const ban=x.duration!==undefined;
      const hd=`<div class="hd"><span class="optag">${esc(x.op_type)}</span>吧务 ${esc(x.op_user)}${x.target?`<span class="chip">被处理 ${esc(x.target)}</span>`:""}${ban&&x.duration?`<span class="tag">${x.duration}天</span>`:""}<span class="t">${esc(x.op_time)}</span></div>`;
      return ban?`<div class="card">${hd}</div>`
                :`<div class="card">${hd}<div class="ttl">${esc(x.title||"")}</div><div class="tx">${esc(x.text||"")}</div></div>`;
    }).join("");
  }
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title><style>${EXPORT_CSS}</style></head><body><h1>${title}</h1><div class="sub">${sub} · 导出于 ${new Date().toLocaleString()}</div>${rows||'<div class="st">无内容</div>'}</body></html>`;
}

// 结果区
function hideRes(){$("#results").hidden=true;$("#error").hidden=true;$("#pager").hidden=true;$("#loading").hidden=true;$("#toolbar").hidden=true;$("#catmenu").hidden=true}
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
function saveBlob(text,type,name){
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([text],{type}));
  a.download=name; a.click(); URL.revokeObjectURL(a.href);
}
$("#dl").onclick=()=>{ if(view) saveBlob(currentText(),"text/plain;charset=utf-8",FLOW[view.formKind].name); };
$("#dlhtml").onclick=()=>{ if(view) saveBlob(buildHTML(),"text/html;charset=utf-8",FLOW[view.formKind].name.replace(".txt",".html")); };

async function init(){
  let def={bduss:"",stoken:"",fname:"",watch:[]};   // 个人配置（secret.py / 环境变量）
  try{ def=await fetch("/api/defaults").then(r=>r.json()); }catch(e){}
  cred.bduss=def.bduss||cred.bduss||"";      // secret.py 优先，覆盖浏览器里残留的旧凭证
  cred.stoken=def.stoken||cred.stoken||"";
  localStorage.bduss=cred.bduss; localStorage.stoken=cred.stoken;   // 同步，清掉旧值
  $("#bduss").value=cred.bduss;
  $("#stoken").value=cred.stoken;
  currentFname=def.fname||currentFname||"";   // secret.py 的 FNAME 优先，否则用本地记忆的
  $("#wsbar").value=currentFname;
  renderWatch(def.watch||[]);                  // 常查发言对象 → 快捷按钮
  pinnedBawu=def.bawu||[]; refreshBawuList();   // 固定吧务名单（含已撤职）→ 锁定候选
  // 预载缓存：概览秒出、锁定吧务候选（含已见过的撤职吧务）立即可用
  if(currentFname){
    const cached=loadForumData(currentFname);
    if(cached){ seenOps=new Set(cached.ops||[]); if(cached.ov) renderOverview(cached.ov);
      $("#wsinfo").textContent="缓存 "+currentFname+"（点「进入」刷新）"; refreshBawuList(); }
  }
  if(cred.bduss) login();
}
init();
</script></body></html>"""


if __name__ == "__main__":
    main()
