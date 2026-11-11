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
import urllib.request
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cas_tool as ct

VERSION = "2.7.3"
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
             "is_dir": bool(e.get("is_dir")), "id": e.get("id", "")} for e in content]


def ol_get(path):
    return ol_post("/fs/get", {"path": path})


def ol_storages():
    base, token = ol_cfg()
    if not base or not token:
        raise RuntimeError("未配置 OpenList")
    req = urllib.request.Request(base.rstrip("/") + "/api/admin/storage/list",
                                 headers={"Authorization": token})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        raise RuntimeError("读取 OpenList 存储列表失败: %s" % e)
    if data.get("code") != 200:
        raise RuntimeError("读取 OpenList 存储列表失败: %s"
                           % (data.get("message") or data.get("code")))
    return (data.get("data") or {}).get("content") or []


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


def _jobs_file():
    return os.path.join(DATA_DIR, "jobs.json")


_JOBS_SAVE_LAST = [0.0]


def _save_jobs(force=False):
    """任务历史落盘(保留最近 100 条),刷新/重启不丢;只存记录,
    不涉及任何已生成的文件。"""
    now = time.time()
    if not force and now - _JOBS_SAVE_LAST[0] < 2:
        return
    _JOBS_SAVE_LAST[0] = now
    try:
        with JOBS_LOCK:
            items = sorted(JOBS.values(), key=lambda j: -j["ts"])[:100]
        tmp = _jobs_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, _jobs_file())
    except OSError:
        pass


def _load_jobs():
    try:
        with open(_jobs_file(), encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return
    with JOBS_LOCK:
        for j in items:
            if j.get("status") == "running":
                j["status"] = "error"
                j["message"] = "服务重启,任务中断"
            JOBS[j["id"]] = j


_load_jobs()


def job_new(kind, title, path="", source=None):
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {"id": jid, "kind": kind, "title": title, "status": "running",
                     "progress": None, "message": "", "result": None,
                     "path": path, "source": source or {},
                     "ts": int(time.time())}
    _save_jobs(force=True)
    return jid


def job_update(jid, **kw):
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid].update(kw)
    _save_jobs(force=("status" in kw))


def _split_ol_path(path):
    path = (path or "").rstrip("/")
    if "/" not in path:
        return "/", path
    parent, name = path.rsplit("/", 1)
    return parent or "/", name


def _job_source(job):
    """生成任务的源文件定位信息；旧任务仅 OpenList 类型可由 path 补齐。"""
    source = job.get("source") or {}
    if source:
        return source
    if job.get("kind") == "gen" and job.get("path"):
        parent, name = _split_ol_path(job["path"])
        return {"type": "ol", "path": job["path"], "dir": parent, "name": name}
    return {}


_SOURCE_DELETING = set()
_SOURCE_DELETING_LOCK = threading.Lock()


def delete_job_source(jid):
    with _SOURCE_DELETING_LOCK:
        if jid in _SOURCE_DELETING:
            raise RuntimeError("该任务的源文件正在删除")
        _SOURCE_DELETING.add(jid)
    try:
        return _delete_job_source_once(jid)
    finally:
        with _SOURCE_DELETING_LOCK:
            _SOURCE_DELETING.discard(jid)


def _delete_job_source_once(jid):
    """删除一个已生成任务的源文件本体。

    仅处理已完成且结果显示 .cas 已保存回原目录的任务；删除前再次
    列源目录确认同名 .cas 仍在，避免源文件与 .cas 同时丢失。
    """
    with JOBS_LOCK:
        job = json.loads(json.dumps(JOBS.get(jid) or {}))
    if not job:
        raise RuntimeError("任务不存在")
    if job.get("kind") not in ("gen", "gen139"):
        raise RuntimeError("该任务不是生成任务")
    if job.get("status") != "done":
        raise RuntimeError("任务尚未生成完成")
    result = job.get("result") or {}
    if not result.get("uploaded_to"):
        raise RuntimeError(".cas 未保存回原目录，不能删除源文件")
    if result.get("source_deleted"):
        return "源文件已删除"
    source = _job_source(job)
    name = source.get("name") or result.get("name") or ""
    if not name:
        raise RuntimeError("任务缺少源文件信息，不能删除")
    cas_name = name + ".cas"
    if source.get("type") == "ol":
        parent = source.get("dir") or _split_ol_path(source.get("path") or "")[0]
        entries = ol_list(parent, refresh=True)
        if not any(e.get("name") == cas_name and not e.get("is_dir")
                   for e in entries):
            raise RuntimeError("源目录未找到已保存的 .cas，已取消删除")
        ol_post("/fs/remove", {"dir": parent, "names": [name]})
    elif source.get("type") == "139":
        parent = source.get("parent") or ""
        file_id = source.get("file_id") or ""
        if not parent or not file_id:
            raise RuntimeError("任务缺少移动云盘源文件标识，不能删除")
        entries = ct.list_139_files(m139_basic(), parent)
        if not any(e.get("name") == cas_name and not e.get("is_dir")
                   for e in entries):
            raise RuntimeError("源目录未找到已保存的 .cas，已取消删除")
        ct.trash_139(m139_basic(), file_id)
    else:
        raise RuntimeError("任务缺少源文件定位信息，不能删除")
    with JOBS_LOCK:
        current = JOBS.get(jid)
        if not current:
            return "源文件已删除"
        new_result = dict(current.get("result") or {})
        new_result["source_deleted"] = True
        current["result"] = new_result
    _save_jobs(force=True)
    return "源文件已删除"


def _run_gen(jid, size, opener, name, uploader):
    """流式读取一遍算完 md5/逐片 md5/sha256,生成双填 .cas。
    opener() 返回可 read 的响应;uploader(cas_bytes) 可选,返回上传到的位置。"""
    try:
        job_update(jid, message="连接数据源 ...")
        resp = opener()
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
        if uploader:
            job_update(jid, message="正在将CAS文件保存至同目录 ...")
            result["uploaded_to"] = uploader(cas_bytes)
        job_update(jid, status="done", progress=100, message="完成", result=result)
    except Exception as e:  # noqa: BLE001
        job_update(jid, status="error", message=str(e)[:300])


def run_gen_job(jid, ol_path, size, upload_back):
    name = ol_path.rstrip("/").split("/")[-1]
    up = None
    if upload_back:
        def up(cas_bytes):
            ol_put(ol_path + ".cas", cas_bytes)
            return ol_path + ".cas"
    _run_gen(jid, size, lambda: ol_download_iter(ol_path), name, up)


def run_gen_job139(jid, file_id, name, size, parent="/", upload_back=True,
                   overwrite=False):
    up = None
    if upload_back and parent:
        def up(cas_bytes):
            if overwrite:
                for it in ct.list_139_files(m139_basic(), parent):
                    if it["name"] == name + ".cas" and not it["is_dir"]:
                        ct.trash_139(m139_basic(), it["id"])
            ct.upload_139_bytes(m139_basic(), parent, name + ".cas", cas_bytes)
            return "移动云盘:" + name + ".cas(同目录)"
    _run_gen(jid, size, lambda: m139_download_iter(file_id), name, up)


# ---------------------------------------------------------------------------
# 还原(与 cas_tool CLI 同逻辑,输入为 cas 字节)
# ---------------------------------------------------------------------------

_M139_LOCK = threading.Lock()


def _m139_cfg():
    s = load_settings()
    env_phone, env_auth = ct.get_139_credentials()
    return {"phone": s["cas139_phone"] or env_phone,
            "auth": s["cas139_auth"] or env_auth,
            "user": s["cas139_user"], "pw": s["cas139_pass"],
            "cookies": s["cas139_mail_cookies"], "sms": s["cas139_sms_code"]}


def _m139_persist(res):
    patch = {}
    if res.get("auth"):
        patch["cas139_auth"] = res["auth"]
    if res.get("mail_cookies"):
        patch["cas139_mail_cookies"] = res["mail_cookies"]
    if res.get("udid"):
        patch["cas139_udid"] = res["udid"]
    if "sms_code" in res:
        patch["cas139_sms_code"] = res["sms_code"]
    if patch:
        cur = load_settings()
        cur.update(patch)
        store_settings(cur)


def _m139_login(cfg, allow_fast):
    """按 OpenList 驱动的顺序登录:先 Cookie 快速登录,再密码登录。"""
    errs = []
    if allow_fast and cfg["cookies"]:
        try:
            return ct.login_139_fast(cfg["user"] or cfg["phone"], cfg["cookies"])
        except Exception as e:  # noqa: BLE001
            errs.append("快速登录:%s" % e)
    if cfg["user"] and cfg["pw"]:
        try:
            return ct.login_139_password(cfg["user"], cfg["pw"],
                                         cfg["cookies"], cfg["sms"])
        except ct.SmsVerificationNeeded as e:
            if e.mail_cookies:
                cur = load_settings()
                cur["cas139_mail_cookies"] = e.mail_cookies
                store_settings(cur)
            raise
        except Exception as e:  # noqa: BLE001
            errs.append("密码登录:%s" % e)
    if not errs:
        raise RuntimeError("未配置密码登录回退:请先把 OpenList 139 存储里的 "
                           "MailCookies、Username、Password 复制到设置页对应三项并保存")
    raise RuntimeError("移动自动登录失败(%s)" % ";".join(errs))


def m139_ensure_auth(force=False):
    """返回可用的 Authorization(不带 Basic 前缀)。
    与 OpenList 驱动同逻辑:到期 15 天内先刷新,刷新失败回退密码登录;
    无授权时先 Cookie 快速登录、再密码登录;新授权自动回存设置。"""
    with _M139_LOCK:
        cfg = _m139_cfg()
        auth = cfg["auth"]
        if auth:
            exp = ct.auth_expiry_ms(auth)
            if exp is None:
                if not force:
                    return auth
            else:
                remain = exp - int(time.time() * 1000)
                if remain > 15 * 24 * 3600 * 1000 and not force:
                    return auth
                try:
                    new = ct.refresh_139_auth(auth)
                    _m139_persist({"auth": new})
                    return new
                except Exception:  # noqa: BLE001
                    if remain > 0 and not force:
                        try:
                            res = _m139_login(cfg, allow_fast=False)
                            _m139_persist(res)
                            return res["auth"]
                        except ct.SmsVerificationNeeded:
                            raise
                        except Exception:  # noqa: BLE001
                            return auth  # 还没过期,先用旧的
                    res = _m139_login(cfg, allow_fast=False)
                    _m139_persist(res)
                    return res["auth"]
        res = _m139_login(cfg, allow_fast=True)
        _m139_persist(res)
        return res["auth"]


def m139_basic():
    cfg = _m139_cfg()
    if not cfg["phone"] and not cfg["auth"] and not cfg["cookies"] \
            and not cfg["user"]:
        raise RuntimeError("未配置移动凭据(设置页填 AUTH,或配置密码登录回退三项)")
    return ct.build_basic_auth(cfg["phone"], m139_ensure_auth())


def m139_download_iter(file_id):
    url = ct.download_url_139(m139_basic(), file_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=120)


def restore_139(cas_bytes, parent="/"):
    obj = ct.decode_cas_bytes(cas_bytes)
    sha = str(obj.get("sha256", "")).strip().lower()
    if not sha:
        return False, "该 .cas 不含 sha256(天翼版),无法还原到移动云盘"
    try:
        basic = m139_basic()
    except Exception as e:  # noqa: BLE001
        return False, str(e)
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
    return False, ("文件不存在于云端,无法秒传(移动云盘未找到 "
                   "sha256=%s... 对应的文件)" % sha[:16])


def restore_189(cas_bytes, cas_name, target):
    obj = ct.decode_cas_bytes(cas_bytes)
    if not str(obj.get("md5", "")).strip():
        return False, "该 .cas 不含 md5(移动版),无法还原到天翼云盘"
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
                 "cas139_phone", "cas139_auth", "web_pw_hash",
                 "cas139_user", "cas139_pass", "cas139_mail_cookies",
                 "cas139_sms_code", "cas139_udid",
                 "mobile_home_id", "mobile_home_name", "tianyi_access",
                 "tianyi_session_key")
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
    out["mobile_dir_ids"] = {str(k).rstrip("/") or "/": str(v).strip()
                             for k, v in (d.get("mobile_dir_ids") or {}).items()
                             if str(v).strip()}
    out["mobile_dirs"] = [{"name": str(x.get("name") or "").strip(),
                           "id": str(x.get("id") or "").strip()}
                          for x in (d.get("mobile_dirs") or [])
                          if isinstance(x, dict) and str(x.get("id") or "").strip()]
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
    sk_in = str(body.get("tianyi_session_key") or "").strip()
    if sk_in:
        cur["tianyi_session_key"] = sk_in
    if body.get("clear_tianyi_session_key"):
        cur["tianyi_session_key"] = ""
    if "cas139_user" in body:
        cur["cas139_user"] = str(body.get("cas139_user") or "").strip()
    if "tianyi_access" in body:
        cur["tianyi_access"] = ("direct" if str(body.get("tianyi_access")) == "direct"
                                else "openlist")
    if "mobile_dirs_text" in body:
        dirs = []
        for line in str(body.get("mobile_dirs_text") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" in line:
                nm, fid = line.split("=", 1)
                nm, fid = nm.strip(), fid.strip()
            else:
                nm, fid = "", line
            if fid:
                dirs.append({"name": nm, "id": fid})
        cur["mobile_dirs"] = dirs
    if "mobile_home_id" in body:
        cur["mobile_home_id"] = str(body.get("mobile_home_id") or "").strip()
    if "mobile_home_name" in body:
        cur["mobile_home_name"] = str(body.get("mobile_home_name") or "").strip()
    for _k in ("cas139_pass", "cas139_mail_cookies", "cas139_sms_code"):
        _v = str(body.get(_k) or "").strip()
        if _v:
            cur[_k] = _v
    if body.get("clear_cas139_pass"):
        cur["cas139_pass"] = ""
    if body.get("clear_cas139_mail_cookies"):
        cur["cas139_mail_cookies"] = ""
    if body.get("clear_openlist_token"):
        cur["openlist_token"] = ""
    if body.get("clear_cas139_auth"):
        cur["cas139_auth"] = ""
    pw = str(body.get("web_password") or "")
    if pw:
        cur["web_pw_hash"] = hash_password(pw)
    if body.get("clear_web_password"):
        cur["web_pw_hash"] = ""
    if "mobile_dir_ids_text" in body:
        mp = {}
        for line in str(body.get("mobile_dir_ids_text") or "").splitlines():
            line = line.strip()
            idx = line.find("=")
            if idx < 0:
                idx = line.find(",")
            if idx <= 0:
                continue
            pth, fid = line[:idx].strip(), line[idx + 1:].strip()
            if pth.startswith("/") and fid:
                mp[pth.rstrip("/") or "/"] = fid
        cur["mobile_dir_ids"] = mp
    if (tok_in or ("openlist_url" in body and cur["openlist_url"] != old_url)) \
            and not body.get("clear_openlist_token"):
        base = cur["openlist_url"] or os.environ.get("OPENLIST_URL", "").strip()
        token = cur["openlist_token"] or os.environ.get("OPENLIST_TOKEN", "").strip()
        if base and token:
            probe_openlist(base, token)
    store_settings(cur)
    return public_settings(cur)


def _auth_expiry_text(auth):
    exp = ct.auth_expiry_ms(auth or "")
    if not exp:
        return ""
    from datetime import datetime
    return datetime.fromtimestamp(exp / 1000).strftime("%Y-%m-%d")


def public_settings(s=None):
    """给页面看的设置:密钥只报已设置/未设置,绝不回显明文。"""
    s = s or load_settings()
    return {"tianyi_home": s["tianyi_home"], "mobile_home": s["mobile_home"],
            "tianyi_paths": s["tianyi_paths"], "mobile_paths": s["mobile_paths"],
            "mobile_dir_ids_text": "\n".join(
                "%s = %s" % (k, v)
                for k, v in sorted((s.get("mobile_dir_ids") or {}).items())),
            "openlist_url": s["openlist_url"],
            "openlist_token_set": bool(s["openlist_token"]
                                       or os.environ.get("OPENLIST_TOKEN")),
            "openlist_token_source": ("页面设置" if s["openlist_token"] else
                                      ("环境变量" if os.environ.get("OPENLIST_TOKEN") else "")),
            "cas139_phone": s["cas139_phone"],
            "cas139_auth_set": bool(s["cas139_auth"]
                                    or ct.get_139_credentials()[1]),
            "cas139_auth_expiry": _auth_expiry_text(
                s["cas139_auth"] or ct.get_139_credentials()[1]),
            "cas139_user": s["cas139_user"],
            "cas139_pass_set": bool(s["cas139_pass"]),
            "cas139_mail_cookies_set": bool(s["cas139_mail_cookies"]),
            "cas139_sms_code_set": bool(s["cas139_sms_code"]),
            "tianyi_access": s.get("tianyi_access") or "openlist",
            "mobile_dirs": s.get("mobile_dirs") or [],
            "mobile_dirs_text": "\n".join(
                (d["name"] + " = " + d["id"]) if d["name"] else d["id"]
                for d in (s.get("mobile_dirs") or [])),
            "mobile_home_id": s["mobile_home_id"],
            "mobile_home_name": s["mobile_home_name"],
            "web_password_set": bool(s["web_pw_hash"] or WEB_PASSWORD),
            "tianyi_session_key_set": bool(s["tianyi_session_key"])}


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


def _scan_original_state(summary, entries):
    """根据 CAS 所在目录的文件清单判定原文件状态。

    返回 (state, existing_size)：同名且同大小为 existing；同名但
    大小不同为 name_conflict；未找到同名文件为 missing。
    """
    target = str(summary.get("name") or "").strip().lower()
    if not target:
        return "missing", None
    try:
        wanted_size = int(summary.get("size"))
    except (TypeError, ValueError):
        wanted_size = None
    for entry in entries:
        if entry.get("is_dir"):
            continue
        if str(entry.get("name") or "").strip().lower() != target:
            continue
        try:
            existing_size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            existing_size = None
        if wanted_size is not None and existing_size == wanted_size:
            return "existing", existing_size
        return "name_conflict", existing_size
    return "missing", None


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
                return self.ok(jobs[:100])
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
            if path == "/api/check189":
                try:
                    cas_bytes = base64.b64decode(body.get("cas_b64", ""))
                    meta = ct.decode_cas_bytes(cas_bytes)
                    sk = load_settings()["tianyi_session_key"]
                    return self.ok(ct.check_189_cas(sk, meta))
                except Exception as e:  # noqa: BLE001
                    return self.fail(str(e))
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
            if path == "/api/mkdir":
                p = str(body.get("path") or "").strip()
                if not p.startswith("/") or p == "/":
                    return self.fail("目录路径不对")
                ol_post("/fs/mkdir", {"path": p})
                return self.ok({"path": p})
            if path == "/api/m139/mkdir":
                nm = str(body.get("name") or "").strip()
                if not nm or "/" in nm:
                    return self.fail("目录名不对")
                data = ct.mkdir_139(m139_basic(),
                                    str(body.get("parent") or "/").strip() or "/",
                                    nm)
                return self.ok({"fileId": data.get("fileId") or ""})
            if path == "/api/m139/scan":
                return self.api_m139_scan(body)
            if path == "/api/m139/relogin":
                try:
                    m139_ensure_auth(force=True)
                    return self.ok({"msg": "移动授权已刷新/重新登录,新授权已保存"})
                except ct.SmsVerificationNeeded as e:
                    return self.fail(str(e))
                except Exception as e:  # noqa: BLE001
                    cfg = _m139_cfg()
                    exp = ct.auth_expiry_ms(cfg["auth"]) if cfg["auth"] else None
                    if exp and exp > int(time.time() * 1000):
                        return self.ok({"msg": "本次刷新未成功(%s);现有授权"
                                               "有效到 %s,暂不受影响。请填写上方"
                                               "密码登录回退三项并保存后,再执行"
                                               "刷新以自动续期"
                                               % (str(e)[:140],
                                                  _auth_expiry_text(cfg["auth"]))})
                    return self.fail(str(e))
            if path == "/api/m139/list":
                try:
                    items = ct.list_139_files(m139_basic(), body.get("parent") or "/")
                    return self.ok({"items": items})
                except Exception as e:  # noqa: BLE001
                    return self.fail(str(e))
            if path == "/api/m139/gen":
                fid = body.get("file_id") or ""
                gname = body.get("name") or ""
                gsize = int(body.get("size") or 0)
                if not fid or gsize <= 0:
                    return self.fail("缺少 file_id / size")
                if gname.lower().endswith(".cas"):
                    return self.fail("CAS 文件本身不能生成 CAS")
                parent = str(body.get("parent") or "/")
                jid = job_new("gen139", "生成 cas(移动直连): " + gname,
                              str(body.get("path") or ""),
                              {"type": "139", "file_id": fid,
                               "parent": parent, "name": gname})
                threading.Thread(target=run_gen_job139,
                                 args=(jid, fid, gname, gsize, parent,
                                       bool(body.get("upload_back", True)),
                                       bool(body.get("overwrite"))),
                                 daemon=True).start()
                return self.ok({"job_id": jid})
            if path == "/api/jobs/delete-source":
                results = []
                seen = set()
                for jid in body.get("ids") or []:
                    if jid in seen:
                        continue
                    seen.add(jid)
                    try:
                        msg = delete_job_source(jid)
                        results.append({"id": jid, "ok": True, "msg": msg})
                    except Exception as e:  # noqa: BLE001
                        results.append({"id": jid, "ok": False,
                                        "msg": str(e)[:300]})
                deleted = sum(1 for r in results if r["ok"])
                return self.ok({"deleted": deleted, "results": results})
            if path == "/api/jobs/clear":
                removed = 0
                with JOBS_LOCK:
                    if body.get("all"):
                        removed = len(JOBS)
                        JOBS.clear()
                    else:
                        for jid in body.get("ids") or []:
                            if JOBS.pop(jid, None) is not None:
                                removed += 1
                _save_jobs(force=True)
                return self.ok({"removed": removed})
            if path == "/api/gen":
                return self.api_gen(body)
            if path == "/api/restore139":
                cas_bytes = base64.b64decode(body.get("cas_b64", ""))
                parent = str(body.get("parent") or "").strip() or "/"
                ok, msg = restore_139(cas_bytes, parent)
                if ok and parent == "/":
                    msg += "(移动云盘根目录)"
                if ok and body.get("sidecar"):
                    try:
                        obj = ct.decode_cas_bytes(cas_bytes)
                        ct.upload_139_bytes(m139_basic(), parent,
                                            (obj.get("name") or "file") + ".cas",
                                            cas_bytes)
                        msg += "(CAS文件已保存至同目录)"
                    except Exception as e:  # noqa: BLE001
                        msg += "(CAS文件保存至同目录失败: %s)" % str(e)[:120]
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
        status = {"version": VERSION,
                  "tianyi_access": load_settings().get("tianyi_access") or "openlist",
                  "openlist_configured": bool(base and token),
                  "openlist_ok": False, "openlist_msg": "",
                  "m139_configured": bool(_m139_cfg()["auth"]
                                          or _m139_cfg()["cookies"]
                                          or _m139_cfg()["user"])}
        if base and token:
            try:
                ol_list("/")
                status["openlist_ok"] = True
                status["openlist_msg"] = "已连通"
            except Exception as e:  # noqa: BLE001
                status["openlist_msg"] = str(e)[:200]
        status["m139_ok"] = False
        status["m139_msg"] = ""
        if status["m139_configured"]:
            try:
                ct.list_139_files(m139_basic(), "/")
                status["m139_ok"] = True
                status["m139_msg"] = "移动直连已连通"
            except Exception as e:  # noqa: BLE001
                status["m139_msg"] = str(e)[:200]
        return self.ok(status)

    def api_list(self, qs):
        path = (qs.get("path") or ["/"])[0]
        refresh = (qs.get("refresh") or [""])[0] == "1"
        entries = ol_list(path, refresh=refresh)
        return self.ok({"path": path, "entries": entries})

    def api_gen(self, body):
        path = body.get("path") or ""
        size = int(body.get("size") or 0)
        if not path or size <= 0:
            return self.fail("缺少 path / size")
        if path.lower().endswith(".cas"):
            return self.fail("CAS 文件本身不能生成 CAS")
        parent, name = _split_ol_path(path)
        jid = job_new("gen", "生成 cas: " + path, path,
                      {"type": "ol", "path": path, "dir": parent, "name": name})
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
                entries = ol_list(dirpath, refresh=True)
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
                        summary = cas_summary(data)
                        state, original_size = _scan_original_state(summary, entries)
                        found.append({"path": p, "cas_name": e["name"],
                                      "cas_b64": base64.b64encode(data).decode(),
                                      "source_type": "ol",
                                      "source_dir": dirpath,
                                      "source_parent_id": "",
                                      "original_state": state,
                                      "original_size": original_size,
                                      **summary})
                    except Exception:  # noqa: BLE001
                        continue

        walk(root, 0)
        return self.ok(found)

    def api_m139_scan(self, body):
        """扫描 139 文件夹(直连)下的 .cas:递归列目录,逐个下载解析,
        返回与 /api/scan 同形的清单。"""
        root_id = str(body.get("parent") or "/").strip() or "/"
        recursive = bool(body.get("recursive", True))
        basic = m139_basic()
        found = []

        def walk(fid, disp, depth):
            if len(found) >= 300:
                return
            try:
                entries = ct.list_139_files(basic, fid)
            except Exception:  # noqa: BLE001
                return
            for e in entries:
                if len(found) >= 300:
                    break
                if e.get("is_dir"):
                    if recursive and depth < 6:
                        walk(e["id"], disp + "/" + e["name"], depth + 1)
                elif (e["name"].lower().endswith(".cas")
                        and 0 < (e.get("size") or 0) <= 1024 * 1024):
                    try:
                        url = ct.download_url_139(basic, e["id"])
                        req = urllib.request.Request(
                            url, headers={"User-Agent": "cas-tool"})
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            data = resp.read()
                        summary = cas_summary(data)
                        state, original_size = _scan_original_state(summary, entries)
                        found.append({"path": disp + "/" + e["name"],
                                      "cas_name": e["name"],
                                      "cas_b64": base64.b64encode(data).decode(),
                                      "source_type": "139",
                                      "source_dir": disp,
                                      "source_parent_id": fid,
                                      "original_state": state,
                                      "original_size": original_size,
                                      **summary})
                    except Exception:  # noqa: BLE001
                        continue

        walk(root_id, "移动云盘", 0)
        return self.ok(found)

    def api_sidecar(self, body):
        """把 .cas 作为侧车文件存到目标目录里视频旁边:<dir>/<视频名>.cas。"""
        cas_bytes = base64.b64decode(body.get("cas_b64", ""))
        summary = cas_summary(cas_bytes)
        video = body.get("video_name") or summary.get("name") or ""
        if not video:
            return self.fail("CAS文件中未包含源文件名,无法确定保存名称")
        dest = (body.get("dir") or "/").rstrip("/") + "/" + video + ".cas"
        ol_put(dest, cas_bytes)
        return self.ok({"path": dest})



# ---------------------------------------------------------------------------
# 页面(单文件,移动优先)
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>CAS 工具 · 登录</title><style>
body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f7f5ef;color:#1c1c1a}
.wrap{max-width:420px;margin:15vh auto;padding:20px}
input,button{width:100%;box-sizing:border-box;font-size:17px;padding:13px;border-radius:10px;border:1px solid #c7c7cc;margin-top:10px}
button{background:#1c1c1a;color:#fdfcf8;border:none;font-weight:600}
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
body{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;margin:0;background:#f7f5ef;color:#1c1c1a;-webkit-tap-highlight-color:transparent}
.wrap{max-width:720px;margin:0 auto;padding:18px 16px 56px}
h1{font-family:Georgia,'Songti SC','STSong',serif;font-size:24px;margin:4px 2px 0;font-weight:700;letter-spacing:.5px;border-bottom:3px double #1c1c1a;padding-bottom:10px}
h2{font-size:15px;margin:0 0 10px;font-weight:700;letter-spacing:.4px}
.card{background:#fff;border:1px solid #e5e1d5;border-radius:8px;padding:18px;margin:14px 0;box-shadow:0 1px 3px rgba(60,50,20,.05)}
button{font-size:15px;padding:11px 16px;border-radius:8px;border:1px solid transparent;background:#1c1c1a;color:#fdfcf8;font-weight:600;min-height:46px;cursor:pointer}
button:active{opacity:.75}
button.grey{background:#f1eee5;color:#1c1c1a;border-color:#ddd8c8}
button.green{background:#177245;color:#fff}
button.red{background:#c62f3b;color:#fff}
button.small{padding:7px 12px;min-height:36px;font-size:13px;border-radius:7px}
input,textarea,select{width:100%;box-sizing:border-box;font-size:16px;padding:11px 12px;border-radius:8px;border:1px solid #d8d3c1;margin:6px 0;background:#fff;outline:none}
input:focus,textarea:focus,select:focus{border-color:#177245;box-shadow:0 0 0 3px rgba(23,114,69,.13)}
textarea{min-height:64px}
.row{display:flex;align-items:center;gap:9px;padding:9px 2px;border-bottom:1px solid #f0ede2}
.row .grow{flex:1;min-width:0;word-break:break-all}
.dirrow{cursor:pointer;border-radius:6px}
.dirrow:active{background:#f2f6f0}
.bar{height:8px;background:#e9e6da;border-radius:4px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:#177245}
.mut{color:#8a8577;font-size:13px}
.msg{white-space:pre-wrap;word-break:break-all;font-size:14px;margin-top:8px;line-height:1.55}
.err{color:#c62f3b}.good{color:#177245}
.kv{font-size:13px;color:#6d675a;word-break:break-all}
.tabbar{position:sticky;top:10px;z-index:6;display:flex;gap:4px;background:#eae7db;padding:4px;border-radius:9px;margin:14px 0 2px}
.tabbtn{flex:1;background:transparent;color:#6d675a;border:none;border-radius:7px;min-height:42px;box-shadow:none}
.tabbtn.on{background:#fff;color:#1c1c1a;box-shadow:0 1px 4px rgba(60,50,20,.16)}
.crumbs{padding:8px 2px 2px;font-size:14px;line-height:2.1}
.crumb{color:#177245;background:#edf3ec;padding:4px 10px;border-radius:6px;cursor:pointer;white-space:nowrap}
#restorebox>button{display:block;width:100%;margin-top:9px}
#scanbtns>button{display:block;width:100%;margin-top:9px}
.dropzone{border:1.5px dashed #c9c2ab;border-radius:8px;padding:20px 14px;text-align:center;color:#6d675a;background:#fbfaf5;cursor:pointer;margin:6px 0}
.dropzone.drag{border-color:#177245;background:#eef5ee;color:#177245}
.overlay{position:fixed;inset:0;background:rgba(22,20,14,.46);z-index:50;align-items:flex-start;justify-content:center;padding:5vh 12px 12px}
.sheet{background:#fff;border-radius:12px;padding:18px;width:100%;max-width:560px;max-height:88vh;overflow:auto;box-shadow:0 16px 48px rgba(0,0,0,.28)}
@media (prefers-color-scheme:dark){body{background:#141412;color:#ece9df}.card{background:#1f1f1d!important;border-color:#35342c!important;box-shadow:none!important}h1{border-bottom-color:#ece9df}input,textarea,select{background:#28281f!important;color:#ece9df;border-color:#403f35!important}.tabbar{background:#28281f!important}.tabbtn{color:#a49d8a!important}.tabbtn.on{background:#3b3a30!important;color:#fff!important;box-shadow:none!important}button{background:#ece9df;color:#141412}button.grey{background:#33322a!important;color:#ece9df!important;border-color:#48473c!important}button.green{background:#1e8a52!important;color:#fff!important}.crumb{background:#22301f!important;color:#a5d3b2!important}.row{border-color:#2d2d28!important}.dirrow:active{background:#22301f!important}.sheet{background:#1f1f1d}.dropzone{background:#22221c;border-color:#4a493e;color:#a49d8a}}
button:disabled{opacity:.35}
</style></head><body><div class="wrap">
<h1>🗂 CAS 双云工具 <span class="mut" id="ver"></span></h1>
<div class="card"><div id="status" class="msg">检查连接中…</div></div>

<div class="tabbar">
<button id="tabbtn-restore" class="tabbtn on" onclick="switchTab('restore')">CAS 还原</button>
<button id="tabbtn-gen" class="tabbtn" onclick="switchTab('gen')">CAS 生成</button>
<button id="tabbtn-settings" class="tabbtn" onclick="switchTab('settings')">设置</button>
</div>

<div id="picker" class="overlay" style="display:none" onclick="if(event.target===this)togglePicker()"><div class="sheet">
<div class="row"><b class="grow">📁 选择目录</b>
<button class="small green" onclick="pickerUse()">✔ 用当前目录</button>
<button class="small grey" onclick="togglePicker()">关闭</button></div>
<div class="row" id="pickercloudtabs"><button class="tabbtn on" id="ptab189" onclick="showPickerCloud('189')">☁️ 天翼云盘</button>
<button class="tabbtn" id="ptab139" onclick="showPickerCloud('139')">📱 移动云盘</button></div>
<div id="pane189">
<div id="warn189" class="msg err" style="display:none"></div>
<div id="crumbs" class="crumbs"></div>
<div class="row"><input id="pickerfilter" class="grow" placeholder="筛选本层文件夹…" oninput="renderPicker()">
<button class="small grey" id="ph189" onclick="pickerHome('189')">天翼挂载目录</button></div>
<div id="pickerlist"></div>
<div class="row"><button class="small grey" onclick="mkdirHere('189')">＋ 新建目录</button></div>
</div>
<div id="pane139" style="display:none">
<div id="warn139" class="msg err" style="display:none"></div>
<div id="p139crumbs" class="crumbs"></div>
<div id="p139list"></div>
<div class="row"><button class="small grey" onclick="mkdirHere('139')">＋ 新建目录</button><button class="small grey" onclick="p139SaveDir()">＋ 把当前目录存到设置</button></div>
</div>
</div></div>

<div id="tab-restore">
<div class="card"><h2>还原 CAS</h2>
<input type="file" id="casfile" multiple hidden onchange="inspectCas()">
<div id="dropzone" class="dropzone" onclick="$('casfile').click()">📥 把 .cas / .zip 拖到这里,或点击选择文件(可多选)</div>
<textarea id="castext" placeholder="或把 .cas 的内容(Base64 文本)粘贴到这里"></textarea>
<div class="row"><span><button onclick="inspectCas()">解析</button></span><button class="grey" onclick="clearRestore()">清空</button></div>
<div id="inspect" class="msg"></div>
<div id="restorebox" style="display:none">
<div class="row"><input id="target189" class="grow" placeholder="目标目录(不填:天翼按钮用天翼挂载目录,移动按钮用移动挂载目录)" oninput="$('parent139').value='';picked139Path=''">
<button class="small grey" onclick="togglePicker('target189')">📁 选目录</button></div>
<div class="row"><button class="small grey" onclick="fillHome('189')">填入天翼挂载目录</button>
<button class="small grey" onclick="fillHome139()">填入移动云盘挂载目录</button></div>
<input id="parent139" type="hidden" value="">
<div class="mut" id="home139label"></div>
<label class="mut"><input type="checkbox" id="sidecar" style="width:auto" checked> 还原成功后,CAS文件保存至同目录</label>
<button class="green" id="btnTy" onclick="doRestore('tianyi')">还原到天翼云盘</button>
<button class="green" id="btnMb" onclick="doRestore('mobile')">还原到移动云盘</button>
</div><div id="restoremsg" class="msg"></div>
<div style="border-top:1px solid #eef0f4;margin-top:14px;padding-top:12px">
<h2>🔍 扫描网盘CAS</h2>
<div class="row"><input id="scanpath" class="grow" placeholder="要扫描的目录(选目录里可切换天翼/移动)" oninput="scanSrc='ol';scan139=null">
<button class="small grey" onclick="togglePicker('scanpath')">📁 选目录</button>
<button class="small" onclick="scanCas()">开始扫描</button></div>
<label class="mut"><input type="checkbox" id="scanrec" style="width:auto" checked> 包含子文件夹(递归最多 6 层,最多取 300 份)</label>
<div id="scanmsg" class="msg"></div>
<div id="scanlist"></div>
<div id="scanbtns" style="display:none">
<div class="row"><button class="small grey" onclick="scanCheckAll(true)">全选可还原</button>
<button class="small grey" onclick="scanCheckAll(false)">全不选</button>
<span class="mut">全选仅选择可还原条目；推送时目标目录用上方还原区</span></div>
<div class="row"><button class="green" onclick="pushScanToRestore()">⬆ 推送到还原区</button>
<button onclick="restoreScanInPlace()">原目录还原</button></div>
<div id="scanrestoremsg" class="msg"></div>
</div>
</div></div>

</div>

<div id="tab-gen" style="display:none">
<div class="row"><button class="small grey" id="srcOl" onclick="setBrowseSrc('ol')">☁️ 天翼云盘</button>
<button class="small grey" id="src139" onclick="setBrowseSrc('139')">📱 移动云盘(直连)</button></div>
<div class="card" id="olbrowse"><h2>浏览网盘 · 生成 CAS</h2>
<div class="row"><button class="small grey" onclick="goUp()">⬆ 上级</button>
<span class="grow" id="curpath">/</span>
<button class="small grey" onclick="loadList()">刷新</button></div>
<div class="row"><button class="small grey" onclick="goHome('189')">跳到天翼挂载目录</button></div>
<label class="mut"><input type="checkbox" id="upback" style="width:auto" checked> 生成后,CAS文件保存至同目录</label>
<div id="entries"></div></div>
<div class="card" id="m139browse" style="display:none"><h2>移动云盘(直连) · 生成 CAS</h2>
<div class="row"><label class="mut"><input type="checkbox" id="upback139" style="width:auto" checked> 生成后,CAS文件保存至同目录</label></div>
<div class="row"><button class="small grey" onclick="m139Roots()">🏠 目录</button>
<button class="small grey" onclick="m139GoHome()">⭐ 默认目录</button>
<span class="grow" id="m139crumbs">移动云盘</span>
<button class="small grey" onclick="loadM139()">刷新</button></div>
<div class="mut">通过 139 接口直接浏览,不经 OpenList;生成的CAS文件可在下方任务中下载,或按上方选项保存至同目录。</div>
<div id="m139entries"></div></div>

<div class="card"><h2>任务</h2><div id="jobs" class="mut">暂无</div><div id="jobsmsg" class="msg"></div></div>
</div>

<div id="tab-settings" style="display:none">
<div class="card"><h2>☁️ 天翼云盘</h2>
<div class="mut">密钥类留空 = 不修改,不回显明文。</div>
<label class="mut">接入方式</label>
<select id="cfgaccess"><option value="openlist">OpenList</option><option value="direct" disabled>直连(开发中)</option></select>
<label class="mut">OpenList 地址</label>
<input id="cfgurl" placeholder="如 http://192.168.111.2:5244">
<label class="mut">OpenList Token <span id="tokstate" class="kv"></span></label>
<input id="cfgtoken" type="password" placeholder="粘贴 OpenList 后台的令牌">
<div class="mut">填 OpenList 后台「设置 → 其他 → 令牌」里的长期令牌(F12 抓的会过期)。保存时先实测连通,不通不存。</div>
<label class="mut"><input type="checkbox" id="cfgtokclear" style="width:auto"> 清除页面里存的 Token(改回用 compose 环境变量里的)</label>
<label class="mut">SessionKey(网页版)<span id="skstate" class="kv"></span></label>
<input id="cfgsk189" type="password" placeholder="用于还原失败时查询原因,与 OpenList 还原无关">
<div class="mut">获取:电脑浏览器登录 cloud.189.cn,按 F12 打开控制台,输入 sessionStorage.getItem('sessionKey') 回车,复制结果填入。会过期,失效重新获取。</div>
<label class="mut"><input type="checkbox" id="cfgskclear" style="width:auto"> 清除已存的 SessionKey</label>
<label class="mut">挂载目录(天翼云盘在 OpenList 里的目录)</label>
<div class="row"><input id="home189" class="grow" placeholder="如 /TV/天翼云盘在线">
<button class="small grey" onclick="togglePicker('home189')">📁 选目录</button></div>
<div class="mut">首页选目录只能在这里面选;还原目标留空时用它。</div>
<label class="mut">额外目录(可多个,一行一个;还原校验用)</label>
<textarea id="paths189" placeholder="/TV/天翼云盘在线&#10;/天翼云盘"></textarea>
<button class="small grey" onclick="togglePicker('append:paths189')">📁 添加一个目录</button>
</div>
<div class="card"><h2>📱 移动云盘</h2>
<div class="mut">接入方式:<b>直连 139 接口</b>,不经 OpenList。</div>
<label class="mut">移动云盘手机号(可不填,AUTH 为完整形式时用不到)</label>
<input id="cfgphone" placeholder="手机号">
<label class="mut">移动 AUTH <span id="authstate" class="kv"></span></label>
<input id="cfgauth" type="password" placeholder="形如 Basic xxxxx,留空 = 不修改">
<label class="mut"><input type="checkbox" id="cfgauthclear" style="width:auto"> 清除页面里存的移动 AUTH</label>
<label class="mut">—— 密码登录回退(与 OpenList 的 139 存储配置同名同格式,可直接复制过来)——</label>
<label class="mut">139 用户名 Username(139 邮箱 / 手机号)</label>
<input id="cfguser" placeholder="Username">
<label class="mut">139 密码 Password <span id="passstate" class="kv"></span></label>
<input id="cfgpass139" type="password" placeholder="留空 = 不修改">
<label class="mut">邮箱 Cookie MailCookies(mail.10086.cn 的 Cookie 整串) <span id="cookstate" class="kv"></span></label>
<textarea id="cfgcookies" rows="2" placeholder="形如 Os_SSo_Sid=...; RMKEY=...;留空 = 不修改"></textarea>
<label class="mut">移动短信验证码 SmsCode <span id="smsstate" class="kv"></span></label>
<input id="cfgsms" type="password" placeholder="平时留空;登录触发风控、收到验证码后填这里再点下方刷新">
<div class="mut">填好后 AUTH 到期自动刷新、失败自动重登;触发短信风控时把验证码填到 SmsCode 再点刷新。</div>
<div class="row"><button class="small green" onclick="relogin139()">🔄 立即刷新移动授权</button><span id="reloginmsg" class="mut"></span></div>
<label class="mut"><input type="checkbox" id="cfgpassclear" style="width:auto"> 清除 139 密码</label>
<label class="mut"><input type="checkbox" id="cfgcookclear" style="width:auto"> 清除邮箱 Cookie</label>
<label class="mut">移动云盘目录(可多个,一行一个:名称 = 目录 ID;第一行 = 默认目录)</label>
<textarea id="dirs139" rows="3" placeholder="中转 = 0123456789abcdef&#10;电影 = fedcba9876543210"></textarea>
<div class="mut">目录 ID:在还原页「选目录 → 移动云盘」里进入文件夹点「把当前目录存到设置」即可;或看 yun.139.com F12 里 file/list 请求的 parentFileId。</div>
</div>
<div class="card"><h2>🔒 页面与安全</h2>
<label class="mut">页面访问口令 <span id="pwstate" class="kv"></span></label>
<input id="cfgpw" type="password" placeholder="留空 = 不修改">
<label class="mut"><input type="checkbox" id="cfgpwclear" style="width:auto"> 清除访问口令(打开页面不再要口令)</label>
<button onclick="saveSettings()">保存设置</button>
<div id="setmsg" class="msg"></div>
<div class="mut">设置存群晖数据目录,所有设备共用;口令忘了可用 compose 的 WEB_PASSWORD 救急。</div>
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
  const tmode = s.tianyi_access==='direct'? '直连' : 'OpenList';
  $('status').innerHTML =
    (s.openlist_configured? (s.openlist_ok? '🟢 天翼云盘 '+tmode+' '+esc(s.openlist_msg) : '🔴 天翼云盘 '+tmode+' 未连通: '+esc(s.openlist_msg)) : '🔴 天翼云盘未配置(设置页天翼云盘区填 OpenList 地址 / Token)')
    +'<br>'+(s.m139_configured? (s.m139_ok? '🟢 移动直连已连通' : '🔴 移动直连未通: '+esc(s.m139_msg||'')) : '🟡 未配置移动凭据(设置页可填;移动还原不可用,天翼不受影响)');
}
function fileToB64(file){return new Promise((res,rej)=>{const r=new FileReader();
  r.onload=()=>res(r.result.split(',')[1]); r.onerror=rej; r.readAsDataURL(file);});}
let batchItems=[], dropFiles=null;
async function collectItems(){
  const items=[];
  const fl = dropFiles || $('casfile').files; dropFiles=null;
  for(const f of fl){
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
  renderBatch(); refreshBtns();
  $('restorebox').style.display='block';
}
function clearRestore(){
  batchItems=[]; dropFiles=null;
  $('casfile').value='';
  $('castext').value='';
  $('inspect').innerHTML='';
  $('restoremsg').innerHTML='';
  $('restorebox').style.display='none';
  refreshBtns();
}
function batchAll(v){ document.querySelectorAll('.itchk').forEach(c=>{ c.checked = v; }); refreshBtns(); }
function syncBatchAll(){ const cs = Array.from(document.querySelectorAll('.itchk')); const b = $('batchall'); if(b && cs.length) b.checked = cs.every(c=>c.checked); refreshBtns(); }
function renderBatch(){
  const head = batchItems.length? '<div><label><input type="checkbox" id="batchall" style="width:auto" checked onchange="batchAll(this.checked)"> <b>全选</b></label> <span class=mut>共 '+batchItems.length+' 份 cas(取消勾选的不会还原)</span></div>' : '';
  $('inspect').innerHTML = head + batchItems.map((it,i)=>
    it.info? '<div><label><input type="checkbox" class="itchk" data-i="'+i+'" style="width:auto" checked onchange="syncBatchAll()"> 📄 '+esc(it.info.name)+'</label> <span class=mut>'+esc(it.info.kind)+' · '+esc(it.info.size)+' 字节</span> <span id="st'+i+'"></span>'+
      (it.src? '<br><span class=kv>来源: '+esc(it.src)+'</span>':'')+
      '<br><span class=kv>md5: '+esc(it.info.md5||'(空)')+' · sliceMd5: '+esc(it.info.sliceMd5||'(空)')+' · sha256: '+esc(it.info.sha256||'(空)')+'</span>'
      +'</div>'
           : '<div>📄 '+esc(it.name)+' <span class=err>解析失败: '+esc(it.err)+'</span></div>'
  ).join('');
}
let scanItems=[], scanSrc='ol', scan139=null;
async function scanCas(){
  let d;
  $('scanmsg').textContent='扫描中,请稍候';
  $('scanbtns').style.display='none';
  if(scanSrc==='139' && scan139){
    d=await api('/api/m139/scan',{parent:scan139.id,recursive:$('scanrec').checked});
  } else {
    const p=$('scanpath').value.trim();
    if(!p){ alert('请先填写要扫描的目录(或点「选目录」)'); return; }
    d=await api('/api/scan',{path:p,recursive:$('scanrec').checked});
  }
  if(!d.ok){ $('scanmsg').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  if(!d.data.length){ $('scanmsg').textContent='该目录下未找到 CAS 文件'; scanItems=[]; $('scanlist').innerHTML=''; return; }
  scanItems = d.data.map(x=>({name:x.cas_name, b64:x.cas_b64, src:x.path,
    srcType:x.source_type || (scanSrc==='139'? '139' : 'ol'),
    srcDir:x.source_dir || '', srcParent:x.source_parent_id || '',
    origState:x.original_state || 'missing', origSize:x.original_size,
    info:{name:x.name, size:x.size, kind:x.kind, md5:x.md5, sliceMd5:x.sliceMd5, sha256:x.sha256}}));
  renderScan();
  $('restorebox').style.display='block';
  $('scanbtns').style.display='block';
  const counts={missing:0, existing:0, name_conflict:0};
  scanItems.forEach(it=>{ counts[it.origState]=(counts[it.origState]||0)+1; });
  $('scanmsg').innerHTML='<span class=good>找到 '+d.data.length+' 份 CAS</span>'
    +' —— 可还原 '+counts.missing+' 份 · 原文件已存在 '+counts.existing+' 份 · 同名大小不一致 '+counts.name_conflict+' 份';
}
function scanStateInfo(it){
  if(it.origState==='existing') return {text:' ✓ 原文件已存在', cls:'good'};
  if(it.origState==='name_conflict') return {text:' ⚠ 同名文件大小不一致(现有 '+esc(it.origSize)+' 字节)', cls:'err'};
  return {text:'', cls:'mut'};
}
function renderScan(){ setTimeout(refreshBtns, 0);
  $('scanlist').innerHTML = scanItems.map((it,i)=>
    '<div><label><input type="checkbox" class="scanchk" data-i="'+i+'" style="width:auto" onchange="refreshBtns()"> 📄 '+esc(it.info.name)+'</label> <span class=mut>'+esc(it.info.kind)+' · '+esc(it.info.size)+' 字节</span> <span id="sst'+i+'" class="'+scanStateInfo(it).cls+'">'+scanStateInfo(it).text+'</span>'+
    '<br><span class=kv>来源: '+esc(it.src)+'</span></div>').join('');
}
function scanCheckAll(v){ document.querySelectorAll('.scanchk').forEach(c=>{ const it=scanItems[Number(c.dataset.i)]; c.checked = v && it && it.origState==='missing'; }); refreshBtns(); }
function scanSetSt(i,txt,cls){ const el=$('sst'+i); if(el){ el.textContent=txt; el.className=cls; } }
function pushScanToRestore(){
  const picked=[];
  scanItems.forEach((it,i)=>{ const c=document.querySelector('.scanchk[data-i="'+i+'"]'); if(c && c.checked) picked.push(it); });
  if(!picked.length){ alert('请先在扫描列表中勾选 cas'); return; }
  let added=0;
  for(const it of picked){
    if(batchItems.some(b=>b.b64===it.b64)) continue;
    batchItems.push({name:it.name, b64:it.b64, src:it.src, info:it.info});
    added++;
  }
  renderBatch();
  $('restorebox').style.display='block';
  refreshBtns();
  $('scanrestoremsg').innerHTML='<span class=good>已推送 '+added+' 份到还原区</span>'
    +(added<picked.length? '(另有 '+(picked.length-added)+' 份已在还原区,跳过)':'')
    +' —— 请在上方还原区选择目标目录后执行还原';
  window.scrollTo({top:0, behavior:'smooth'});
}
let scanRestoring=false;
async function refreshScanState(it, cache){
  const key = it.srcType==='139'? '139:'+it.srcParent : 'ol:'+(it.srcDir||'');
  if(!cache.has(key)){
    cache.set(key, (async()=>{
      try{
        if(it.srcType==='139'){
          const d=await api('/api/m139/list',{parent:it.srcParent});
          return d.ok? (d.data.items||[]) : null;
        }
        const target=it.srcDir || (it.src.replace(/\/[^\/]*$/,'') || '/');
        const d=await api('/api/list?path='+encodeURIComponent(target)+'&refresh=1');
        return d.ok? (d.data.entries||[]) : null;
      }catch(e){ return null; }
    })());
  }
  const entries=await cache.get(key);
  if(!entries) return it.origState;
  const wanted=String(it.info.name||'').trim().toLowerCase();
  const found=entries.find(e=>!e.is_dir && String(e.name||'').trim().toLowerCase()===wanted);
  if(!found){ it.origState='missing'; it.origSize=null; }
  else {
    const sz=Number(found.size||0);
    it.origSize=sz;
    it.origState = sz===Number(it.info.size)? 'existing' : 'name_conflict';
  }
  return it.origState;
}
async function restoreScanInPlace(){
  if(scanRestoring) return;
  const picked=[];
  scanItems.forEach((it,i)=>{ const c=document.querySelector('.scanchk[data-i="'+i+'"]'); if(c && c.checked) picked.push(i); });
  if(!picked.length){ alert('请先在扫描列表中勾选 CAS'); return; }
  scanRestoring=true;
  let okN=0, skipN=0, badN=0;
  const cache=new Map(), actionable=[];
  for(const i of picked){
    const it=scanItems[i];
    scanSetSt(i,' 状态确认中…','mut');
    await refreshScanState(it, cache);
    if(it.origState==='existing'){ scanSetSt(i,' ✓ 原文件已存在,已跳过','good'); skipN++; }
    else if(it.origState==='name_conflict'){ scanSetSt(i,' ⚠ 同名文件大小不一致,已跳过','err'); skipN++; }
    else actionable.push(i);
  }
  if(actionable.length && !confirm('将对 '+actionable.length+' 份 CAS 执行原目录还原'+(skipN? ',另有 '+skipN+' 份将跳过':'')+',是否继续?')){
    actionable.forEach(i=>scanSetSt(i,'','mut'));
    $('scanrestoremsg').textContent='已取消原目录还原';
    scanRestoring=false;
    return;
  }
  for(const i of actionable){
    const it=scanItems[i];
    scanSetSt(i,' 原目录还原中…','mut');
    try{
      if(it.srcType==='139'){
        if(!it.srcParent) throw new Error('缺少来源目录 ID');
        const d=await api('/api/restore139',{cas_b64:it.b64,parent:it.srcParent,sidecar:false});
        if(!d.ok) throw new Error(d.msg||'还原失败');
        it.origState='existing'; it.origSize=Number(it.info.size);
        scanSetSt(i,' ✓ 已还原到原目录','good'); okN++;
      } else {
        const target=it.srcDir || (it.src.replace(/\/[^\/]*$/,'') || '/');
        const d=await api('/api/restoreol',{cas_b64:it.b64,cas_name:it.name,target:target});
        if(!d.ok){
          const why=await failReason189(it);
          throw new Error(why||d.msg||'还原失败');
        }
        scanSetSt(i,' 回查中…','mut');
        await sleepMs(1500);
        const ex=await api('/api/exists',{path:target,name:it.info.name});
        if(ex.ok && ex.data.found){
          it.origState='existing'; it.origSize=Number(it.info.size);
          scanSetSt(i,' ✓ 已还原到原目录','good'); okN++;
        }
        else {
          const why=await failReason189(it);
          throw new Error(why||'文件不存在于云端,无法秒传');
        }
      }
    }catch(e){ scanSetSt(i,' ✗ '+e.message,'err'); badN++; }
  }
  if(actionable.length){
    $('scanrestoremsg').innerHTML='<span class='+(badN? 'err':'good')+'>原目录还原完成: 成功 '+okN+' · 跳过 '+skipN+' · 失败 '+badN+'</span>';
  } else {
    $('scanrestoremsg').innerHTML='<span class=good>所选 CAS 均无需还原: 已跳过 '+skipN+' 份</span>';
  }
  scanRestoring=false;
}
function setSt(i,txt,cls){ const el=$('st'+i); if(el) el.innerHTML='<span class='+cls+'>'+esc(txt)+'</span>'; }
const dz=$('dropzone');
['dragenter','dragover'].forEach(ev=>dz.addEventListener(ev,e=>{ e.preventDefault(); dz.classList.add('drag'); }));
['dragleave','drop'].forEach(ev=>dz.addEventListener(ev,e=>{ e.preventDefault(); dz.classList.remove('drag'); }));
dz.addEventListener('drop',e=>{ const fs=e.dataTransfer.files; if(fs && fs.length){ dropFiles=fs; inspectCas(); } });
['dragover','drop'].forEach(ev=>window.addEventListener(ev,e=>e.preventDefault()));
let pickerPath='/', pickerTarget='target189', pickerScope=null;
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
['cfgtoken','cfgauth','cfgpw','cfgpass139','cfgsms','cfgcookies'].forEach(function(id){ const el=$(id); if(el) el.addEventListener('focus', function(){ if(el.value===MASK) el.select(); }); });
async function relogin139(){
  $('reloginmsg').textContent='先保存设置...';
  await saveSettings();
  $('reloginmsg').textContent='刷新/登录中...';
  const d = await api('/api/m139/relogin',{});
  $('reloginmsg').innerHTML = d.ok? '<span class=good>'+esc(d.data.msg)+'</span>' : '<span class=err>'+esc(d.msg)+'</span>';
  loadSettings(); status();
}
async function loadSettings(){
  const d = await api('/api/settings');
  if(!d.ok) return;
  SETTINGS=d.data;
  $('home189').value=SETTINGS.tianyi_home||''; $('cfgaccess').value=SETTINGS.tianyi_access||'openlist';
  updateHome139Label();
  $('cfgurl').value=SETTINGS.openlist_url||''; $('cfgphone').value=SETTINGS.cas139_phone||'';
  $('paths189').value=(SETTINGS.tianyi_paths||[]).join('\n');
  $('dirs139').value=SETTINGS.mobile_dirs_text||'';
  $('cfgtoken').value = SETTINGS.openlist_token_set? MASK : '';
  $('cfgsk189').value = SETTINGS.tianyi_session_key_set? MASK : '';
  $('skstate').textContent = SETTINGS.tianyi_session_key_set? '(已设置)' : '(未设置)';
  $('cfgauth').value = SETTINGS.cas139_auth_set? MASK : '';
  $('cfguser').value=SETTINGS.cas139_user||'';
  $('cfgpass139').value = SETTINGS.cas139_pass_set? MASK : '';
  $('cfgcookies').value = SETTINGS.cas139_mail_cookies_set? MASK : '';
  $('cfgsms').value = SETTINGS.cas139_sms_code_set? MASK : '';
  $('passstate').textContent = SETTINGS.cas139_pass_set? '(已设置)' : '(未设置)';
  $('cookstate').textContent = SETTINGS.cas139_mail_cookies_set? '(已设置)' : '(未设置)';
  $('smsstate').textContent = SETTINGS.cas139_sms_code_set? '(已填,下次登录时用)' : '';
  $('cfgpw').value = SETTINGS.web_password_set? MASK : '';
  $('tokstate').textContent = SETTINGS.openlist_token_set? '(已设置'+(SETTINGS.openlist_token_source? ' · 来自'+SETTINGS.openlist_token_source : '')+')' : '(未设置)';
  $('authstate').textContent = SETTINGS.cas139_auth_set? '(已设置'+(SETTINGS.cas139_auth_expiry? ' · 授权到期 '+SETTINGS.cas139_auth_expiry : '')+')' : '(未设置)';
  loadList();
  $('pwstate').textContent = SETTINGS.web_password_set? '(已设置)' : '(未设置)';
}
async function saveSettings(){
  const body={tianyi_home:$('home189').value.trim(), tianyi_access:$('cfgaccess').value,
    tianyi_paths:$('paths189').value.split('\n').map(s=>s.trim()).filter(Boolean),
    mobile_dirs_text:$('dirs139').value,
    openlist_url:$('cfgurl').value.trim(), openlist_token:secVal('cfgtoken'),
    tianyi_session_key: secVal('cfgsk189'),
    clear_tianyi_session_key:$('cfgskclear').checked,
    cas139_phone:$('cfgphone').value.trim(), cas139_auth:secVal('cfgauth'),
    web_password:secValRaw('cfgpw'), clear_web_password:$('cfgpwclear').checked,
    clear_openlist_token:$('cfgtokclear').checked, clear_cas139_auth:$('cfgauthclear').checked,
    cas139_user:$('cfguser').value.trim(), cas139_pass:secVal('cfgpass139'),
    cas139_mail_cookies:secVal('cfgcookies'), cas139_sms_code:secVal('cfgsms'),
    clear_cas139_pass:$('cfgpassclear').checked, clear_cas139_mail_cookies:$('cfgcookclear').checked};
  const d = await api('/api/settings', body);
  $('setmsg').innerHTML = d.ok? '<span class=good>已保存</span>' : '<span class=err>'+esc(d.msg)+'</span>';
  if(d.ok){ $('cfgtoken').value=''; $('cfgauth').value=''; $('cfgpw').value=''; $('cfgpwclear').checked=false; $('cfgtokclear').checked=false; $('cfgsk189').value=''; $('cfgskclear').checked=false; $('cfgauthclear').checked=false; $('cfgpassclear').checked=false; $('cfgcookclear').checked=false; loadSettings(); status(); }
}
function checkedKinds(){
  const s=new Set();
  batchItems.forEach((it,i)=>{ const c=document.querySelector('.itchk[data-i="'+i+'"]');
    if(it.info && (c? c.checked : true)){ const cl=itemCloud(it); if(cl) s.add(cl); } });
  return s;
}
function casWarnFor(cloud){
  const s=checkedKinds(); if(!s.size) return '';
  if(cloud==='139' && !s.has('139') && !s.has('both')) return '所选 cas 为天翼版,仅可还原到天翼云盘';
  if(cloud==='189' && !s.has('189') && !s.has('both')) return '所选 cas 为移动版,仅可还原到移动云盘';
  return '';
}
function fillHome(which){
  const w = casWarnFor('189'); if(w){ alert('⚠ '+w); return; }
  const v = SETTINGS.tianyi_home;
  if(v){ $('target189').value=v; $('parent139').value=''; picked139Path=''; } else { alert('未设置挂载目录,请先在「设置」页配置'); }
}
function goHome(which){
  const v = SETTINGS.tianyi_home;
  if(v){ genRoot=v; curPath=v; loadList(); } else { alert('未设置挂载目录,请先在「设置」页配置'); }
}
let pickerCloud='189', p139Stack=[], p139Items=[], picked139Path='';
function tianyiRoots(){
  const roots=[];
  if(SETTINGS.tianyi_home) roots.push(SETTINGS.tianyi_home);
  for(const x of (SETTINGS.tianyi_paths||[])){ if(x && roots.indexOf(x)<0) roots.push(x); }
  if(roots.indexOf('/')>=0) return ['/'];
  return roots;
}
function pickerRoots139(){
  const roots=[];
  for(const d of (SETTINGS.mobile_dirs||[])) roots.push({id:d.id, name:d.name||d.id});
  if(!roots.length && SETTINGS.mobile_home_id)
    roots.push({id:SETTINGS.mobile_home_id, name:SETTINGS.mobile_home_name||'主目录'});
  return roots;
}
function togglePicker(targetId){
  if(targetId) pickerTarget=targetId;
  const p=$('picker'); const show=p.style.display==='none';
  if(show){
    const settingsCtx = !(pickerTarget==='target189' || pickerTarget==='scanpath');
    $('pickercloudtabs').style.display = settingsCtx? 'none':'';
    pickerScope=null;
    if(settingsCtx){
      pickerCloud='189';
      if(pickerTarget.indexOf('append:')!==0) pickerPath = ($(pickerTarget).value||'').trim() || '/';
    } else {
      const roots = tianyiRoots().map((p,i)=>({label: i===0? '☁️ 天翼云盘' : '☁️ 天翼目录', path:p}));
      pickerScope = roots.length? {roots:roots, base:''} : null;
      const isRestore = pickerTarget==='target189';
      const tval = (isRestore? ($('target189').value||'') : ($('scanpath').value||'')).trim();
      const pid = isRestore? ($('parent139').value||'').trim() : (scan139? scan139.id : '');
      if(pid){
        pickerCloud='139';
        let nm='';
        const d0=(SETTINGS.mobile_dirs||[]).find(d=>d.id===pid);
        if(d0) nm=d0.name||pid;
        else if(SETTINGS.mobile_home_id===pid) nm=SETTINGS.mobile_home_name||pid;
        else if(isRestore) nm=picked139Path||'已选目录';
        else nm=(scan139&&scan139.name)||'已选目录';
        p139Stack=[{id:pid, name:nm}];
      } else if(tval && !tval.startsWith('移动云盘:')){
        pickerCloud='189';
        if(pickerScope){
          const hit=pickerScope.roots.find(r=>tval===r.path || tval.startsWith(r.path+'/'));
          pickerScope.base = hit? hit.path : tval;
        }
        pickerPath=tval;
        p139Stack=[];
      } else {
        pickerCloud='189';
        if(pickerScope){
          if(pickerScope.roots.length===1){ pickerScope.base=pickerScope.roots[0].path; pickerPath=pickerScope.roots[0].path; }
          else pickerPath='';
        } else pickerPath='/';
        p139Stack=[];
      }
    }
    $('ph189').style.display = pickerScope? 'none':'';
    showPickerCloud(pickerCloud);
  }
  p.style.display=show?'flex':'none';
}
function showPickerCloud(c){
  const w=casWarnFor(c);
  const el=$(c==='139'? 'warn139':'warn189');
  if(el){ el.textContent = w? '⚠ '+w : ''; el.style.display = w? 'block' : 'none'; }
  pickerCloud=c;
  $('ptab189').classList.toggle('on', c==='189');
  $('ptab139').classList.toggle('on', c==='139');
  $('pane189').style.display = c==='189'? 'block':'none';
  $('pane139').style.display = c==='139'? 'block':'none';
  if(c==='189') loadPicker(); else load139Pane();
}
function pickerEnterRoot(i){ const r=pickerScope.roots[i]; pickerScope.base=r.path; pickerPath=r.path; $('pickerfilter').value=''; loadPicker(); }
function pickerToChoice(){ if(pickerScope && pickerScope.roots.length>1){ pickerScope.base=''; pickerPath=''; loadPicker(); } }
let pickerDirs=[];
async function loadPicker(){
  renderCrumbs();
  if(pickerScope && !pickerScope.base){
    $('pickerlist').innerHTML = pickerScope.roots.map((r,i)=>'<div class="row dirrow" onclick="pickerEnterRoot('+i+')"><span>📁</span><span class="grow">'+esc(r.label)+'<br><span class="mut">'+esc(r.path)+'</span></span></div>').join('');
    return;
  }
  $('pickerlist').innerHTML='<div class="mut" style="padding:8px 2px">加载中…</div>';
  const d = await api('/api/list?path='+encodeURIComponent(pickerPath));
  if(!d.ok){ $('pickerlist').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  pickerDirs = d.data.entries.filter(e=>e.is_dir);
  renderPicker();
}
function renderCrumbs(){
  if(pickerScope){
    if(!pickerScope.base){ $('crumbs').innerHTML='<span class="crumb">先选一个云盘挂载目录</span>'; return; }
    const base=pickerScope.base;
    const root=pickerScope.roots.find(r=>r.path===base);
    let h='<span class="crumb" onclick="pickerToChoice()">'+esc(root? root.label : '挂载目录')+'</span>';
    const baseParts = base==='/'? [] : base.split('/').filter(Boolean);
    const allParts = pickerPath.split('/').filter(Boolean);
    let acc = base==='/'? '' : base;
    for(let k=baseParts.length;k<allParts.length;k++){ acc+='/'+allParts[k]; h+=' <span class="mut">›</span> <span class="crumb" onclick="pickerGo(\''+esc(acc)+'\')">'+esc(allParts[k])+'</span>'; }
    $('crumbs').innerHTML=h;
    return;
  }
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
  }).join('') : '<div class="mut" style="padding:8px 2px">'+(q?'没有匹配的文件夹':'当前层级无子文件夹,可点「✔ 用当前目录」确认')+'</div>';
}
function pickerGo(p){
  if(pickerScope && pickerScope.base){
    const b=pickerScope.base;
    if(b!=='/' && !(p===b || p.startsWith(b+'/'))) p=b;
  }
  pickerPath=p; $('pickerfilter').value=''; loadPicker();
}
function pickerEnter(p){ pickerGo(p); }
function pickerUp(){
  if(pickerScope && pickerScope.base && pickerPath===pickerScope.base){ pickerToChoice(); return; }
  if(pickerPath && pickerPath!=='/') pickerGo(pickerPath.split('/').slice(0,-1).join('/')||'/');
}
function pickerHome(which){ const v=SETTINGS.tianyi_home; if(v){ pickerGo(v); } else alert('未设置挂载目录,请先在「设置」页配置'); }
function pickerPick(p){
  if(pickerTarget.indexOf('append:')===0){
    const ta=$(pickerTarget.slice(7));
    const lines=ta.value.split('\n').map(s=>s.trim()).filter(Boolean);
    if(lines.indexOf(p)<0) lines.push(p);
    ta.value=lines.join('\n');
  } else { $(pickerTarget).value=p; if(pickerTarget==='scanpath'){ scanSrc='ol'; scan139=null; } }
  $('picker').style.display='none';
}
function p139CurName(){ return p139Stack.map(c=>c.name).join('/'); }
async function load139Pane(){
  const box=$('p139list'), cr=$('p139crumbs');
  if(!p139Stack.length){
    cr.textContent='移动云盘目录';
    box.innerHTML = pickerRoots139().map((r,i)=>'<div class="row dirrow" onclick="p139EnterRoot('+i+')"><span>📁</span><span class="grow">'+esc(r.name)+'<br><span class="mut">'+esc(r.id)+'</span></span></div>').join('');
    return;
  }
  cr.textContent = p139CurName();
  box.innerHTML='<div class="mut" style="padding:8px 2px">加载中…</div>';
  const cur=p139Stack[p139Stack.length-1];
  const d = await api('/api/m139/list',{parent:cur.id});
  if(!d.ok){ box.innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  p139Items=d.data.items.filter(e=>e.is_dir);
  box.innerHTML = '<div class=row><button class="small grey" onclick="p139Up()">⬆ 上一级</button></div>'
    + (p139Items.map((e,i)=>'<div class="row dirrow" onclick="p139Down('+i+')"><span>📁</span><span class="grow">'+esc(e.name)+'</span></div>').join('') || '<div class="mut" style="padding:8px 2px">当前目录无子文件夹,可点「✔ 用当前目录」确认</div>');
}
function p139EnterRoot(i){ const r=pickerRoots139()[i]; p139Stack=[{id:r.id,name:r.name}]; load139Pane(); }
function p139Down(i){ const e=p139Items[i]; p139Stack.push({id:e.id,name:e.name}); load139Pane(); }
function p139Up(){ p139Stack.pop(); load139Pane(); }
async function p139SaveDir(){
  if(!p139Stack.length){ alert('请先进入一个目录'); return; }
  const cur=p139Stack[p139Stack.length-1];
  const dirs=(SETTINGS.mobile_dirs||[]).slice();
  if(dirs.some(d=>d.id===cur.id)){ alert('这个目录已在设置里'); return; }
  dirs.push({name:p139CurName(), id:cur.id});
  const d = await api('/api/settings',{mobile_dirs_text:dirs.map(x=>(x.name? x.name+' = ':'')+x.id).join('\n')});
  if(d.ok){ SETTINGS.mobile_dirs=dirs; const el=$('dirs139'); if(el) el.value=dirs.map(x=>(x.name? x.name+' = ':'')+x.id).join('\n'); alert('已存到设置「移动云盘目录」'); }
  else alert(d.msg);
}
async function mkdirHere(cloud){
  let name = prompt('新目录名');
  if(name===null) return;
  name = name.trim();
  if(!name || name.includes('/')){ alert('目录名不能为空,也不能带 /'); return; }
  if(cloud==='189'){
    if(pickerScope && !pickerScope.base){ alert('请先进入一个挂载目录'); return; }
    const p = (pickerPath==='/'?'':pickerPath)+'/'+name;
    const d = await api('/api/mkdir',{path:p});
    if(d.ok) loadPicker(); else alert(d.msg);
  } else {
    if(!p139Stack.length){ alert('先进入一个目录'); return; }
    const cur=p139Stack[p139Stack.length-1];
    const d = await api('/api/m139/mkdir',{parent:cur.id, name:name});
    if(d.ok) load139Pane(); else alert(d.msg);
  }
}
function pickerUse(){
  if(pickerCloud==='139'){
    if(!p139Stack.length){ alert('先进入一个目录'); return; }
    const cur=p139Stack[p139Stack.length-1];
    const nm=p139CurName();
    if(pickerTarget==='target189'){ $('parent139').value=cur.id; picked139Path=nm; $('target189').value='移动云盘:'+nm; }
    else if(pickerTarget==='scanpath'){ scan139={id:cur.id,name:nm}; scanSrc='139'; $('scanpath').value='移动云盘:'+nm; }
    $('picker').style.display='none';
    return;
  }
  if(pickerScope && !pickerScope.base){ alert('先进入一个挂载目录,再点「用当前目录」'); return; }
  pickerPick(pickerPath);
  if(pickerTarget==='target189'){ $('parent139').value=''; picked139Path=''; }
}
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
function kindsOf(items, sel){
  const s=new Set();
  items.forEach((it,i)=>{ if(it.info && sel(i)){ const c=itemCloud(it); if(c) s.add(c); } });
  return s;
}
function applyBtns(ty, mb, s){
  if(ty) ty.disabled = !(s.has('189')||s.has('both'));
  if(mb) mb.disabled = !(s.has('139')||s.has('both'));
}
function refreshBtns(){
  applyBtns($('btnTy'), $('btnMb'), kindsOf(batchItems, i=>{ const c=document.querySelector('.itchk[data-i="'+i+'"]'); return c? c.checked : true; }));
}
function itemCloud(it){
  const k=(it.info&&it.info.kind)||'';
  if(k.indexOf('both')===0) return 'both';
  if(k.indexOf('tianyi')===0) return '189';
  if(k.indexOf('mobile')===0) return '139';
  return '';
}
async function failReason189(it){
  // 天翼还原失败后,用网页版接口向 189 查具体原因;查不到返回空串
  const c=itemCloud(it);
  if(c!=='189' && c!=='both') return '';
  try{
    const d=await api('/api/check189',{cas_b64:it.b64});
    if(!d.ok) return '';
    if(d.data.state==='missing') return '文件不存在于天翼云端,无法秒传';
    if(d.data.state==='error') return '秒传被拒绝:'+d.data.msg;
    if(d.data.state==='ok') return '云端存在该文件,但本次秒传未成功';
  }catch(e){}
  return '';
}
async function restoreCore(to, items, setStatus){
  const url = to==='mobile'? '/api/restore139' : (to==='openlist'? '/api/restoreol' : '/api/restore189');
  let target = $('target189').value.trim();
  if(to!=='mobile' && target.startsWith('移动云盘:')) target = SETTINGS.tianyi_home || '/';
  const dirs139 = SETTINGS.mobile_dirs||[];
  const directHome139 = (to==='mobile' && !$('parent139').value.trim())? ((dirs139[0]&&dirs139[0].id) || SETTINGS.mobile_home_id || '') : '';
  if(to!=='mobile' && !target) target = SETTINGS.tianyi_home || '/';
  {
    const warns=[];
    for(const it of items){
      if(!it.info || it._skip) continue;
      const c=itemCloud(it);
      if(to==='mobile'){
        // 移动走直连选目录,不做 OpenList 路径校验
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
    const body = to==='mobile'? {cas_b64:it.b64,parent:$('parent139').value||directHome139||'',sidecar:!!$('sidecar').checked}
                              : {cas_b64:it.b64,cas_name:it.name,target:target};
    const d = await api(url,body);
    if(!d.ok){ let m2=d.msg; const why=await failReason189(it); if(why) m2=why; setStatus(i,'✗ '+m2,'err'); badN++; continue; }
    if(to==='mobile'){
      setStatus(i,'✓ '+(d.msg||'已还原'),'good'); okN++; continue;
    }
    await sleepMs(1500);
    const ex = await api('/api/exists',{path:target,name:it.info.name});
    if(ex.ok && ex.data.found){
      let note='✓ 已确认文件在 '+target;
      if($('sidecar').checked){
        const sc = await api('/api/sidecar',{dir:target,video_name:it.info.name,cas_b64:it.b64});
        note += sc.ok? '(CAS文件已保存至同目录)' : '(CAS文件保存至同目录失败: '+sc.msg+')';
      }
      setStatus(i,note,'good'); okN++;
    }
    else { const why=await failReason189(it); setStatus(i,'✗ '+(why||'文件不存在于云端,无法秒传'),'err'); badN++; }
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
let browseSrc='ol';
let m139Stack=[], m139Items=[];
function setBrowseSrc(s){
  browseSrc=s;
  $('olbrowse').style.display = s==='ol'? '' : 'none';
  $('m139browse').style.display = s==='139'? '' : 'none';
  if(s==='139') loadM139();
}
async function loadM139(){
  if(!m139Stack.length){
    $('m139crumbs').textContent='移动云盘目录';
    const roots=pickerRoots139();
    $('m139entries').innerHTML = roots.length? roots.map((r,i)=>'<div class=row><span class=grow>📁 '+esc(r.name)+'</span><button class="small grey" onclick="m139EnterRoot('+i+')">打开</button></div>').join('')
      : '<span class=mut>未配置目录(设置页「移动云盘目录」)</span>';
    return;
  }
  const cur=m139Stack[m139Stack.length-1];
  $('m139crumbs').textContent = m139Stack.map(c=>c.name).join(' / ');
  $('m139entries').innerHTML='<span class=mut>加载中...</span>';
  const d = await api('/api/m139/list',{parent:cur.id});
  if(!d.ok){ $('m139entries').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  m139Items=d.data.items;
  $('m139entries').innerHTML = m139Items.map((e,i)=> e.is_dir
    ? '<div class=row><span class=grow>📁 '+esc(e.name)+'</span><button class="small grey" onclick="m139Down('+i+')">打开</button></div>'
    : '<div class=row><span class=grow>📄 '+esc(e.name)+'<br><span class=mut>'+e.size+' 字节</span></span>'+(e.name.toLowerCase().endsWith('.cas')? '<span class=mut>CAS 文件</span>' : '<button class="small green" onclick="genCas139('+i+')">生成 CAS</button>')+'</div>'
  ).join('') || '<span class=mut>(空文件夹)</span>';
}
function m139EnterRoot(i){ const r=pickerRoots139()[i]; m139Stack=[{id:r.id,name:r.name}]; loadM139(); }
function m139Roots(){ m139Stack=[]; loadM139(); }
function m139Down(i){ const e=m139Items[i]; m139Stack.push({id:e.id,name:e.name}); loadM139(); }
function m139Up(i){ m139Stack=m139Stack.slice(0,i+1); loadM139(); }
async function genCas139(i){
  const e=m139Items[i];
  const cur=m139Stack[m139Stack.length-1];
  let up=$('upback139').checked, ow=false;
  if(up && m139Items.some(x=>!x.is_dir && x.name===e.name+'.cas')){
    if(confirm('源目录已有 '+e.name+'.cas,覆盖保存新 cas?\n确定=覆盖;取消=只生成、不上传回目录')) ow=true;
    else up=false;
  }
  const d = await api('/api/m139/gen',{file_id:e.id,name:e.name,size:e.size,
    parent:cur? cur.id : '/', upload_back:up, overwrite:ow,
    path:m139Stack.map(c=>c.name).join('/')+'/'+e.name});
  if(d.ok){ alert('已开始生成,进度见「任务」区'); loadJobs(); } else alert(d.msg);
}
function updateHome139Label(){
  const dirs=SETTINGS.mobile_dirs||[];
  const t = dirs.length? '移动默认目录:'+(dirs[0].name||dirs[0].id)
    : (SETTINGS.mobile_home_id? '移动默认目录:'+(SETTINGS.mobile_home_name||SETTINGS.mobile_home_id) : '移动默认目录:未配置(设置页「移动云盘目录」)');
  const el=$('home139label'); if(el) el.textContent=t;
}
function fillHome139(){
  const w = casWarnFor('139'); if(w){ alert('⚠ '+w); return; }
  const dirs=SETTINGS.mobile_dirs||[];
  const id=(dirs[0]&&dirs[0].id)||SETTINGS.mobile_home_id||'';
  if(!id){ alert('请先在「设置」页配置移动云盘目录'); return; }
  $('parent139').value=id;
  const nm=((dirs[0]&&dirs[0].name)||SETTINGS.mobile_home_name||id);
  picked139Path=nm;
  $('target189').value='移动云盘:'+nm;
}
function m139GoHome(){
  const dirs=SETTINGS.mobile_dirs||[];
  const id=(dirs[0]&&dirs[0].id)||SETTINGS.mobile_home_id;
  if(!id){ alert('请先在「设置」页配置移动云盘目录'); return; }
  const nm=((dirs[0]&&dirs[0].name)||SETTINGS.mobile_home_name||'目录').split('/').pop();
  m139Stack=[{id:id,name:nm}];
  loadM139();
}
let genRoot=null, curEntries=[];
async function loadList(){
  const roots=tianyiRoots();
  if(genRoot===null){
    if(roots.length===1){ genRoot=roots[0]; curPath=genRoot; }
    else {
      $('curpath').textContent='天翼挂载目录';
      $('entries').innerHTML = roots.length? roots.map((r,i)=>'<div class=row><span class=grow>📁 '+esc(r)+'</span><button class="small grey" onclick="genEnterRoot('+i+')">打开</button></div>').join('')
        : '<div class=mut>请先在「设置」页配置天翼挂载目录</div>';
      return;
    }
  }
  const d = await api('/api/list?path='+encodeURIComponent(curPath));
  if(!d.ok){ $('entries').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  curEntries = d.data.entries || [];
  $('curpath').textContent=curPath;
  $('entries').innerHTML = d.data.entries.map(e=>{
    const p = (curPath==='/'?'':curPath)+'/'+e.name;
    if(e.is_dir) return '<div class=row><span class=grow>📁 '+esc(e.name)+'</span><button class="small" onclick="enterDir(\''+esc(p)+'\')">打开</button></div>';
    return '<div class=row><span class=grow>📄 '+esc(e.name)+'<br><span class=mut>'+e.size+' 字节</span></span>'+(e.name.toLowerCase().endsWith('.cas')? '<span class=mut>CAS 文件</span>' : '<button class="small green" onclick="genCas(\''+esc(p)+'\','+e.size+')">生成 CAS</button>')+'</div>';
  }).join('') || '<div class=mut>空目录</div>';
}
function genEnterRoot(i){ genRoot=tianyiRoots()[i]; curPath=genRoot; loadList(); }
function enterDir(p){ curPath=p; loadList(); }
function goUp(){
  if(genRoot!==null && curPath===genRoot){ if(tianyiRoots().length>1){ genRoot=null; loadList(); } return; }
  if(curPath!=='/'){ curPath=curPath.split('/').slice(0,-1).join('/')||'/'; loadList(); }
}
async function genCas(p,size){
  let up=$('upback').checked;
  if(up){
    const nm=p.split('/').pop();
    if(curEntries.some(e=>!e.is_dir && e.name===nm+'.cas')
       && !confirm('源目录已有 '+nm+'.cas,覆盖保存新 cas?\n确定=覆盖;取消=只生成、不上传回目录')) up=false;
  }
  const d = await api('/api/gen',{path:p,size:size,upload_back:up});
  alert(d.ok? '已开始生成,进度见「任务」区' : d.msg);
  loadJobs();
}
const genResults={};
let jobRecords={}, checkedJobIds=new Set();
async function pushGenToRestore(jid, btn){
  const g=genResults[jid]; if(!g) return;
  if(!batchItems.some(b=>b.b64===g.b64)){
    const ins=await api('/api/inspect',{cas_b64:g.b64});
    batchItems.push({name:(g.name||'file')+'.cas', b64:g.b64, src:'生成区推送',
      info:ins.ok? ins.data : null, err:ins.ok? '' : ins.msg});
    renderBatch();
  }
  $('restorebox').style.display='block';
  refreshBtns();
  if(btn) btn.textContent='✓ 已推送到还原区';
  showTab('restore');
  window.scrollTo({top:0, behavior:'smooth'});
}
function jobsAll(v){ document.querySelectorAll('.jobchk').forEach(c=>{ c.checked=v; if(v) checkedJobIds.add(c.dataset.id); else checkedJobIds.delete(c.dataset.id); }); }
function jobCheckChanged(c){ if(c.checked) checkedJobIds.add(c.dataset.id); else checkedJobIds.delete(c.dataset.id); }
async function clearJobs(all){
  let ids=[];
  if(!all){
    ids=Array.from(checkedJobIds);
    if(!ids.length){ alert('请先勾选要清空的任务'); return; }
  }
  if(!confirm(all? '清空全部任务记录?(只清列表,不删除已生成的 cas 文件)' : '清空所选 '+ids.length+' 条任务记录?(只清列表,不删除已生成的 cas 文件)')) return;
  const d = await api('/api/jobs/clear', all? {all:true} : {ids:ids});
  if(d.ok){ if(all) checkedJobIds.clear(); else ids.forEach(id=>checkedJobIds.delete(id)); loadJobs(); } else alert(d.msg);
}
async function deleteJobSources(){
  const ids=Array.from(checkedJobIds);
  if(!ids.length){ alert('请先勾选要删除源文件的生成任务'); return; }
  const eligible=ids.filter(id=>{ const j=jobRecords[id]; return j && j.status==='done' && j.result && j.result.uploaded_to && !j.result.source_deleted; });
  if(!eligible.length){ alert('所选任务中没有可删除源文件的任务(须生成完成,且 .cas 已保存回原目录)'); return; }
  if(!confirm('将删除所选 '+eligible.length+' 个任务的源文件,仅保留 .cas。是否继续?')) return;
  $('jobsmsg').textContent='正在删除源文件…';
  const d=await api('/api/jobs/delete-source',{ids:eligible});
  if(!d.ok){ $('jobsmsg').innerHTML='<span class=err>'+esc(d.msg)+'</span>'; return; }
  const failed=(d.data.results||[]).filter(r=>!r.ok);
  $('jobsmsg').innerHTML='<span class='+(failed.length? 'err':'good')+'>源文件删除完成: 成功 '+d.data.deleted+' · 失败 '+failed.length+'</span>'
    +(failed.length? '<br>'+failed.map(r=>esc(r.msg)).join('<br>') : '');
  loadJobs();
}
async function loadJobs(){
  const d = await api('/api/jobs');
  if(!d.ok) return;
  if(!d.data.length){ jobRecords={}; checkedJobIds.clear(); $('jobs').innerHTML='暂无'; return; }
  jobRecords={};
  d.data.forEach(j=>{ jobRecords[j.id]=j; if(j.status==='done'&&j.result&&j.result.cas_b64) genResults[j.id]={name:j.result.name, b64:j.result.cas_b64}; });
  checkedJobIds=new Set(Array.from(checkedJobIds).filter(id=>jobRecords[id]));
  $('jobs').innerHTML =
    '<div class=row style="flex-wrap:wrap"><label><input type="checkbox" style="width:auto" onchange="jobsAll(this.checked)"> <b>全选</b></label><span class=grow></span><button class="small grey" onclick="deleteJobSources()">删除源文件</button><button class="small grey" onclick="clearJobs(false)">清空已选</button><button class="small grey" onclick="clearJobs(true)">全部清空</button></div>'
    + d.data.map(j=>
    '<div style="margin:8px 0"><div><label><input type="checkbox" class="jobchk" data-id="'+j.id+'" style="width:auto" onchange="jobCheckChanged(this)"'+(checkedJobIds.has(j.id)? ' checked':'')+'> '+esc(j.title)+'</label></div>'
    +(j.path? '<div class=kv>📁 '+esc(j.path)+'</div>':'')+
    '<div class=mut>'+esc(j.status)+(j.progress!=null? ' · '+j.progress+'%':'')+' · '+esc(j.message||'')+'</div>'+
    (j.progress!=null&&j.status==='running'? '<div class=bar><i style="width:'+j.progress+'%"></i></div>':'')+
    (j.status==='done'&&j.result? '<div class=good>生成完成'+(j.result.uploaded_to? ' · 已保存至 '+esc(j.result.uploaded_to):'')+(j.result.source_deleted? ' · 源文件已删除':'')+'</div>'+(j.result.cas_b64? '<div class=row style="margin-top:6px"><a download="'+esc((j.result.name||'file')+'.cas')+'" href="data:text/plain;base64,'+j.result.cas_b64+'"><button class="small green">⬇ 下载 .cas</button></a><button class="small" onclick="pushGenToRestore(\''+j.id+'\',this)">⬆ 推送到还原区</button></div>':''):'')+
    '</div>').join('');
}
status(); loadSettings(); loadList(); loadJobs(); refreshBtns();
setInterval(loadJobs, 2500);
</script></body></html>"""


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("cas-web v%s 监听 :%d  数据目录 %s  口令保护:%s"
          % (VERSION, PORT, DATA_DIR, "开" if password_required() else "关"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
