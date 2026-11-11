#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cas_web.py — cas_tool 的 Web 后台(iOS 友好),配合 cas_tool.py 使用。

设计:
  - 网盘访问全部走 OpenList 接口(OPENLIST_URL / OPENLIST_TOKEN),
    等于复用你在 OpenList 里已经配好的全部挂载(天翼 189pc、移动、本地等):
    浏览目录、流式下载算哈希生成 .cas、把 .cas 上传回网盘、种子秒传保存。
  - 移动版 .cas 还原仍直调 139 接口(CAS139_PHONE / CAS139_AUTH),
    因为 OpenList 的种子秒传保存没有接移动 CAS。
  - 生成的 / 上传的 .cas 存放在服务端数据目录(CAS_DATA_DIR,容器里是 /data),
    形成一个 cas 库,手机上可随时下载 / 还原 / 删除。
  - 页面为单文件原生 HTML/CSS/JS,大按钮、响应式,针对 iPhone Safari 操作优化。

环境变量:
  OPENLIST_URL / OPENLIST_TOKEN   必需(网盘挂载接口)
  CAS139_PHONE / CAS139_AUTH      移动还原时必需(或 YDYP_PHONE / YDYP_AUTH)
  WEB_PASSWORD                    可选:设置后打开页面需要先输口令(局域网也建议设)
  PORT                            默认 8099
  CAS_DATA_DIR                    默认 ./data(容器里 /data)
"""

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cas_tool as ct

VERSION = "1.2.0"
PORT = int(os.environ.get("PORT", "8099"))
DATA_DIR = os.environ.get("CAS_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
LIB_DIR = os.path.join(DATA_DIR, "caslib")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
MIB = 1024 * 1024

os.makedirs(LIB_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# OpenList 接口
# ---------------------------------------------------------------------------

def ol_cfg():
    return (os.environ.get("OPENLIST_URL", "").strip(),
            os.environ.get("OPENLIST_TOKEN", "").strip())


def ol_post(route, payload):
    base, token = ol_cfg()
    if not base or not token:
        raise RuntimeError("未配置 OPENLIST_URL / OPENLIST_TOKEN")
    st, txt = ct.openlist_post(base, token, route, payload)
    try:
        resp = json.loads(txt)
    except ValueError:
        raise RuntimeError("OpenList %s 返回非 JSON (HTTP %s)" % (route, st))
    if resp.get("code") != 200:
        raise RuntimeError("OpenList %s 失败: %s" % (route, resp.get("message") or txt[:200]))
    return resp.get("data")


def ol_list(path):
    data = ol_post("/fs/list", {"path": path, "page": 1, "per_page": 0, "refresh": False})
    content = (data or {}).get("content") or []
    return [{"name": e.get("name", ""), "size": e.get("size", 0),
             "is_dir": bool(e.get("is_dir"))} for e in content]


def ol_get(path):
    return ol_post("/fs/get", {"path": path})


def ol_download_iter(path, info=None):
    """流式下载 OpenList 里的一个文件,逐块 yield bytes。
    先用 fs/get 的 raw_url;失败再退回 /d/<path> 带令牌。"""
    base, token = ol_cfg()
    if info is None:
        info = ol_get(path)
    raw_url = (info or {}).get("raw_url") or ""
    urls = []
    if raw_url:
        urls.append((raw_url, {}))
    urls.append((base.rstrip("/") + "/d" + urllib.parse.quote(path),
                 {"Authorization": token}))
    last_err = None
    for url, headers in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=120)
            return resp  # 调用方负责 read/close
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError("下载 %s 失败: %s" % (path, last_err))


def ol_put(path, data: bytes):
    base, token = ol_cfg()
    url = base.rstrip("/") + "/api/fs/put"
    req = urllib.request.Request(url, data=data, method="PUT", headers={
        "Authorization": token,
        "File-Path": urllib.parse.quote(path),
        "Content-Type": "application/octet-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            txt = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
    resp_json = json.loads(txt)
    if resp_json.get("code") != 200:
        raise RuntimeError("上传 %s 失败: %s" % (path, resp_json.get("message") or txt[:200]))


# ---------------------------------------------------------------------------
# 任务(后台生成 .cas,带进度)
# ---------------------------------------------------------------------------

JOBS = {}
JOBS_LOCK = threading.Lock()


def job_new(kind, title):
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {"id": jid, "kind": kind, "title": title, "status": "running",
                     "progress": None, "message": "", "result": None,
                     "ts": int(time.time())}
    return jid


def job_update(jid, **kw):
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid].update(kw)


def lib_path_for(cas_name):
    safe = cas_name.replace("/", "_").replace("\\", "_").strip()
    if not safe:
        safe = "unnamed.cas"
    path = os.path.join(LIB_DIR, safe)
    if os.path.exists(path):
        stem, ext = os.path.splitext(safe)
        path = os.path.join(LIB_DIR, "%s.%d%s" % (stem, int(time.time()), ext))
    return path


def run_gen_job(jid, ol_path, size, upload_back):
    """从 OpenList 流式读取文件,一遍算完 md5/逐片 md5/sha256,生成双填 .cas。"""
    try:
        job_update(jid, message="连接 OpenList ...")
        resp = ol_download_iter(ol_path)
        slice_size = ct.cas_slice_size(size)
        whole_md5 = hashlib.md5()
        whole_sha = hashlib.sha256()
        pieces = []
        buf = bytearray()
        done = 0
        with resp:
            while True:
                chunk = resp.read(4 * MIB)
                if not chunk:
                    break
                whole_sha.update(chunk)
                buf.extend(chunk)
                done += len(chunk)
                while len(buf) >= slice_size:
                    piece = bytes(buf[:slice_size])
                    del buf[:slice_size]
                    pieces.append(hashlib.md5(piece).hexdigest().upper())
                    whole_md5.update(piece)
                if size > 0:
                    job_update(jid, progress=round(done * 100 / size, 1),
                               message="已读 %s / %s" % (ct.fmt_size(done), ct.fmt_size(size)))
        if buf:
            piece = bytes(buf)
            pieces.append(hashlib.md5(piece).hexdigest().upper())
            whole_md5.update(piece)
        if done != size:
            raise RuntimeError("读到的字节数 %d 与文件大小 %d 不符,已中止(文件可能在变动)" % (done, size))
        file_md5 = whole_md5.hexdigest().upper()
        name = ol_path.rstrip("/").split("/")[-1]
        cas_obj = {
            "name": name,
            "size": size,
            "md5": file_md5,
            "sliceMd5": ct.slice_md5_from_pieces(pieces, file_md5),
            "sha256": whole_sha.hexdigest(),
            "recover_pass": "",
            "create_time": str(int(time.time())),
        }
        cas_bytes = ct.encode_cas(cas_obj)
        out_path = lib_path_for(name + ".cas")
        with open(out_path, "wb") as f:
            f.write(cas_bytes)
        result = {"cas_file": os.path.basename(out_path), "name": name, "size": size}
        if upload_back:
            job_update(jid, message="上传 .cas 回网盘同目录 ...")
            ol_put(ol_path + ".cas", cas_bytes)
            result["uploaded_to"] = ol_path + ".cas"
        job_update(jid, status="done", progress=100, message="完成", result=result)
    except Exception as e:  # noqa: BLE001
        job_update(jid, status="error", message=str(e)[:300])


# ---------------------------------------------------------------------------
# 还原(与 cas_tool CLI 同逻辑,输入为 cas 字节)
# ---------------------------------------------------------------------------

def restore_139(cas_bytes, parent="/"):
    obj = ct.decode_cas_bytes(cas_bytes)
    sha = str(obj.get("sha256", "")).strip().lower()
    if not sha:
        return False, "这份 .cas 没有 sha256(是天翼版),不能还原到移动"
    phone, auth = ct.get_139_credentials()
    if not phone or not auth:
        return False, "服务端未配置 CAS139_PHONE / CAS139_AUTH,无法直调移动接口"
    name = obj.get("name", "")
    size = int(obj.get("size", 0))
    import mimetypes
    payload = {
        "contentHash": sha,
        "contentHashAlgorithm": "SHA256",
        "contentType": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "parallelUpload": False,
        "partInfos": ct.build_139_part_infos(size),
        "size": size,
        "parentFileId": parent or "/",
        "name": name,
        "type": "file",
        "fileRenameMode": "auto_rename",
    }
    basic = ct.build_basic_auth(phone, auth)
    st, txt = ct.personal_post(basic, ct.FILE_CREATE_URL, payload)
    if st != 200:
        return False, "file/create HTTP %s: %s" % (st, txt[:200])
    try:
        resp = json.loads(txt)
    except ValueError:
        return False, "file/create 返回非 JSON: %s" % txt[:200]
    if str(resp.get("code")) not in ("0000", "0"):
        return False, "file/create 失败 code=%s: %s" % (resp.get("code"), txt[:300])
    data = resp.get("data") or {}
    ret_parts = data.get("partInfos") or []
    has_upload = any(isinstance(p, dict) and p.get("uploadUrl") for p in ret_parts)
    if data.get("exist") is True or (data.get("fileId") and not has_upload):
        return True, "✓ 秒传命中:《%s》已还原到移动云盘" % name
    return False, ("未命中: 移动云盘服务端没有 sha256=%s... 的现成文件,"
                   "这份 cas 本身没问题,是云端没有源" % sha[:16])


def restore_189(cas_bytes, cas_name, target):
    obj = ct.decode_cas_bytes(cas_bytes)
    if not str(obj.get("md5", "")).strip():
        return False, "这份 .cas 没有 md5(是移动版),不能还原到天翼"
    return restore_openlist(cas_bytes, cas_name, target)


def restore_openlist(cas_bytes, cas_name, target):
    """经 OpenList 种子秒传保存还原到任意挂载目录(天翼/移动都可):
    本工具不预判哈希够不够,把 cas 原样交给 OpenList,以它的预检结果为准。"""
    if not target:
        return False, "请填写 OpenList 里的目标目录"
    payload = {
        "seed_data": base64.b64encode(cas_bytes).decode("ascii"),
        "file_name": cas_name or "file.cas",
        "path": target,
        "selected_files": [0],
    }
    try:
        cap_note = ""
        try:
            cap = ol_post("/fs/seed/capabilities", payload)
            cap_note = json.dumps(cap, ensure_ascii=False)[:300]
        except RuntimeError as e:
            cap_note = "预检未通过: %s" % e
        ol_post("/fs/seed/rapid_upload", payload)
        return True, "✓ 已提交秒传保存到 %s(预检: %s)" % (target, cap_note)
    except RuntimeError as e:
        return False, "秒传保存失败: %s" % e


def cas_summary(cas_bytes):
    obj = ct.decode_cas_bytes(cas_bytes)
    return {
        "name": obj.get("name", ""), "size": obj.get("size", ""),
        "md5": obj.get("md5", ""), "sliceMd5": obj.get("sliceMd5", ""),
        "sha256": obj.get("sha256", ""), "kind": ct.cas_kind(obj),
        "create_time": obj.get("create_time", ""),
        "has_recover_pass": bool(obj.get("recover_pass")),
    }


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

SESSIONS = set()
SESSIONS_LOCK = threading.Lock()


def make_session():
    tok = uuid.uuid4().hex + uuid.uuid4().hex
    with SESSIONS_LOCK:
        SESSIONS.add(tok)
    return tok


def session_ok(handler):
    if not WEB_PASSWORD:
        return True
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("cas_sess="):
            with SESSIONS_LOCK:
                return part[9:] in SESSIONS
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "cas-web/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- 基础读写 -----------------------------------------------------------
    def send_bytes(self, data, ctype="application/json; charset=utf-8", status=200, extra=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False), status=status)

    def ok(self, data=None, msg="ok"):
        self.send_json({"ok": True, "msg": msg, "data": data})

    def fail(self, msg, status=200):
        self.send_json({"ok": False, "msg": str(msg), "data": None}, status=status)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    # -- 路由 ---------------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/healthz":
            return self.send_bytes("ok", "text/plain")
        if path == "/":
            if WEB_PASSWORD and not session_ok(self):
                return self.send_bytes(LOGIN_HTML, "text/html; charset=utf-8")
            return self.send_bytes(INDEX_HTML, "text/html; charset=utf-8")
        if not session_ok(self):
            return self.fail("未登录", status=401)
        try:
            if path == "/api/status":
                return self.api_status()
            if path == "/api/list":
                return self.api_list(qs)
            if path == "/api/jobs":
                with JOBS_LOCK:
                    jobs = sorted(JOBS.values(), key=lambda j: -j["ts"])
                return self.ok(jobs[:30])
            if path == "/api/lib":
                return self.api_lib()
            if path == "/api/lib/download":
                return self.api_lib_download(qs)
            return self.fail("无此路径", status=404)
        except Exception as e:  # noqa: BLE001
            return self.fail(e)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            body = self.read_json()
            if WEB_PASSWORD and body.get("password") == WEB_PASSWORD:
                tok = make_session()
                return self.send_bytes(
                    json.dumps({"ok": True, "msg": "ok", "data": None}),
                    extra={"Set-Cookie": "cas_sess=%s; Path=/; HttpOnly; SameSite=Lax" % tok})
            if not WEB_PASSWORD:
                return self.ok()
            return self.fail("口令不对")
        if not session_ok(self):
            return self.fail("未登录", status=401)
        try:
            body = self.read_json()
            if path == "/api/inspect":
                cas_bytes = base64.b64decode(body.get("cas_b64", ""))
                return self.ok(cas_summary(cas_bytes))
            if path == "/api/gen":
                return self.api_gen(body)
            if path == "/api/restore139":
                cas_bytes = base64.b64decode(body.get("cas_b64", ""))
                ok, msg = restore_139(cas_bytes, body.get("parent") or "/")
                return self.ok(msg=msg) if ok else self.fail(msg)
            if path == "/api/restore189":
                cas_bytes = base64.b64decode(body.get("cas_b64", ""))
                ok, msg = restore_189(cas_bytes, body.get("cas_name") or "file.cas",
                                      body.get("target") or "")
                return self.ok(msg=msg) if ok else self.fail(msg)
            if path == "/api/restoreol":
                cas_bytes = base64.b64decode(body.get("cas_b64", ""))
                ok, msg = restore_openlist(cas_bytes, body.get("cas_name") or "file.cas",
                                            body.get("target") or "")
                return self.ok(msg=msg) if ok else self.fail(msg)
            if path == "/api/lib/add":
                return self.api_lib_add(body)
            if path == "/api/lib/delete":
                return self.api_lib_delete(body)
            if path == "/api/lib/restore":
                return self.api_lib_restore(body)
            return self.fail("无此路径", status=404)
        except Exception as e:  # noqa: BLE001
            return self.fail(e)

    # -- API 实现 -------------------------------------------------------------
    def api_status(self):
        base, token = ol_cfg()
        status = {"version": VERSION, "openlist_configured": bool(base and token),
                  "openlist_ok": False, "openlist_msg": "",
                  "m139_configured": bool(ct.get_139_credentials()[0]
                                          and ct.get_139_credentials()[1])}
        if base and token:
            try:
                ol_list("/")
                status["openlist_ok"] = True
                status["openlist_msg"] = "已连通 " + base
            except Exception as e:  # noqa: BLE001
                status["openlist_msg"] = str(e)[:200]
        return self.ok(status)

    def api_list(self, qs):
        path = (qs.get("path") or ["/"])[0]
        entries = ol_list(path)
        return self.ok({"path": path, "entries": entries})

    def api_gen(self, body):
        path = body.get("path") or ""
        size = int(body.get("size") or 0)
        if not path or size <= 0:
            return self.fail("缺少 path / size")
        jid = job_new("gen", "生成 cas: " + path)
        t = threading.Thread(target=run_gen_job,
                             args=(jid, path, size, bool(body.get("upload_back"))),
                             daemon=True)
        t.start()
        return self.ok({"job_id": jid})

    def api_lib(self):
        items = []
        for fn in sorted(os.listdir(LIB_DIR)):
            fp = os.path.join(LIB_DIR, fn)
            if not os.path.isfile(fp):
                continue
            item = {"file": fn, "mtime": int(os.path.getmtime(fp))}
            try:
                with open(fp, "rb") as f:
                    item.update(cas_summary(f.read()))
            except Exception as e:  # noqa: BLE001
                item["kind"] = "解析失败: %s" % e
            items.append(item)
        items.sort(key=lambda x: -x["mtime"])
        return self.ok(items)

    def api_lib_download(self, qs):
        name = (qs.get("name") or [""])[0]
        fp = os.path.join(LIB_DIR, os.path.basename(name))
        if not os.path.isfile(fp):
            return self.fail("文件不存在", status=404)
        with open(fp, "rb") as f:
            data = f.read()
        disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(os.path.basename(name))
        return self.send_bytes(data, "application/octet-stream",
                               extra={"Content-Disposition": disp})

    def api_lib_add(self, body):
        cas_bytes = base64.b64decode(body.get("cas_b64", ""))
        summary = cas_summary(cas_bytes)  # 先验证能解析
        name = (summary.get("name") or "file") + ".cas"
        fp = lib_path_for(name)
        with open(fp, "wb") as f:
            f.write(cas_bytes)
        return self.ok({"file": os.path.basename(fp), **summary})

    def api_lib_delete(self, body):
        fp = os.path.join(LIB_DIR, os.path.basename(body.get("file") or ""))
        if os.path.isfile(fp):
            os.remove(fp)
            return self.ok(msg="已删除")
        return self.fail("文件不存在")

    def api_lib_restore(self, body):
        fp = os.path.join(LIB_DIR, os.path.basename(body.get("file") or ""))
        if not os.path.isfile(fp):
            return self.fail("文件不存在")
        with open(fp, "rb") as f:
            cas_bytes = f.read()
        to = body.get("to")
        if to == "mobile":
            ok, msg = restore_139(cas_bytes, body.get("parent") or "/")
        elif to == "tianyi":
            ok, msg = restore_189(cas_bytes, os.path.basename(fp), body.get("target") or "")
        elif to == "openlist":
            ok, msg = restore_openlist(cas_bytes, os.path.basename(fp), body.get("target") or "")
        else:
            return self.fail("to 只能是 mobile / tianyi / openlist")
        return self.ok(msg=msg) if ok else self.fail(msg)


# ---------------------------------------------------------------------------
# 页面(单文件,移动优先)
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>CAS 工具 · 登录</title><style>
body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f2f2f7;color:#111}
.wrap{max-width:420px;margin:15vh auto;padding:20px}
input,button{width:100%;box-sizing:border-box;font-size:17px;padding:13px;border-radius:10px;border:1px solid #c7c7cc;margin-top:10px}
button{background:#007aff;color:#fff;border:none;font-weight:600}
</style></head><body><div class="wrap"><h2>🔐 CAS 双云工具</h2>
<input id="pw" type="password" placeholder="访问口令" autocomplete="current-password">
<button onclick="login()">进入</button><p id="m" style="color:#c00"></p></div>
<script>
async function login(){
  const r = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: document.getElementById('pw').value})});
  const d = await r.json();
  if(d.ok){ location.reload(); } else { document.getElementById('m').textContent = d.msg; }
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
</script></body></html>"""

INDEX_HTML = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>CAS 双云工具</title><style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f2f2f7;color:#111}
@media (prefers-color-scheme:dark){body{background:#000;color:#eee}.card{background:#1c1c1e!important}.row{border-color:#333!important}input,textarea,select{background:#2c2c2e;color:#eee;border-color:#444!important}}
.wrap{max-width:760px;margin:0 auto;padding:12px 12px 40px}
h1{font-size:20px;margin:8px 0}
h2{font-size:16px;margin:0 0 8px}
.card{background:#fff;border-radius:14px;padding:14px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}
button{font-size:15px;padding:10px 14px;border-radius:10px;border:none;background:#007aff;color:#fff;font-weight:600;min-height:44px}
button.grey{background:#8e8e93}button.green{background:#34c759}button.red{background:#ff3b30}
button.small{padding:6px 10px;min-height:36px;font-size:13px}
input,textarea{width:100%;font-size:16px;padding:10px;border-radius:10px;border:1px solid #c7c7cc;margin:6px 0}
textarea{min-height:64px}
.row{display:flex;align-items:center;gap:8px;padding:10px 4px;border-bottom:1px solid #e5e5ea}
.row .grow{flex:1;min-width:0;word-break:break-all}
.bar{height:8px;background:#e5e5ea;border-radius:4px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:#007aff}
.mut{color:#8e8e93;font-size:13px}
.msg{white-space:pre-wrap;word-break:break-all;font-size:14px;margin-top:8px}
.err{color:#ff3b30}.good{color:#248a3d}
.kv{font-size:13px;color:#666;word-break:break-all}
</style></head><body><div class="wrap">
<h1>🗂 CAS 双云工具 <span class="mut" id="ver"></span></h1>
<div class="card"><div id="status" class="msg">检查连接中…</div></div>

<div class="card"><h2>① 还原一份 .cas</h2>
<input type="file" id="casfile" accept=".cas,.txt">
<textarea id="castext" placeholder="或把 .cas 的内容(Base64 文本)粘贴到这里"></textarea>
<button onclick="inspectCas()">解析</button>
<button class="grey" onclick="saveToLib()">存入 cas 库</button>
<div id="inspect" class="msg"></div>
<div id="restorebox" style="display:none">
<div class="row"><input id="target189" class="grow" placeholder="OpenList 目标目录(如 /天翼云盘/电影 或 /移动云盘/电影)">
<button class="small grey" onclick="togglePicker()">📁 选目录</button></div>
<div id="picker" style="display:none">
<div class="row"><button class="small grey" onclick="pickerUp()">⬆ 上级</button><span class="grow" id="pickerpath">/</span></div>
<div id="pickerlist"></div>
<div class="row"><button class="small green" onclick="pickerUse()">✔ 用当前目录</button>
<button class="small grey" onclick="togglePicker()">关闭</button></div>
</div>
<input id="parent139" value="/" placeholder="移动直调时父目录 fileId(根目录填 /)">
<label class="mut"><input type="checkbox" id="savecas" style="width:auto" checked> 还原时同时把这份 .cas 存入 cas 库</label>
<button class="green" onclick="doRestore('mobile')">还原到移动(直调139接口)</button>
<button class="green" onclick="doRestore('tianyi')">还原到天翼(经OpenList)</button>
<button class="green" onclick="doRestore('openlist')">经 OpenList 还原到上面目录(移动挂载可试)</button>
</div><div id="restoremsg" class="msg"></div></div>

<div class="card"><h2>② 浏览网盘 · 生成 .cas</h2>
<div class="row"><button class="small grey" onclick="goUp()">⬆ 上级</button>
<span class="grow" id="curpath">/</span>
<button class="small grey" onclick="loadList()">刷新</button></div>
<label class="mut"><input type="checkbox" id="upback" style="width:auto"> 生成后把 .cas 上传回同目录</label>
<div id="entries"></div></div>

<div class="card"><h2>③ cas 库(存在群晖上)</h2><div id="lib"></div></div>

<div class="card"><h2>④ 任务</h2><div id="jobs" class="mut">暂无</div></div>
</div>
<script>
let curPath='/', curCasB64='', curCasName='';
const $=id=>document.getElementById(id);
async function api(url, body){
  const opt = body? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {};
  const r = await fetch(url,opt); const d = await r.json();
  if(r.status===401){ location.reload(); throw new Error('未登录'); }
  return d;
}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function status(){
  const d = await api('/api/status');
  if(!d.ok){ $('status').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  const s=d.data; $('ver').textContent='v'+s.version;
  $('status').innerHTML =
    (s.openlist_configured? (s.openlist_ok? '🟢 OpenList '+esc(s.openlist_msg) : '🔴 OpenList 未连通: '+esc(s.openlist_msg)) : '🔴 未配置 OPENLIST_URL / OPENLIST_TOKEN')
    +'<br>'+(s.m139_configured? '🟢 移动直调接口已配置凭据' : '🟡 未配置 CAS139_PHONE / CAS139_AUTH(移动还原不可用,天翼不受影响)');
}
function fileToB64(file){return new Promise((res,rej)=>{const r=new FileReader();
  r.onload=()=>res(r.result.split(',')[1]); r.onerror=rej; r.readAsDataURL(file);});}
async function getCasB64(){
  const f=$('casfile').files[0];
  if(f){ curCasName=f.name; return await fileToB64(f); }
  const t=$('castext').value.trim();
  if(t){ curCasName='pasted.cas'; return btoa(unescape(encodeURIComponent(t))); }
  return '';
}
async function inspectCas(){
  curCasB64 = await getCasB64();
  if(!curCasB64){ $('inspect').innerHTML='<span class=err>先选择 .cas 文件或粘贴内容</span>'; return; }
  const d = await api('/api/inspect',{cas_b64:curCasB64});
  if(!d.ok){ $('inspect').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; $('restorebox').style.display='none'; return; }
  const s=d.data;
  $('inspect').innerHTML='类型:<b>'+esc(s.kind)+'</b><br>名字:'+esc(s.name)+
    '<br>大小:'+esc(s.size)+' 字节<br><span class=kv>md5: '+esc(s.md5||'(空)')+
    '<br>sliceMd5: '+esc(s.sliceMd5||'(空)')+'<br>sha256: '+esc(s.sha256||'(空)')+'</span>';
  $('restorebox').style.display='block';
}
async function saveToLib(){
  if(!curCasB64) curCasB64 = await getCasB64();
  if(!curCasB64){ alert('先选择 .cas 文件'); return; }
  const d = await api('/api/lib/add',{cas_b64:curCasB64});
  $('restoremsg').innerHTML = d.ok? '<span class=good>已存入 cas 库: '+esc(d.data.file)+'</span>' : '<span class=err>'+esc(d.msg)+'</span>';
  if(d.ok) loadLib();
}
let pickerPath='/';
function togglePicker(){
  const p=$('picker'); const show=p.style.display==='none';
  p.style.display=show?'block':'none'; if(show) loadPicker();
}
async function loadPicker(){
  const d = await api('/api/list?path='+encodeURIComponent(pickerPath));
  if(!d.ok){ $('pickerlist').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  $('pickerpath').textContent=pickerPath;
  $('pickerlist').innerHTML = d.data.entries.filter(e=>e.is_dir).map(e=>{
    const p = (pickerPath==='/'?'':pickerPath)+'/'+e.name;
    return '<div class=row><span class=grow>📁 '+esc(e.name)+'</span>'+
      '<button class="small" onclick="pickerEnter(\''+esc(p)+'\')">打开</button>'+
      '<button class="small green" onclick="pickerPick(\''+esc(p)+'\')">选这个</button></div>';
  }).join('') || '<div class=mut>没有子文件夹</div>';
}
function pickerEnter(p){ pickerPath=p; loadPicker(); }
function pickerUp(){ if(pickerPath!=='/'){ pickerPath=pickerPath.split('/').slice(0,-1).join('/')||'/'; loadPicker(); } }
function pickerPick(p){ $('target189').value=p; $('picker').style.display='none'; }
function pickerUse(){ pickerPick(pickerPath); }
async function doRestore(to){
  if(!curCasB64){ alert('先解析一份 cas'); return; }
  if($('savecas').checked){
    const sd = await api('/api/lib/add',{cas_b64:curCasB64});
    if(sd.ok) loadLib();
  }
  $('restoremsg').textContent='提交中…';
  const url = to==='mobile'? '/api/restore139' : (to==='openlist'? '/api/restoreol' : '/api/restore189');
  const body = to==='mobile'? {cas_b64:curCasB64,parent:$('parent139').value||'/'}
                            : {cas_b64:curCasB64,cas_name:curCasName,target:$('target189').value};
  const d = await api(url,body);
  $('restoremsg').innerHTML = d.ok? '<span class=good>'+esc(d.msg)+'</span>' : '<span class=err>'+esc(d.msg)+'</span>';
}
async function loadList(){
  const d = await api('/api/list?path='+encodeURIComponent(curPath));
  if(!d.ok){ $('entries').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  $('curpath').textContent=curPath;
  $('entries').innerHTML = d.data.entries.map(e=>{
    const p = (curPath==='/'?'':curPath)+'/'+e.name;
    if(e.is_dir) return '<div class=row><span class=grow>📁 '+esc(e.name)+'</span><button class="small" onclick="enterDir(\''+esc(p)+'\')">打开</button></div>';
    return '<div class=row><span class=grow>📄 '+esc(e.name)+'<br><span class=mut>'+e.size+' 字节</span></span><button class="small green" onclick="genCas(\''+esc(p)+'\','+e.size+')">生成 cas</button></div>';
  }).join('') || '<div class=mut>空目录</div>';
}
function enterDir(p){ curPath=p; loadList(); }
function goUp(){ if(curPath!=='/'){ curPath=curPath.split('/').slice(0,-1).join('/')||'/'; loadList(); } }
async function genCas(p,size){
  const d = await api('/api/gen',{path:p,size:size,upload_back:$('upback').checked});
  alert(d.ok? '已开始生成,进度看「任务」区' : d.msg);
  loadJobs();
}
async function loadLib(){
  const d = await api('/api/lib');
  if(!d.ok){ $('lib').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  $('lib').innerHTML = d.data.map(it=>
    '<div class=row><span class=grow>📄 '+esc(it.name||it.file)+'<br><span class=mut>'+esc(it.kind||'')+' · '+(it.size||'')+' 字节</span></span>'+
    '<button class="small" onclick="dlLib(\''+esc(it.file)+'\')">下载</button>'+
    '<button class="small green" onclick="libRestore(\''+esc(it.file)+'\',\'mobile\')">移动还原</button>'+
    '<button class="small green" onclick="libRestore(\''+esc(it.file)+'\',\'tianyi\')">天翼还原</button>'+
    '<button class="small green" onclick="libRestore(\''+esc(it.file)+'\',\'openlist\')">OL还原</button>'+
    '<button class="small red" onclick="delLib(\''+esc(it.file)+'\')">删</button></div>'
  ).join('') || '<div class=mut>库里还没有 cas</div>';
}
function dlLib(f){ location.href='/api/lib/download?name='+encodeURIComponent(f); }
async function delLib(f){ if(!confirm('删除 '+f+' ?'))return; const d=await api('/api/lib/delete',{file:f}); loadLib(); }
async function libRestore(f,to){
  let body={file:f,to:to};
  if(to==='tianyi'||to==='openlist'){ let t=($('target189').value||'').trim(); if(!t){ t=prompt('OpenList 目标目录(天翼或移动挂载路径)','/天翼云盘/'); if(!t)return; } body.target=t; }
  else { const p=prompt('移动父目录 fileId(根目录 /)','/'); if(p===null)return; body.parent=p; }
  const d = await api('/api/lib/restore',body);
  alert(d.ok? d.msg : '失败: '+d.msg);
}
async function loadJobs(){
  const d = await api('/api/jobs');
  if(!d.ok||!d.data.length) return;
  $('jobs').innerHTML = d.data.map(j=>
    '<div style="margin:8px 0"><div>'+esc(j.title)+'</div>'+
    '<div class=mut>'+esc(j.status)+(j.progress!=null? ' · '+j.progress+'%':'')+' · '+esc(j.message||'')+'</div>'+
    (j.progress!=null&&j.status==='running'? '<div class=bar><i style="width:'+j.progress+'%"></i></div>':'')+
    (j.status==='done'&&j.result? '<div class=good>已存入 cas 库: '+esc(j.result.cas_file||'')+(j.result.uploaded_to? ' · 已上传回 '+esc(j.result.uploaded_to):'')+'</div>':'')+
    '</div>').join('');
  if(d.data.some(j=>j.status==='done')) loadLib();
}
status(); loadList(); loadLib(); loadJobs();
setInterval(loadJobs, 2500);
</script></body></html>"""


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("cas-web v%s 监听 :%d  数据目录 %s  口令保护:%s"
          % (VERSION, PORT, DATA_DIR, "开" if WEB_PASSWORD else "关"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
