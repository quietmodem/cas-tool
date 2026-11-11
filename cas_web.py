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
  - v1.5.0 起不再保存本地 cas 库:生成的 .cas 在任务里直接下载,或上传回
    网盘原文件旁边;还原确认成功后 .cas 也旁存到视频同目录。服务端数据目录
    (CAS_DATA_DIR,容器里是 /data)只存设置(天翼/移动主目录)。
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
import io
import json
import os
import zipfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cas_tool as ct

VERSION = "1.10.0"
PORT = int(os.environ.get("PORT", "8099"))
DATA_DIR = os.environ.get("CAS_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
MIB = 1024 * 1024

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# OpenList 接口
# ---------------------------------------------------------------------------

def ol_cfg():
    return effective_openlist()


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


def ol_list(path, refresh=False):
    data = ol_post("/fs/list", {"path": path, "page": 1, "per_page": 0, "refresh": bool(refresh)})
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
        result = {"name": name, "size": size,
                  "cas_b64": base64.b64encode(cas_bytes).decode()}
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
    phone, auth = m139_creds()
    if not phone or not auth:
        return False, "未配置移动凭据(设置页填手机号 + AUTH,或 compose 里配 CAS139_PHONE / CAS139_AUTH)"
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
    本工具不预判哈希够不够,把 cas 原样交给 OpenList,以它的预检结果为准。
    目标目录不填 = OpenList 根目录 / 。"""
    target = (target or "").strip() or "/"
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


def unzip_cas(zip_bytes):
    """从 zip 压缩包里取出全部 .cas 条目,返回 [{name, cas_b64}]。
    只取小文件(单个 ≤1MB、总解压 ≤16MB、最多 200 个),防压缩包炸弹。"""
    items, total = [], 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            base = os.path.basename(info.filename.replace("\\", "/"))
            if not base.lower().endswith(".cas"):
                continue
            if info.file_size > 1024 * 1024:
                continue
            total += info.file_size
            if total > 16 * 1024 * 1024 or len(items) >= 200:
                break
            items.append({"name": base,
                          "cas_b64": base64.b64encode(z.read(info)).decode("ascii")})
    return items


def settings_path():
    return os.path.join(DATA_DIR, "settings.json")


SETTINGS_KEYS = ("tianyi_home", "mobile_home", "openlist_url", "openlist_token",
                 "cas139_phone", "cas139_auth", "web_pw_hash")
PATH_KEYS = ("tianyi_paths", "mobile_paths")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        d = {}
    out = {k: str(d.get(k) or "") for k in SETTINGS_KEYS}
    for k in PATH_KEYS:
        v = d.get(k) or []
        out[k] = [str(x).strip().rstrip("/") or "/" for x in v if str(x).strip()]
    return out


def store_settings(data):
    with open(settings_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def hash_password(pw):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120000)
    return "pbkdf2$%s$%s" % (salt.hex(), dk.hex())


def check_stored_password(pw, stored):
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(),
                                  bytes.fromhex(salt_hex), 120000)
        return dk.hex() == dk_hex
    except Exception:  # noqa: BLE001
        return False


def password_required():
    return bool(WEB_PASSWORD or load_settings()["web_pw_hash"])


def verify_password(pw):
    if WEB_PASSWORD and pw == WEB_PASSWORD:
        return True
    h = load_settings()["web_pw_hash"]
    return bool(h) and check_stored_password(pw, h)


def probe_openlist(base, token):
    """保存前实测一组 OpenList 地址+Token,不通就报错,避免把坏 token 存进去。"""
    st, txt = ct.openlist_post(base.rstrip("/"), token, "/fs/list",
                               {"path": "/", "page": 1, "per_page": 0})
    try:
        resp = json.loads(txt)
    except ValueError:
        raise ValueError("OpenList 地址 %s 返回异常(HTTP %s),本次配置未保存" % (base, st))
    if resp.get("code") != 200:
        raise ValueError("OpenList 校验失败:%s —— 这组地址/Token 未保存,请核对后重新粘贴"
                         % (resp.get("message") or txt[:120]))


def update_settings(body):
    """设置页提交:密钥类字段留空 = 不改;clear_* 为真 = 清除该项。"""
    cur = load_settings()
    old_url = cur["openlist_url"]
    for k in ("tianyi_home", "mobile_home"):
        if k in body:
            v = str(body.get(k) or "").strip()
            if v and not v.startswith("/"):
                raise ValueError("主目录要以 / 开头(OpenList 里的完整路径),或留空")
            cur[k] = v.rstrip("/") or ("/" if v else "")
    for k in PATH_KEYS:
        if k in body:
            raw = body.get(k) or []
            if isinstance(raw, str):
                raw = raw.splitlines()
            vals = []
            for x in raw:
                v = str(x).strip()
                if not v:
                    continue
                if not v.startswith("/"):
                    raise ValueError("云盘路径要以 / 开头:%s" % v)
                v = v.rstrip("/") or "/"
                if v not in vals:
                    vals.append(v)
            cur[k] = vals
    if "openlist_url" in body:
        cur["openlist_url"] = str(body.get("openlist_url") or "").strip().rstrip("/")
    if "cas139_phone" in body:
        cur["cas139_phone"] = str(body.get("cas139_phone") or "").strip()
    tok_in = str(body.get("openlist_token") or "").strip()
    if tok_in.lower().startswith("bearer "):
        tok_in = tok_in[7:].strip()
    if tok_in:
        cur["openlist_token"] = tok_in
    auth_in = str(body.get("cas139_auth") or "").strip()
    if auth_in:
        cur["cas139_auth"] = auth_in
    if body.get("clear_openlist_token"):
        cur["openlist_token"] = ""
    if body.get("clear_cas139_auth"):
        cur["cas139_auth"] = ""
    pw = str(body.get("web_password") or "")
    if pw:
        cur["web_pw_hash"] = hash_password(pw)
    if body.get("clear_web_password"):
        cur["web_pw_hash"] = ""
    if (tok_in or ("openlist_url" in body and cur["openlist_url"] != old_url)) \
            and not body.get("clear_openlist_token"):
        base = cur["openlist_url"] or os.environ.get("OPENLIST_URL", "").strip()
        token = cur["openlist_token"] or os.environ.get("OPENLIST_TOKEN", "").strip()
        if base and token:
            probe_openlist(base, token)
    store_settings(cur)
    return public_settings(cur)


def public_settings(s=None):
    """给页面看的设置:密钥只报已设置/未设置,绝不回显明文。"""
    s = s or load_settings()
    return {"tianyi_home": s["tianyi_home"], "mobile_home": s["mobile_home"],
            "tianyi_paths": s["tianyi_paths"], "mobile_paths": s["mobile_paths"],
            "openlist_url": s["openlist_url"],
            "openlist_token_set": bool(s["openlist_token"]
                                       or os.environ.get("OPENLIST_TOKEN")),
            "openlist_token_source": ("页面设置" if s["openlist_token"] else
                                      ("环境变量" if os.environ.get("OPENLIST_TOKEN") else "")),
            "cas139_phone": s["cas139_phone"],
            "cas139_auth_set": bool(s["cas139_auth"]
                                    or ct.get_139_credentials()[1]),
            "web_password_set": bool(s["web_pw_hash"] or WEB_PASSWORD)}


def effective_openlist():
    s = load_settings()
    base = s["openlist_url"] or os.environ.get("OPENLIST_URL", "").strip()
    token = s["openlist_token"] or os.environ.get("OPENLIST_TOKEN", "").strip()
    return base.rstrip("/"), token


def m139_creds():
    s = load_settings()
    if s["cas139_phone"] and s["cas139_auth"]:
        return s["cas139_phone"], s["cas139_auth"]
    return ct.get_139_credentials()


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
    if not password_required():
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
            if password_required() and not session_ok(self):
                return self.send_bytes(LOGIN_HTML, "text/html; charset=utf-8")
            return self.send_bytes(INDEX_HTML, "text/html; charset=utf-8")
        if not session_ok(self):
            return self.fail("未登录", status=401)
        try:
            if path == "/api/status":
                return self.api_status()
            if path == "/api/settings":
                return self.ok(public_settings())
            if path == "/api/list":
                return self.api_list(qs)
            if path == "/api/jobs":
                with JOBS_LOCK:
                    jobs = sorted(JOBS.values(), key=lambda j: -j["ts"])
                return self.ok(jobs[:30])
            return self.fail("无此路径", status=404)
        except Exception as e:  # noqa: BLE001
            return self.fail(e)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            body = self.read_json()
            if verify_password(body.get("password") or ""):
                tok = make_session()
                return self.send_bytes(
                    json.dumps({"ok": True, "msg": "ok", "data": None}),
                    extra={"Set-Cookie": "cas_sess=%s; Path=/; HttpOnly; SameSite=Lax" % tok})
            if not password_required():
                return self.ok()
            return self.fail("口令不对")
        if not session_ok(self):
            return self.fail("未登录", status=401)
        try:
            body = self.read_json()
            if path == "/api/inspect":
                cas_bytes = base64.b64decode(body.get("cas_b64", ""))
                return self.ok(cas_summary(cas_bytes))
            if path == "/api/scan":
                return self.api_scan(body)
            if path == "/api/settings":
                try:
                    return self.ok(update_settings(body))
                except ValueError as e:
                    return self.fail(str(e))
            if path == "/api/unzipcas":
                try:
                    items = unzip_cas(base64.b64decode(body.get("zip_b64", "")))
                except zipfile.BadZipFile:
                    return self.fail("这不是有效的 zip 压缩包")
                if not items:
                    return self.fail("压缩包里没找到 .cas 文件")
                return self.ok(items)
            if path == "/api/exists":
                # 天翼目录列表有延迟/缓存:轮询最多约 30 秒,第二次起强制刷新,名字大小写不敏感
                epath = (body.get("path") or "/").rstrip("/") or "/"
                ename = (body.get("name") or "").strip().lower()
                found = False
                for attempt in range(8):
                    entries = ol_list(epath, refresh=attempt > 0)
                    if any((e.get("name") or "").strip().lower() == ename for e in entries):
                        found = True
                        break
                    if attempt < 7:
                        time.sleep(4)
                return self.ok({"found": found})
            if path == "/api/sidecar":
                return self.api_sidecar(body)
            if path == "/api/gen":
                return self.api_gen(body)
            if path == "/api/restore139":
                cas_bytes = base64.b64decode(body.get("cas_b64", ""))
                parent = str(body.get("parent") or "").strip()
                tdir = str(body.get("target") or "").strip()
                where = ""
                if not parent:
                    if tdir and tdir != "/":
                        try:
                            info = ol_get(tdir)
                        except Exception as e:
                            return self.fail("解析目标目录失败: %s" % e)
                        parent = str((info or {}).get("id")
                                     or (info or {}).get("fileId") or "").strip()
                        if not parent:
                            return self.fail("目标目录 %s 没拿到移动 fileId"
                                             "(确认它是移动云盘挂载下的目录)" % tdir)
                        where = "(目标目录 %s)" % tdir
                    else:
                        parent = "/"
                        where = "(移动云盘根目录)"
                ok, msg = restore_139(cas_bytes, parent)
                if ok and where:
                    msg += where
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
            return self.fail("无此路径", status=404)
        except Exception as e:  # noqa: BLE001
            return self.fail(e)

    # -- API 实现 -------------------------------------------------------------
    def api_status(self):
        base, token = ol_cfg()
        status = {"version": VERSION, "openlist_configured": bool(base and token),
                  "openlist_ok": False, "openlist_msg": "",
                  "m139_configured": bool(m139_creds()[0] and m139_creds()[1])}
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

    def api_scan(self, body):
        """扫描指定 OpenList 目录下的 .cas 文件:逐个下载解析,返回可直接还原的清单。"""
        root = (body.get("path") or "").rstrip("/") or "/"
        recursive = bool(body.get("recursive", True))
        found = []

        def walk(dirpath, depth):
            if len(found) >= 300:
                return
            try:
                entries = ol_list(dirpath)
            except Exception:  # noqa: BLE001
                return
            for e in entries:
                if len(found) >= 300:
                    break
                p = (dirpath.rstrip("/") + "/" + e["name"]) if dirpath != "/" else "/" + e["name"]
                if e.get("is_dir"):
                    if recursive and depth < 6:
                        walk(p, depth + 1)
                elif e["name"].lower().endswith(".cas") and 0 < (e.get("size") or 0) <= 1024 * 1024:
                    try:
                        with ol_download_iter(p) as resp:
                            data = resp.read()
                        found.append({"path": p, "cas_name": e["name"],
                                      "cas_b64": base64.b64encode(data).decode(),
                                      **cas_summary(data)})
                    except Exception:  # noqa: BLE001
                        continue

        walk(root, 0)
        return self.ok(found)

    def api_sidecar(self, body):
        """把 .cas 作为侧车文件存到目标目录里视频旁边:<dir>/<视频名>.cas。"""
        cas_bytes = base64.b64decode(body.get("cas_b64", ""))
        summary = cas_summary(cas_bytes)
        video = body.get("video_name") or summary.get("name") or ""
        if not video:
            return self.fail("cas 里没有文件名,无法决定侧车名")
        dest = (body.get("dir") or "/").rstrip("/") + "/" + video + ".cas"
        ol_put(dest, cas_bytes)
        return self.ok({"path": dest})



# ---------------------------------------------------------------------------
# 页面(单文件,移动优先)
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>CAS 工具 · 登录</title><style>
body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f2f2f7;color:#111}
.wrap{max-width:420px;margin:15vh auto;padding:20px}
input,button{width:100%;box-sizing:border-box;font-size:17px;padding:13px;border-radius:10px;border:1px solid #c7c7cc;margin-top:10px}
button{background:#0a6cff;color:#fff;border:none;font-weight:600}
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
body{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;margin:0;background:#eceef3;color:#111;-webkit-tap-highlight-color:transparent}
@media (prefers-color-scheme:dark){body{background:#0b0b0f;color:#eee}.card{background:#1b1b20!important;box-shadow:none!important}input,textarea,select{background:#26262c!important;color:#eee;border-color:#3a3a42!important}.tabbar{background:#26262c!important}.tabbtn{color:#aaa!important}.tabbtn.on{background:#3a3a42!important;color:#fff!important;box-shadow:none!important}button.grey{background:#33333a!important;color:#eee!important}.crumb{background:#22314d!important}.row{border-color:#2c2c33!important}.dirrow:active{background:#22314d!important}}
.wrap{max-width:720px;margin:0 auto;padding:14px 14px 48px}
h1{font-size:21px;margin:10px 2px 0;font-weight:700;letter-spacing:.2px}
h2{font-size:15px;margin:0 0 10px;font-weight:600}
.card{background:#fff;border-radius:18px;padding:16px;margin:12px 0;box-shadow:0 2px 12px rgba(25,35,70,.07)}
button{font-size:15px;padding:11px 16px;border-radius:12px;border:none;background:#0a6cff;color:#fff;font-weight:600;min-height:46px;cursor:pointer}
button:active{opacity:.7}
button.grey{background:#e8eaf0;color:#222}button.green{background:#2fae4e}button.red{background:#e5484d}
button.small{padding:7px 12px;min-height:38px;font-size:13px;border-radius:10px}
input,textarea{width:100%;font-size:16px;padding:11px 12px;border-radius:12px;border:1.5px solid #d9dde6;margin:6px 0;background:#fcfdff;outline:none}
input:focus,textarea:focus{border-color:#0a6cff}
textarea{min-height:64px}
.row{display:flex;align-items:center;gap:9px;padding:9px 2px;border-bottom:1px solid #eef0f4}
.row .grow{flex:1;min-width:0;word-break:break-all}
.dirrow{cursor:pointer;border-radius:10px}
.dirrow:active{background:#eef4ff}
.bar{height:8px;background:#e5e5ea;border-radius:4px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:#0a6cff}
.mut{color:#8e8e93;font-size:13px}
.msg{white-space:pre-wrap;word-break:break-all;font-size:14px;margin-top:8px}
.err{color:#e5484d}.good{color:#248a3d}
.kv{font-size:13px;color:#666;word-break:break-all}
.tabbar{position:sticky;top:10px;z-index:6;display:flex;gap:4px;background:#dde1e9;padding:4px;border-radius:14px;margin:14px 0 2px}
.tabbtn{flex:1;background:transparent;color:#5a5f6a;border-radius:11px;min-height:42px;box-shadow:none}
.tabbtn.on{background:#fff;color:#111;box-shadow:0 1px 5px rgba(0,0,0,.14)}
.crumbs{padding:8px 2px 2px;font-size:14px;line-height:2.1}
.crumb{color:#0a6cff;background:#e9f1fe;padding:4px 10px;border-radius:8px;cursor:pointer;white-space:nowrap}
#restorebox>button{display:block;width:100%;margin-top:9px}
.overlay{position:fixed;inset:0;background:rgba(10,12,20,.45);z-index:50;align-items:flex-start;justify-content:center;padding:5vh 12px 12px}
.sheet{background:#fff;border-radius:18px;padding:16px;width:100%;max-width:560px;max-height:88vh;overflow:auto;box-shadow:0 12px 40px rgba(0,0,0,.25)}
@media (prefers-color-scheme:dark){.sheet{background:#1b1b20}}
#scanbtns>button{display:block;width:100%;margin-top:9px}
</style></head><body><div class="wrap">
<h1>🗂 CAS 双云工具 <span class="mut" id="ver"></span></h1>
<div class="card"><div id="status" class="msg">检查连接中…</div></div>

<div class="tabbar">
<button id="tabbtn-restore" class="tabbtn on" onclick="switchTab('restore')">cas 还原</button>
<button id="tabbtn-gen" class="tabbtn" onclick="switchTab('gen')">生成 cas</button>
<button id="tabbtn-settings" class="tabbtn" onclick="switchTab('settings')">设置</button>
</div>

<div id="picker" class="overlay" style="display:none" onclick="if(event.target===this)togglePicker()"><div class="sheet">
<div class="row"><b class="grow">📁 选择目录</b>
<button class="small green" onclick="pickerUse()">✔ 用当前目录</button>
<button class="small grey" onclick="togglePicker()">关闭</button></div>
<div id="crumbs" class="crumbs"></div>
<div class="row"><input id="pickerfilter" class="grow" placeholder="筛选本层文件夹…" oninput="renderPicker()">
<button class="small grey" onclick="pickerHome('189')">天翼主目录</button>
<button class="small grey" onclick="pickerHome('139')">移动主目录</button></div>
<div id="pickerlist"></div>
</div></div>

<div id="tab-restore">
<div class="card"><h2>还原 .cas</h2>
<input type="file" id="casfile" accept=".cas,.txt,.zip" multiple>
<div class="mut">可多选 .cas,也可选含多个 .cas 的 .zip 压缩包</div>
<textarea id="castext" placeholder="或把 .cas 的内容(Base64 文本)粘贴到这里"></textarea>
<button onclick="inspectCas()">解析</button>
<div id="inspect" class="msg"></div>
<div id="restorebox" style="display:none">
<div class="row"><input id="target189" class="grow" placeholder="目标目录(不填:天翼按钮用天翼主目录,移动按钮用移动主目录)">
<button class="small grey" onclick="togglePicker('target189')">📁 选目录</button></div>
<div class="row"><button class="small grey" onclick="fillHome('189')">填入天翼主目录</button>
<button class="small grey" onclick="fillHome('139')">填入移动主目录</button></div>
<input id="parent139" value="" placeholder="移动直调父目录 fileId(高级,一般留空;留空按上方目标目录还原)">
<label class="mut"><input type="checkbox" id="sidecar" style="width:auto" checked> 还原成功后,把 .cas 也存到目标目录(视频旁边)</label>
<button class="green" onclick="doRestore('tianyi')">还原到天翼云盘</button>
<button class="green" onclick="doRestore('mobile')">还原到移动云盘</button>
<div class="mut">两个按钮的区别只是去哪个云:天翼经 OpenList 秒传,移动走 139 直调(OpenList 不支持移动秒传)。</div>
</div><div id="restoremsg" class="msg"></div>
<div style="border-top:1px solid #eef0f4;margin-top:14px;padding-top:12px">
<h2>🔍 扫描网盘目录里的 .cas</h2>
<div class="row"><input id="scanpath" class="grow" placeholder="要扫描的目录,如 /TV/天翼云盘在线/中转">
<button class="small grey" onclick="togglePicker('scanpath')">📁 选目录</button>
<button class="small" onclick="scanCas()">开始扫描</button></div>
<label class="mut"><input type="checkbox" id="scanrec" style="width:auto" checked> 包含子文件夹(递归最多 6 层,最多取 300 份)</label>
<div id="scanmsg" class="msg"></div>
<div id="scanlist"></div>
<div id="scanbtns" style="display:none">
<div class="row"><button class="small grey" onclick="scanCheckAll(true)">全选</button>
<button class="small grey" onclick="scanCheckAll(false)">全不选</button>
<span class="mut">目标目录/旁存 .cas 用上方还原区那一栏(留空按主目录)</span></div>
<button class="green" onclick="scanRestore('tianyi')">还原到天翼云盘</button>
<button class="green" onclick="scanRestore('mobile')">还原到移动云盘</button>
<div id="scanrestoremsg" class="msg"></div>
</div>
</div></div>

</div>

<div id="tab-gen" style="display:none">
<div class="card"><h2>浏览网盘 · 生成 .cas</h2>
<div class="row"><button class="small grey" onclick="goUp()">⬆ 上级</button>
<span class="grow" id="curpath">/</span>
<button class="small grey" onclick="loadList()">刷新</button></div>
<div class="row"><button class="small grey" onclick="goHome('189')">跳到天翼主目录</button>
<button class="small grey" onclick="goHome('139')">跳到移动主目录</button></div>
<label class="mut"><input type="checkbox" id="upback" style="width:auto" checked> 生成后把 .cas 上传回同目录(视频旁边,推荐)</label>
<div id="entries"></div></div>

<div class="card"><h2>任务</h2><div id="jobs" class="mut">暂无</div></div>
</div>

<div id="tab-settings" style="display:none">
<div class="card"><h2>网盘配置</h2>
<div class="mut">在这里配好就行,不用改 docker-compose;配置存在群晖的工具数据目录里。密钥类留空 = 不修改,页面只显示已设置/未设置,不回显明文。</div>
<label class="mut">OpenList 地址</label>
<input id="cfgurl" placeholder="如 http://192.168.111.2:5244">
<label class="mut">OpenList Token <span id="tokstate" class="kv"></span></label>
<input id="cfgtoken" type="password" placeholder="粘贴 OpenList 后台的令牌">
<div class="mut">填 OpenList 后台「设置 → 其他 → 令牌」里的长期令牌;浏览器 F12 里抓的登录 token 是临时的,会过期失效。保存时工具会先实测这组地址+Token,不通不会存。框里显示星号 = 已保存:不动它、或清空,都算不修改;要更换直接输入新的,要删除勾下面的清除项。</div>
<label class="mut"><input type="checkbox" id="cfgtokclear" style="width:auto"> 清除页面里存的 Token(改回用 compose 环境变量里的)</label>
<label class="mut">移动云盘手机号(移动直调还原用,可不填)</label>
<input id="cfgphone" placeholder="手机号">
<label class="mut">移动 AUTH <span id="authstate" class="kv"></span></label>
<input id="cfgauth" type="password" placeholder="形如 Basic xxxxx,留空 = 不修改">
<label class="mut">页面访问口令 <span id="pwstate" class="kv"></span></label>
<input id="cfgpw" type="password" placeholder="留空 = 不修改">
<label class="mut"><input type="checkbox" id="cfgauthclear" style="width:auto"> 清除页面里存的移动 AUTH</label>
<label class="mut"><input type="checkbox" id="cfgpwclear" style="width:auto"> 清除访问口令(打开页面不再要口令)</label>
</div>
<div class="card"><h2>主目录设置</h2>
<div class="mut">设置后:还原时目标目录留空会按按钮自动用对应主目录;生成页也能一键跳过去,不用每次翻 OpenList 的目录树。</div>
<label class="mut">天翼云盘主目录</label>
<div class="row"><input id="home189" class="grow" placeholder="如 /TV/天翼云盘在线">
<button class="small grey" onclick="togglePicker('home189')">📁 选目录</button></div>
<label class="mut">移动云盘主目录</label>
<div class="row"><input id="home139" class="grow" placeholder="如 /TV/移动云盘">
<button class="small grey" onclick="togglePicker('home139')">📁 选目录</button></div>
<label class="mut">天翼云盘路径(可多个,一行一个;还原校验用)</label>
<textarea id="paths189" placeholder="/TV/天翼云盘在线&#10;/天翼云盘"></textarea>
<button class="small grey" onclick="togglePicker('append:paths189')">📁 添加一个目录</button>
<label class="mut">移动云盘路径(可多个,一行一个)</label>
<textarea id="paths139" placeholder="/TV/移动云盘&#10;/移动云盘"></textarea>
<button class="small grey" onclick="togglePicker('append:paths139')">📁 添加一个目录</button>
<div class="mut">校验规则:天翼 cas 还到不属于天翼的目录、移动 cas 还到不属于移动的目录,还原前会弹窗提醒(主目录自动算作本云路径,不用重复填)。清单留空 = 不校验。</div>
<button onclick="saveSettings()">保存设置</button>
<div id="setmsg" class="msg"></div>
<div class="mut">所有设置保存在群晖的工具数据目录(settings.json),所有设备共用同一份。docker-compose 里的同名环境变量若也填了,页面设置优先,环境变量只作兜底——访问口令忘了时,在 compose 里临时加 WEB_PASSWORD 就能救急登录。</div>
</div>
</div>
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
    +'<br>'+(s.m139_configured? '🟢 移动直调接口已配置凭据' : '🟡 未配置移动凭据(设置页可填;移动直调还原不可用,天翼不受影响)');
}
function fileToB64(file){return new Promise((res,rej)=>{const r=new FileReader();
  r.onload=()=>res(r.result.split(',')[1]); r.onerror=rej; r.readAsDataURL(file);});}
let batchItems=[];
async function collectItems(){
  const items=[];
  for(const f of $('casfile').files){
    if(/\.zip$/i.test(f.name)){
      const d = await api('/api/unzipcas',{zip_b64:await fileToB64(f)});
      if(!d.ok) throw new Error(f.name+': '+d.msg);
      for(const it of d.data) items.push({name:it.name, b64:it.cas_b64});
    } else {
      items.push({name:f.name, b64:await fileToB64(f)});
    }
  }
  const t=$('castext').value.trim();
  if(t) items.push({name:'pasted.cas', b64:btoa(unescape(encodeURIComponent(t)))});
  return items;
}
async function inspectCas(){
  $('inspect').textContent='解析中…';
  try{ batchItems = await collectItems(); }
  catch(e){ $('inspect').innerHTML='<span class=err>'+esc(e.message)+'</span>'; return; }
  if(!batchItems.length){ $('inspect').innerHTML='<span class=err>先选择 .cas/.zip 文件或粘贴内容</span>'; $('restorebox').style.display='none'; return; }
  for(const it of batchItems){
    const d = await api('/api/inspect',{cas_b64:it.b64});
    it.info = d.ok? d.data : null; it.err = d.ok? '' : d.msg;
  }
  renderBatch();
  $('restorebox').style.display='block';
}
function batchAll(v){ document.querySelectorAll('.itchk').forEach(c=>{ c.checked = v; }); }
function syncBatchAll(){ const cs = Array.from(document.querySelectorAll('.itchk')); const b = $('batchall'); if(b && cs.length) b.checked = cs.every(c=>c.checked); }
function renderBatch(){
  const head = batchItems.length? '<div><label><input type="checkbox" id="batchall" style="width:auto" checked onchange="batchAll(this.checked)"> <b>全选</b></label> <span class=mut>共 '+batchItems.length+' 份 cas(取消勾选的不会还原)</span></div>' : '';
  $('inspect').innerHTML = head + batchItems.map((it,i)=>
    it.info? '<div><label><input type="checkbox" class="itchk" data-i="'+i+'" style="width:auto" checked onchange="syncBatchAll()"> 📄 '+esc(it.info.name)+'</label> <span class=mut>'+esc(it.info.kind)+' · '+esc(it.info.size)+' 字节</span> <span id="st'+i+'"></span>'+
      (it.src? '<br><span class=kv>来源: '+esc(it.src)+'</span>':'')+
      '<br><span class=kv>md5: '+esc(it.info.md5||'(空)')+' · sliceMd5: '+esc(it.info.sliceMd5||'(空)')+' · sha256: '+esc(it.info.sha256||'(空)')+'</span></div>'
           : '<div>📄 '+esc(it.name)+' <span class=err>解析失败: '+esc(it.err)+'</span></div>'
  ).join('');
}
let scanItems=[];
async function scanCas(){
  const p=$('scanpath').value.trim();
  if(!p){ alert('先填要扫描的目录(或点「选目录」)'); return; }
  $('scanmsg').textContent='扫描中…目录大的话稍等一下';
  $('scanbtns').style.display='none';
  const d=await api('/api/scan',{path:p,recursive:$('scanrec').checked});
  if(!d.ok){ $('scanmsg').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  if(!d.data.length){ $('scanmsg').textContent='这个目录下没找到 .cas 文件'; scanItems=[]; $('scanlist').innerHTML=''; return; }
  scanItems = d.data.map(x=>({name:x.cas_name, b64:x.cas_b64, src:x.path,
    info:{name:x.name, size:x.size, kind:x.kind, md5:x.md5, sliceMd5:x.sliceMd5, sha256:x.sha256}}));
  renderScan();
  $('restorebox').style.display='block';
  $('scanbtns').style.display='block';
  $('scanmsg').innerHTML='<span class=good>找到 '+d.data.length+' 份 cas</span> —— 在下面列表里勾选要还原的,再点「还原选中的」按钮';
}
function renderScan(){
  $('scanlist').innerHTML = scanItems.map((it,i)=>
    '<div><label><input type="checkbox" class="scanchk" data-i="'+i+'" style="width:auto"> 📄 '+esc(it.info.name)+'</label> <span class=mut>'+esc(it.info.kind)+' · '+esc(it.info.size)+' 字节</span> <span id="sst'+i+'"></span>'+
    '<br><span class=kv>来源: '+esc(it.src)+'</span></div>').join('');
}
function scanCheckAll(v){ document.querySelectorAll('.scanchk').forEach(c=>c.checked=v); }
function scanSetSt(i,txt,cls){ const el=$('sst'+i); if(el){ el.textContent=txt; el.className=cls; } }
async function scanRestore(to){
  const items=[], idx=[];
  scanItems.forEach((it,i)=>{ const c=document.querySelector('.scanchk[data-i="'+i+'"]'); if(c && c.checked){ items.push(it); idx.push(i); } });
  if(!items.length){ alert('先在扫描列表里勾选要还原的 cas'); return; }
  const r = await restoreCore(to, items, (j,txt,cls)=>scanSetSt(idx[j],txt,cls));
  if(!r){ $('scanrestoremsg').innerHTML='已取消还原'; return; }
  $('scanrestoremsg').innerHTML='还原完成: 确认成功 '+r.okN+' · 未确认/失败 '+r.badN+'(目标目录: '+esc(r.target)+')';
}
function setSt(i,txt,cls){ const el=$('st'+i); if(el) el.innerHTML='<span class='+cls+'>'+esc(txt)+'</span>'; }
let pickerPath='/', pickerTarget='target189';
function switchTab(name){
  for(const n of ['restore','gen','settings']){
    $('tab-'+n).style.display = n===name? 'block':'none';
    $('tabbtn-'+n).classList.toggle('on', n===name);
  }
}
let SETTINGS={tianyi_home:'',mobile_home:''};
const MASK='••••••••';
function secVal(id){ const v=$(id).value; return v===MASK? '' : v.trim(); }
function secValRaw(id){ const v=$(id).value; return v===MASK? '' : v; }
['cfgtoken','cfgauth','cfgpw'].forEach(function(id){ const el=$(id); if(el) el.addEventListener('focus', function(){ if(el.value===MASK) el.select(); }); });
async function loadSettings(){
  const d = await api('/api/settings');
  if(!d.ok) return;
  SETTINGS=d.data;
  $('home189').value=SETTINGS.tianyi_home||''; $('home139').value=SETTINGS.mobile_home||'';
  $('cfgurl').value=SETTINGS.openlist_url||''; $('cfgphone').value=SETTINGS.cas139_phone||'';
  $('paths189').value=(SETTINGS.tianyi_paths||[]).join('\n');
  $('paths139').value=(SETTINGS.mobile_paths||[]).join('\n');
  $('cfgtoken').value = SETTINGS.openlist_token_set? MASK : '';
  $('cfgauth').value = SETTINGS.cas139_auth_set? MASK : '';
  $('cfgpw').value = SETTINGS.web_password_set? MASK : '';
  $('tokstate').textContent = SETTINGS.openlist_token_set? '(已设置'+(SETTINGS.openlist_token_source? ' · 来自'+SETTINGS.openlist_token_source : '')+')' : '(未设置)';
  $('authstate').textContent = SETTINGS.cas139_auth_set? '(已设置)' : '(未设置)';
  $('pwstate').textContent = SETTINGS.web_password_set? '(已设置)' : '(未设置)';
}
async function saveSettings(){
  const body={tianyi_home:$('home189').value.trim(), mobile_home:$('home139').value.trim(),
    tianyi_paths:$('paths189').value.split('\n').map(s=>s.trim()).filter(Boolean),
    mobile_paths:$('paths139').value.split('\n').map(s=>s.trim()).filter(Boolean),
    openlist_url:$('cfgurl').value.trim(), openlist_token:secVal('cfgtoken'),
    cas139_phone:$('cfgphone').value.trim(), cas139_auth:secVal('cfgauth'),
    web_password:secValRaw('cfgpw'), clear_web_password:$('cfgpwclear').checked,
    clear_openlist_token:$('cfgtokclear').checked, clear_cas139_auth:$('cfgauthclear').checked};
  const d = await api('/api/settings', body);
  $('setmsg').innerHTML = d.ok? '<span class=good>已保存</span>' : '<span class=err>'+esc(d.msg)+'</span>';
  if(d.ok){ $('cfgtoken').value=''; $('cfgauth').value=''; $('cfgpw').value=''; $('cfgpwclear').checked=false; $('cfgtokclear').checked=false; $('cfgauthclear').checked=false; loadSettings(); status(); }
}
function fillHome(which){
  const v = which==='189'? SETTINGS.tianyi_home : SETTINGS.mobile_home;
  if(v){ $('target189').value=v; } else { alert('还没设置主目录,先到「设置」页设置'); }
}
function goHome(which){
  const v = which==='189'? SETTINGS.tianyi_home : SETTINGS.mobile_home;
  if(v){ curPath=v; loadList(); } else { alert('还没设置主目录,先到「设置」页设置'); }
}
function togglePicker(targetId){
  if(targetId) pickerTarget=targetId;
  const p=$('picker'); const show=p.style.display==='none';
  if(show && targetId && targetId.indexOf('append:')!==0){ pickerPath = ($(pickerTarget).value||'').trim() || '/'; }
  p.style.display=show?'flex':'none'; if(show) loadPicker();
}
let pickerDirs=[];
async function loadPicker(){
  renderCrumbs();
  $('pickerlist').innerHTML='<div class="mut" style="padding:8px 2px">加载中…</div>';
  const d = await api('/api/list?path='+encodeURIComponent(pickerPath));
  if(!d.ok){ $('pickerlist').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  pickerDirs = d.data.entries.filter(e=>e.is_dir);
  renderPicker();
}
function renderCrumbs(){
  const parts = pickerPath==='/'? [] : pickerPath.split('/').filter(Boolean);
  let html='<span class="crumb" onclick="pickerGo(\'/\')">根目录</span>';
  let acc='';
  for(const p of parts){ acc+='/'+p; html+=' <span class="mut">›</span> <span class="crumb" onclick="pickerGo(\''+esc(acc)+'\')">'+esc(p)+'</span>'; }
  $('crumbs').innerHTML=html;
}
function renderPicker(){
  const q=(($('pickerfilter').value)||'').trim().toLowerCase();
  const items=pickerDirs.filter(e=>!q||e.name.toLowerCase().includes(q));
  $('pickerlist').innerHTML = items.length? items.map(e=>{
    const p=(pickerPath==='/'?'':pickerPath)+'/'+e.name;
    return '<div class="row dirrow" onclick="pickerEnter(\''+esc(p)+'\')"><span>📁</span><span class="grow">'+esc(e.name)+'</span>'+
      '<button class="small green" onclick="event.stopPropagation();pickerPick(\''+esc(p)+'\')">选这个</button></div>';
  }).join('') : '<div class="mut" style="padding:8px 2px">'+(q?'没有匹配的文件夹':'这一层没有文件夹,点上面「✔ 用当前目录」就是它')+'</div>';
}
function pickerGo(p){ pickerPath=p; $('pickerfilter').value=''; loadPicker(); }
function pickerEnter(p){ pickerGo(p); }
function pickerUp(){ if(pickerPath!=='/') pickerGo(pickerPath.split('/').slice(0,-1).join('/')||'/'); }
function pickerHome(which){ const v=which==='189'? SETTINGS.tianyi_home : SETTINGS.mobile_home; if(v){ pickerGo(v); } else alert('还没设置主目录,先到「设置」页设置'); }
function pickerPick(p){
  if(pickerTarget.indexOf('append:')===0){
    const ta=$(pickerTarget.slice(7));
    const lines=ta.value.split('\n').map(s=>s.trim()).filter(Boolean);
    if(lines.indexOf(p)<0) lines.push(p);
    ta.value=lines.join('\n');
  } else { $(pickerTarget).value=p; }
  $('picker').style.display='none';
}
function pickerUse(){ pickerPick(pickerPath); }
const sleepMs=ms=>new Promise(r=>setTimeout(r,ms));
function cloudPathSet(which){
  const list = (which==='189'? SETTINGS.tianyi_paths : SETTINGS.mobile_paths) || [];
  const home = which==='189'? SETTINGS.tianyi_home : SETTINGS.mobile_home;
  const set = list.slice();
  if(home && set.indexOf(home)<0) set.push(home);
  return set;
}
function inCloud(target, which){
  const set = cloudPathSet(which);
  if(!set.length) return null;
  const t = (target||'/').replace(/\/+$/,'') || '/';
  return set.some(p=>{ const q=(p||'/').replace(/\/+$/,'') || '/'; return q==='/' || t===q || t.indexOf(q+'/')===0; });
}
function itemCloud(it){
  const k=(it.info&&it.info.kind)||'';
  if(k.indexOf('both')===0) return 'both';
  if(k.indexOf('tianyi')===0) return '189';
  if(k.indexOf('mobile')===0) return '139';
  return '';
}
async function restoreCore(to, items, setStatus){
  const url = to==='mobile'? '/api/restore139' : (to==='openlist'? '/api/restoreol' : '/api/restore189');
  let target = $('target189').value.trim();
  if(!target) target = (to==='tianyi'? SETTINGS.tianyi_home : SETTINGS.mobile_home) || '/';
  {
    const warns=[];
    for(const it of items){
      if(!it.info || it._skip) continue;
      const c=itemCloud(it);
      if(to==='mobile'){
        if(inCloud(target,'139')===false) warns.push('《'+it.info.name+'》要还原到移动,但目标目录 '+target+' 不在设置的移动云盘路径下');
      } else if(to==='tianyi'){
        if(inCloud(target,'189')===false) warns.push('《'+it.info.name+'》要还原到天翼,但目标目录 '+target+' 不在设置的天翼云盘路径下');
      } else if(c==='189'){
        if(inCloud(target,'189')===false) warns.push('《'+it.info.name+'》是天翼 cas,目标目录 '+target+' 不在设置的天翼云盘路径下');
      } else if(c==='139'){
        if(inCloud(target,'139')===false) warns.push('《'+it.info.name+'》是移动 cas,目标目录 '+target+' 不在设置的移动云盘路径下');
      } else if(c==='both'){
        if(inCloud(target,'189')===false && inCloud(target,'139')===false) warns.push('《'+it.info.name+'》是双填 cas,目标目录 '+target+' 既不在天翼也不在移动云盘路径下');
      }
    }
    if(warns.length){
      const shown = warns.slice(0,5).join('\n') + (warns.length>5? '\n…等共 '+warns.length+' 条' : '');
      if(!confirm('⚠ 目录可能选错了:\n\n'+shown+'\n\n仍要继续还原吗？')) return null;
    }
  }
  let okN=0, badN=0;
  for(let i=0;i<items.length;i++){
    const it=items[i];
    if(!it.info){ badN++; continue; }
    if(it._skip){ continue; }
    setStatus(i,'还原中…','mut');
    const body = to==='mobile'? {cas_b64:it.b64,parent:$('parent139').value||'',target:target}
                              : {cas_b64:it.b64,cas_name:it.name,target:target};
    const d = await api(url,body);
    if(!d.ok){ setStatus(i,'✗ '+d.msg,'err'); badN++; continue; }
    if(to==='mobile'){
      let note = '✓ '+(d.msg||'已还原');
      if($('sidecar').checked && !String($('parent139').value||'').trim()){
        const sc = await api('/api/sidecar',{dir:target,video_name:it.info.name,cas_b64:it.b64});
        note += sc.ok? '(.cas 已存到同目录)' : '(.cas 存同目录失败: '+sc.msg+')';
      }
      setStatus(i,note,'good'); okN++; continue;
    }
    await sleepMs(1500);
    const ex = await api('/api/exists',{path:target,name:it.info.name});
    if(ex.ok && ex.data.found){
      let note='✓ 已确认文件在 '+target;
      if($('sidecar').checked){
        const sc = await api('/api/sidecar',{dir:target,video_name:it.info.name,cas_b64:it.b64});
        note += sc.ok? '(.cas 已存到同目录)' : '(.cas 存同目录失败: '+sc.msg+')';
      }
      setStatus(i,note,'good'); okN++;
    }
    else { setStatus(i,'⚠ 已提交,但在 '+target+' 还没看到这个文件(可能仍在处理,或保存失败,请看 OpenList 后台任务/日志)','err'); badN++; }
  }
  return {okN:okN, badN:badN, target:target};
}
async function doRestore(to){
  if(!batchItems.length){ alert('先解析 cas'); return; }
  batchItems.forEach((it,i)=>{ const c=document.querySelector('.itchk[data-i="'+i+'"]'); it._skip = c? !c.checked : false; });
  const r = await restoreCore(to, batchItems, setSt);
  if(!r){ $('restoremsg').innerHTML='已取消还原'; return; }
  $('restoremsg').innerHTML='批量还原完成: 确认成功 '+r.okN+' · 未确认/失败 '+r.badN+'(目标目录: '+esc(r.target)+')';
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
async function loadJobs(){
  const d = await api('/api/jobs');
  if(!d.ok||!d.data.length) return;
  $('jobs').innerHTML = d.data.map(j=>
    '<div style="margin:8px 0"><div>'+esc(j.title)+'</div>'+
    '<div class=mut>'+esc(j.status)+(j.progress!=null? ' · '+j.progress+'%':'')+' · '+esc(j.message||'')+'</div>'+
    (j.progress!=null&&j.status==='running'? '<div class=bar><i style="width:'+j.progress+'%"></i></div>':'')+
    (j.status==='done'&&j.result? '<div class=good>生成完成'+(j.result.uploaded_to? ' · 已上传回 '+esc(j.result.uploaded_to):'')+'</div>'+(j.result.cas_b64? '<div style="margin-top:6px"><a download="'+esc((j.result.name||'file')+'.cas')+'" href="data:text/plain;base64,'+j.result.cas_b64+'"><button class="small green">⬇ 下载这份 .cas</button></a></div>':''):'')+
    '</div>').join('');
}
status(); loadSettings(); loadList(); loadJobs();
setInterval(loadJobs, 2500);
</script></body></html>"""


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("cas-web v%s 监听 :%d  数据目录 %s  口令保护:%s"
          % (VERSION, PORT, DATA_DIR, "开" if password_required() else "关"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
