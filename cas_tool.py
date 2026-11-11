#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cas_tool.py — 天翼云盘 / 移动云盘(139) 双支持 .cas 小工具(纯标准库,无第三方依赖)

功能:
  inspect   解析 .cas,自动判断类型(天翼版 / 移动版 / 双填版 / 损坏)
  gen       对本地文件一遍流式计算,生成 .cas(--target tianyi|mobile|both)
  verify    用本地原文件校验一份 .cas 的哈希是否对得上
  restore   按 .cas 内容自动分流还原:
              - 移动版 -> 直调 139 新个人云 file/create(SHA256 秒传探测)
              - 天翼版 -> 走你已部署的 OpenList 种子接口 /api/fs/seed/rapid_upload
            restore-139 / restore-189 为手动指定分支的版本

.cas 统一格式(与网友流通的双云格式一致,Base64 编码的紧凑 JSON,字段顺序固定):
  {"name":..., "size":..., "md5":..., "sliceMd5":...,
   "sha256":..., "recover_pass":..., "create_time":...}
  天翼版填 md5/sliceMd5(大写),移动版填 sha256(小写),不用的那组留空字符串。
  gen --target both 会把两组都填上,得到一份两家解析器都能认的双填版。

天翼 sliceMd5 规则(据 OpenList 189pc 驱动 partSize / pkg/torrent.CASSliceSize 核准):
  分片大小由文件大小唯一决定: <=10MiB*999 用 10MiB; <=10MiB*2*999 用 20MiB;
  更大时按 1999 片上限向上取 10MiB 整数倍(最小 50MiB)。
  sliceMd5: 只有 1 片时 = 该片 MD5(=整文件 MD5);
            多片时 = MD5(逐片 MD5 大写后以 "\\n" 拼接) 的十六进制大写。

移动 139 秒传(据新个人云接口实测脚本实现):
  POST https://personal-kd-njs.yun.139.com/hcy/file/create
  body 带 contentHash=<sha256>、contentHashAlgorithm="SHA256";
  服务端池里有该哈希时 create 直接返回已存在(fileId 且无待上传分片),
  没有时返回带 uploadUrl 的分片计划 —— 本工具只做秒传,不上传本体,
  未命中会明确报告,不会假装成功。
  请求需 Authorization(Basic)与 Mcloud-Sign 签名头,算法见 mcloud_sign()。

凭据只从环境变量读,不会写进任何文件或输出:
  移动: CAS139_PHONE + CAS139_AUTH(或兼容青龙脚本的 YDYP_PHONE + YDYP_AUTH)
        AUTH 获取: 浏览器登录 yun.139.com -> F12 网络 -> 任一请求头里的
        Authorization(形如 Basic xxxxx,填整个值或只填 xxxxx 部分均可)
  天翼(经 OpenList): OPENLIST_URL(如 http://192.168.111.x:5244)
                      OPENLIST_TOKEN(OpenList 后台 设置->其他/令牌)
"""

import argparse
import base64
import hashlib
import json
import math
import mimetypes
import os
import random
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

VERSION = "1.0.0"
MIB = 1024 * 1024

# ---------------------------------------------------------------------------
# .cas 基础: 编解码 / 类型判断
# ---------------------------------------------------------------------------

CAS_KEYS = ["name", "size", "md5", "sliceMd5", "sha256", "recover_pass", "create_time"]


def decode_cas_bytes(raw: bytes) -> dict:
    """把 .cas 文件内容(Base64 JSON,或明文 JSON)解成 dict。"""
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        return json.loads(text)
    compact = "".join(text.split())
    dec = base64.b64decode(compact + "=" * (-len(compact) % 4))
    return json.loads(dec.decode("utf-8"))


def encode_cas(obj: dict) -> bytes:
    """按流通格式编码: 紧凑 JSON(不转义中文) -> Base64,无换行。"""
    ordered = {k: obj.get(k, "") for k in CAS_KEYS}
    text = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(text.encode("utf-8"))


def load_cas(path: str) -> dict:
    with open(path, "rb") as f:
        return decode_cas_bytes(f.read())


def cas_kind(obj: dict) -> str:
    """根据哪组哈希被填了判断类型。"""
    has_md5 = bool(str(obj.get("md5", "")).strip())
    has_slice = bool(str(obj.get("sliceMd5", "")).strip())
    has_sha = bool(str(obj.get("sha256", "")).strip())
    if has_md5 and has_sha:
        return "both(双填版:天翼/移动都能认)"
    if has_md5 and has_slice:
        return "tianyi(天翼版)"
    if has_md5 and not has_slice:
        return "tianyi?(只有 md5,大文件缺 sliceMd5,天翼还原可能失败)"
    if has_sha:
        return "mobile(移动版)"
    return "broken(两组哈希都是空的,无法还原)"


def fmt_size(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1024 ** 3:
        return "%d 字节 (%.2f GiB)" % (n, n / 1024 ** 3)
    if n >= 1024 ** 2:
        return "%d 字节 (%.2f MiB)" % (n, n / 1024 ** 2)
    return "%d 字节" % n


def fmt_time(v) -> str:
    try:
        ts = int(v)
    except (TypeError, ValueError):
        return str(v)
    tz = timezone(timedelta(hours=8))
    return "%s (北京时间 %s)" % (
        ts, datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S"))


def print_cas_info(obj: dict, path: str = ""):
    if isinstance(obj.get("files"), list):
        print("这是多文件 .cas(files 数组),共 %d 个条目:" % len(obj["files"]))
        for i, entry in enumerate(obj["files"]):
            print("--- [%d] ---" % i)
            print_cas_info(entry)
        return
    print("文件: %s" % (path or obj.get("name", "")))
    print("类型: %s" % cas_kind(obj))
    print("  name        : %s" % obj.get("name", ""))
    print("  size        : %s" % fmt_size(obj.get("size", "")))
    print("  md5         : %s" % (obj.get("md5") or "(空)"))
    print("  sliceMd5    : %s" % (obj.get("sliceMd5") or "(空)"))
    print("  sha256      : %s" % (obj.get("sha256") or "(空)"))
    print("  recover_pass: %s" % ("(空)" if not obj.get("recover_pass") else "(已设置,已隐藏)"))
    print("  create_time : %s" % fmt_time(obj.get("create_time", "")))
    extra = [k for k in obj.keys() if k not in CAS_KEYS]
    if extra:
        print("  额外字段(本工具忽略): %s" % ", ".join(extra))
    # 基本合法性检查
    sha = str(obj.get("sha256", ""))
    if sha and not (len(sha) == 64 and all(c in "0123456789abcdefABCDEF" for c in sha)):
        print("  ⚠ sha256 不是 64 位十六进制,可能已损坏")
    md5v = str(obj.get("md5", ""))
    if md5v and not (len(md5v) == 32 and all(c in "0123456789abcdefABCDEF" for c in md5v)):
        print("  ⚠ md5 不是 32 位十六进制,可能已损坏")


# ---------------------------------------------------------------------------
# 哈希计算(一遍流式,同时算整文件 MD5 / SHA256 与天翼逐片 MD5)
# ---------------------------------------------------------------------------

def cas_slice_size(size: int) -> int:
    """天翼 CAS 分片大小:由文件大小唯一决定(与 189pc 驱动 partSize 一致)。"""
    default = 10 * MIB
    if size > default * 2 * 999:
        return max(math.ceil((size / 1999) / default), 5) * default
    if size > default * 999:
        return default * 2
    return default


def slice_md5_from_pieces(pieces_upper, file_md5_upper: str) -> str:
    if not pieces_upper:
        return file_md5_upper
    if len(pieces_upper) == 1:
        return pieces_upper[0]
    joined = "\n".join(pieces_upper)
    return hashlib.md5(joined.encode("utf-8")).hexdigest().upper()


def hash_file(path: str, need_md5: bool, need_sha256: bool, progress: bool = True):
    """返回 (size, md5_upper, slice_md5_upper, sha256_lower, pieces_upper)。"""
    size = os.path.getsize(path)
    slice_size = cas_slice_size(size)
    whole_md5 = hashlib.md5()
    whole_sha = hashlib.sha256()
    pieces = []
    done = 0
    last_report = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(slice_size if need_md5 else 8 * MIB)
            if not chunk:
                break
            if need_md5:
                pieces.append(hashlib.md5(chunk).hexdigest().upper())
                whole_md5.update(chunk)
            if need_sha256:
                whole_sha.update(chunk)
            done += len(chunk)
            if progress and size >= 512 * MIB and done - last_report >= 512 * MIB:
                last_report = done
                print("  ...已读 %.1f%%" % (done * 100 / size), file=sys.stderr)
    file_md5 = whole_md5.hexdigest().upper() if need_md5 else ""
    return {
        "size": size,
        "md5": file_md5,
        "sliceMd5": slice_md5_from_pieces(pieces, file_md5) if need_md5 else "",
        "sha256": whole_sha.hexdigest() if need_sha256 else "",
        "pieces": pieces,
        "slice_size": slice_size,
    }


# ---------------------------------------------------------------------------
# 子命令: inspect / gen / verify
# ---------------------------------------------------------------------------

def cmd_inspect(args):
    obj = load_cas(args.cas)
    print_cas_info(obj, args.cas)
    return 0


def cmd_gen(args):
    target = args.target
    need_md5 = target in ("tianyi", "both")
    need_sha = target in ("mobile", "both")
    print("正在计算哈希(文件越大越久,大文件只读一遍)...", file=sys.stderr)
    h = hash_file(args.file, need_md5, need_sha)
    name = os.path.basename(args.file)
    obj = {
        "name": name,
        "size": h["size"],
        "md5": h["md5"] if need_md5 else "",
        "sliceMd5": h["sliceMd5"] if need_md5 else "",
        "sha256": h["sha256"] if need_sha else "",
        "recover_pass": args.recover_pass or "",
        "create_time": str(int(time.time())),
    }
    out = args.out
    if not out:
        out = os.path.join(os.path.dirname(os.path.abspath(args.file)), name + ".cas")
    with open(out, "wb") as f:
        f.write(encode_cas(obj))
    print("已生成: %s" % out)
    print_cas_info(obj, out)
    if need_md5:
        print("  天翼分片: %d MiB/片,共 %d 片" % (h["slice_size"] // MIB, len(h["pieces"])))
    return 0


def cmd_verify(args):
    obj = load_cas(args.cas)
    if isinstance(obj.get("files"), list):
        print("多文件 .cas 暂不支持 verify,请拆成单文件后逐个校验")
        return 2
    need_md5 = bool(str(obj.get("md5", "")).strip())
    need_sha = bool(str(obj.get("sha256", "")).strip())
    if not need_md5 and not need_sha:
        print("这份 .cas 两组哈希都是空的,没有可校验的内容")
        return 2
    h = hash_file(args.file, need_md5, need_sha)
    ok = True
    if h["size"] != int(obj.get("size", -1)):
        print("✗ size 不符: cas=%s 本地=%s" % (obj.get("size"), h["size"]))
        ok = False
    else:
        print("✓ size 一致: %d" % h["size"])
    if need_md5:
        if h["md5"] == str(obj["md5"]).upper():
            print("✓ md5 一致")
        else:
            print("✗ md5 不符: cas=%s 本地=%s" % (obj["md5"], h["md5"]))
            ok = False
        cas_slice = str(obj.get("sliceMd5", "")).upper()
        if cas_slice:
            if h["sliceMd5"] == cas_slice:
                print("✓ sliceMd5 一致(分片 %d MiB)" % (h["slice_size"] // MIB))
            else:
                print("✗ sliceMd5 不符: cas=%s 本地=%s" % (cas_slice, h["sliceMd5"]))
                ok = False
    if need_sha:
        if h["sha256"] == str(obj["sha256"]).lower():
            print("✓ sha256 一致")
        else:
            print("✗ sha256 不符: cas=%s 本地=%s" % (obj["sha256"], h["sha256"]))
            ok = False
    print("结论: %s" % ("本地文件与这份 .cas 是同一个文件" if ok else "对不上,不是同一个文件(或 cas 已损坏)"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 移动 139 还原: 直调新个人云 file/create(SHA256 秒传探测)
# 实现要点移植自公开实测脚本 deeplee/personal-scripts 的 139_yunduo.py
# ---------------------------------------------------------------------------

PERSONAL_HOST = "https://personal-kd-njs.yun.139.com/hcy"
FILE_CREATE_URL = PERSONAL_HOST + "/file/create"
FILE_LIST_URL = PERSONAL_HOST + "/file/list"
FILE_DOWNLOAD_URL = PERSONAL_HOST + "/file/getDownloadUrl"
RECYCLE_TRASH_URL = PERSONAL_HOST + "/recyclebin/batchTrash"
X_DEVICE_INFO = "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||"
X_YUN_CLIENT_INFO = "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||"
UA_139 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _esc_js(s: str) -> str:
    """encodeURIComponent 等价实现(保留 ! ' ( ) *)。"""
    s = urllib.parse.quote(s, safe="")
    return (s.replace("%21", "!").replace("%27", "'")
             .replace("%28", "(").replace("%29", ")").replace("%2A", "*"))


def mcloud_sign(body_str: str) -> str:
    """Mcloud-Sign = "ts,rand,sign"
    sign = MD5( MD5( base64( sort-chars( encodeURIComponent(body) ) ) )
                + MD5(ts + ":" + rand) ) 大写"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    rand = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
    sorted_eb = "".join(sorted(_esc_js(body_str)))
    b64 = base64.b64encode(sorted_eb.encode("utf-8")).decode("ascii")
    h1 = hashlib.md5(b64.encode("utf-8")).hexdigest()
    h2 = hashlib.md5((ts + ":" + rand).encode("utf-8")).hexdigest()
    sign = hashlib.md5((h1 + h2).encode("utf-8")).hexdigest().upper()
    return "%s,%s,%s" % (ts, rand, sign)


def build_basic_auth(phone: str, auth_token: str) -> str:
    """把用户给的 token 规范成 'Basic <base64(pc:手机号:token)>'。
    若 token 本身已是 base64 后的完整值(解开以 pc: 开头),原样使用。"""
    t = auth_token.strip().strip("'").strip('"')
    if t.lower().startswith("basic "):
        t = t[6:].strip()
    try:
        dec = base64.b64decode(t + "=" * (-len(t) % 4)).decode("utf-8", "replace")
        if dec.startswith("pc:"):
            return "Basic " + t
    except Exception:
        pass
    wrapped = base64.b64encode(("pc:%s:%s" % (phone, t)).encode("utf-8")).decode("ascii")
    return "Basic " + wrapped


def personal_headers(basic: str, body_str: str) -> dict:
    return {
        "Accept": "application/json,text/plain,*/*",
        "Authorization": basic,
        "Caller": "web",
        "Cms-Device": "default",
        "Mcloud-Channel": "1000101",
        "Mcloud-Client": "10701",
        "Mcloud-Route": "001",
        "Mcloud-Sign": mcloud_sign(body_str),
        "Mcloud-Version": "7.14.0",
        "Origin": "https://yun.139.com/",
        "Referer": "https://yun.139.com/",
        "User-Agent": UA_139,
        "x-DeviceInfo": X_DEVICE_INFO,
        "x-huawei-channelSrc": "10000034",
        "x-inner-ntwk": "2",
        "x-m4c-caller": "PC",
        "x-m4c-src": "10002",
        "x-SvcType": "1",
        "X-Yun-Api-Version": "v1",
        "X-Yun-App-Channel": "10000034",
        "X-Yun-Channel-Source": "10000034",
        "X-Yun-Client-Info": X_YUN_CLIENT_INFO,
        "X-Yun-Module-Type": "100",
        "X-Yun-Svc-Type": "1",
        "Content-Type": "application/json",
    }


def personal_post(basic: str, url: str, payload: dict):
    body_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    req = urllib.request.Request(
        url, data=body_str.encode("utf-8"), method="POST",
        headers=personal_headers(basic, body_str))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def personal_call(basic: str, url: str, payload: dict):
    """调一个 hcy 接口并校验业务码,返回 data(形状同 OpenList 139 驱动)。"""
    st, txt = personal_post(basic, url, payload)
    if st != 200:
        raise RuntimeError("139 接口 HTTP %s: %s" % (st, txt[:200]))
    try:
        resp = json.loads(txt)
    except ValueError:
        raise RuntimeError("139 接口返回非 JSON: %s" % txt[:200])
    if str(resp.get("code")) not in ("0000", "0"):
        raise RuntimeError("139 接口失败 code=%s: %s"
                           % (resp.get("code"), resp.get("message") or txt[:200]))
    return resp.get("data") or {}


def list_139_files(basic: str, parent: str = "/"):
    """列一个 139 文件夹的全部条目(游标翻页),返回 [{id, name, size, is_dir}]。"""
    out = []
    cursor = ""
    while True:
        data = personal_call(basic, FILE_LIST_URL, {
            "imageThumbnailStyleList": ["Small", "Large"],
            "orderBy": "updated_at", "orderDirection": "DESC",
            "pageInfo": {"pageCursor": cursor, "pageSize": 100},
            "parentFileId": parent or "/"})
        for it in data.get("items") or data.get("list") or []:
            out.append({"id": it.get("fileId") or it.get("file_id") or "",
                        "name": it.get("name") or "",
                        "size": it.get("size") or 0,
                        "is_dir": it.get("type") == "folder"})
        cursor = data.get("nextPageCursor") or ""
        if not cursor:
            return out


def download_url_139(basic: str, file_id: str):
    """取 139 文件下载直链(cdnUrl 优先,同 OpenList 驱动的取法)。"""
    data = personal_call(basic, FILE_DOWNLOAD_URL, {"fileId": file_id})
    cdn = data.get("cdnUrl") or ""
    if cdn and data.get("cdnSwitch"):
        return cdn
    url = data.get("url") or ""
    if not url:
        raise RuntimeError("139 未返回下载链接")
    return url


def build_139_part_infos(size: int, part_size: int = 100 * MIB):
    """秒传探测用的分片计划: 100MiB 一片(命中与否只看 contentHash,
    分片计划仅在未命中、真要上传时才有意义;本工具不上传本体)。"""
    if size <= 0:
        return [{"partNumber": 1, "partSize": 0}]
    parts = []
    remain = size
    num = 1
    while remain > 0:
        cur = min(part_size, remain)
        parts.append({"partNumber": num, "partSize": cur})
        remain -= cur
        num += 1
    return parts


def get_139_credentials():
    phone = os.environ.get("CAS139_PHONE") or os.environ.get("YDYP_PHONE") or ""
    auth = os.environ.get("CAS139_AUTH") or os.environ.get("YDYP_AUTH") or ""
    return phone.strip(), auth.strip()


def cmd_restore_139(args):
    obj = load_cas(args.cas)
    if isinstance(obj.get("files"), list):
        print("多文件 .cas 请先拆分;本工具一次还原一个文件")
        return 2
    sha = str(obj.get("sha256", "")).strip().lower()
    if not sha:
        print("这份 .cas 没有 sha256,是天翼版,对移动云盘无效。请改用 restore-189。")
        return 2
    name = obj.get("name", "")
    size = int(obj.get("size", 0))
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    payload = {
        "contentHash": sha,
        "contentHashAlgorithm": "SHA256",
        "contentType": ctype,
        "parallelUpload": False,
        "partInfos": build_139_part_infos(size),
        "size": size,
        "parentFileId": args.parent,
        "name": name,
        "type": "file",
        "fileRenameMode": "auto_rename",
    }
    if args.dry_run:
        print("== dry-run:将向 %s 发送 ==" % FILE_CREATE_URL)
        shown = dict(payload)
        shown["partInfos"] = shown["partInfos"][:3] + (
            ["...(共 %d 片)" % len(payload["partInfos"])] if len(payload["partInfos"]) > 3 else [])
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        print("请求头含 Authorization(已隐藏)与 Mcloud-Sign(发送时按 body 现场计算)")
        return 0
    phone, auth = get_139_credentials()
    if not phone or not auth:
        print("缺少移动云盘凭据。请先设置环境变量:")
        print("  CAS139_PHONE=你的手机号 CAS139_AUTH='Basic xxxxx'")
        print("AUTH 获取: 浏览器登录 yun.139.com -> F12 -> 网络 -> 点任一请求 -> 请求头 Authorization")
        return 2
    basic = build_basic_auth(phone, auth)
    st, txt = personal_post(basic, FILE_CREATE_URL, payload)
    if st != 200:
        print("file/create HTTP %s: %s" % (st, txt[:300]))
        return 1
    try:
        resp = json.loads(txt)
    except ValueError:
        print("file/create 返回非 JSON: %s" % txt[:300])
        return 1
    if str(resp.get("code")) not in ("0000", "0"):
        msg = resp.get("message") or resp.get("msg") or ""
        print("file/create 失败: code=%s %s" % (resp.get("code"), msg))
        print("原始返回: %s" % txt[:500])
        return 1
    data = resp.get("data") or {}
    file_id = data.get("fileId")
    upload_id = data.get("uploadId")
    ret_parts = data.get("partInfos") or []
    has_upload_url = any(p.get("uploadUrl") for p in ret_parts if isinstance(p, dict))
    print("file/create 成功响应: fileId=%s" % file_id)
    if data.get("exist") is True or (file_id and not has_upload_url):
        print("✓ 秒传命中!《%s》已还原到移动云盘(父目录 %s)" % (name, args.parent))
        return 0
    # 未命中: 服务端没有这个哈希的现成文件,本工具到此为止(不上传本体)
    print("✗ 未命中: 移动云盘服务端池里没有 sha256=%s... 的文件" % sha[:16])
    print("  这份 .cas 本身没问题,只是云端没有可秒传的源;需要有原文件的人先真上传一次。")
    if args.cleanup_on_miss and file_id:
        st2, txt2 = personal_post(basic, RECYCLE_TRASH_URL, {"fileIds": [file_id]})
        print("  已清理未完成占位(fileId=%s): HTTP %s" % (file_id, st2))
    else:
        print("  提示: 如网盘里出现未完成占位文件,手动删除即可,或加 --cleanup-on-miss 让工具清理。")
    return 1


# ---------------------------------------------------------------------------
# 天翼还原: 经 OpenList 种子接口 /api/fs/seed/rapid_upload
# (用户已部署 OpenList 且挂载了 189pc;凭据走 OPENLIST_URL / OPENLIST_TOKEN)
# ---------------------------------------------------------------------------

def openlist_post(base: str, token: str, route: str, payload: dict):
    url = base.rstrip("/") + "/api" + route
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": token,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def cmd_restore_189(args):
    obj = load_cas(args.cas)
    if not str(obj.get("md5", "")).strip():
        print("这份 .cas 没有 md5,是移动版,对天翼云盘无效。请改用 restore-139。")
        return 2
    if not str(obj.get("sliceMd5", "")).strip() and int(obj.get("size", 0)) > 10 * MIB:
        print("⚠ 这份 .cas 只有 md5、没有 sliceMd5,且文件 >10MiB,天翼 CAS 秒传大概率失败")
    with open(args.cas, "rb") as f:
        seed_data = base64.b64encode(f.read()).decode("ascii")
    payload = {
        "seed_data": seed_data,
        "file_name": os.path.basename(args.cas),
        "path": args.target,
        "selected_files": [0],
    }
    base = os.environ.get("OPENLIST_URL", "").strip()
    token = os.environ.get("OPENLIST_TOKEN", "").strip()
    if args.dry_run:
        shown = dict(payload)
        shown["seed_data"] = seed_data[:40] + "...(Base64,共 %d 字符)" % len(seed_data)
        print("== dry-run:将依次调用 ==")
        print("POST %s/api/fs/seed/capabilities (预检)" % (base or "<OPENLIST_URL>"))
        print("POST %s/api/fs/seed/rapid_upload (秒传保存)" % (base or "<OPENLIST_URL>"))
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        print("请求头 Authorization: <OPENLIST_TOKEN,已隐藏>")
        return 0
    if not base or not token:
        print("缺少 OpenList 凭据。请先设置环境变量:")
        print("  OPENLIST_URL=http://你的OpenList地址:5244 OPENLIST_TOKEN=你的令牌")
        print("令牌在 OpenList 后台: 设置 -> 其他 -> 令牌(或个人资料页)")
        return 2
    # 1) 预检: 看目标路径的驱动能不能用这份种子的哈希秒传
    st, txt = openlist_post(base, token, "/fs/seed/capabilities", payload)
    if st == 200:
        try:
            cap = json.loads(txt)
            print("预检(capabilities): %s" % json.dumps(cap.get("data", cap), ensure_ascii=False)[:800])
        except ValueError:
            print("预检返回非 JSON: %s" % txt[:300])
    else:
        print("预检请求 HTTP %s(继续尝试直接秒传): %s" % (st, txt[:200]))
    # 2) 秒传保存
    st, txt = openlist_post(base, token, "/fs/seed/rapid_upload", payload)
    print("rapid_upload HTTP %s" % st)
    try:
        resp = json.loads(txt)
    except ValueError:
        print("返回非 JSON: %s" % txt[:500])
        return 1
    if resp.get("code") == 200:
        print("✓ 已提交秒传保存到 %s" % args.target)
        print("  返回: %s" % json.dumps(resp.get("data"), ensure_ascii=False)[:800])
        print("  请到 OpenList / 天翼云盘里确认《%s》已落地" % obj.get("name", ""))
        return 0
    print("✗ 秒传保存失败: %s" % (resp.get("message") or txt[:500]))
    print("  已知情况: 若提示 target driver cannot reuse the available hashes,")
    print("  多半是这份 .cas 的分片信息不足以过 OpenList 主线预检(旧格式常见),")
    print("  这类文件你已验证可用油猴脚本在 189 网页版还原 —— 本工具不重复诊断这一点。")
    return 1


def cmd_restore(args):
    obj = load_cas(args.cas)
    kind = cas_kind(obj)
    print("识别为: %s" % kind)
    has_md5 = bool(str(obj.get("md5", "")).strip())
    has_sha = bool(str(obj.get("sha256", "")).strip())
    if args.to == "mobile" or (has_sha and not has_md5):
        if not has_sha:
            print("指定了 mobile 但这份 .cas 没有 sha256")
            return 2
        args.parent = args.parent or "/"
        return cmd_restore_139(args)
    if args.to == "tianyi" or (has_md5 and not has_sha):
        if not has_md5:
            print("指定了 tianyi 但这份 .cas 没有 md5")
            return 2
        if not args.target:
            print("天翼还原需要 --target 指定 OpenList 里天翼挂载的目标目录,如 --target /天翼云盘/电影")
            return 2
        return cmd_restore_189(args)
    if has_md5 and has_sha:
        print("双填版: 请用 --to mobile 或 --to tianyi 指定还原到哪家")
        return 2
    print("这份 .cas 两组哈希都是空的,无法还原")
    return 2


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="cas_tool.py",
        description="天翼 / 移动(139)云盘双支持 .cas 工具: 生成、解析、校验、还原")
    p.add_argument("--version", action="version", version="cas_tool " + VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("inspect", help="解析一份 .cas 并判断类型")
    sp.add_argument("cas")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("gen", help="对本地文件生成 .cas")
    sp.add_argument("file")
    sp.add_argument("--target", choices=["tianyi", "mobile", "both"], default="both",
                    help="tianyi=只填 md5/sliceMd5; mobile=只填 sha256; both=都填(默认)")
    sp.add_argument("--out", help="输出路径(默认与原文件同目录、<原名>.cas)")
    sp.add_argument("--recover-pass", default="", help="还原口令(可空,写入 recover_pass 字段)")
    sp.set_defaults(func=cmd_gen)

    sp = sub.add_parser("verify", help="用本地原文件校验一份 .cas")
    sp.add_argument("cas")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("restore", help="按内容自动分流还原(双填版需 --to 指定)")
    sp.add_argument("cas")
    sp.add_argument("--to", choices=["tianyi", "mobile"], help="双填版时指定去向")
    sp.add_argument("--target", help="天翼: OpenList 里天翼挂载的目标目录")
    sp.add_argument("--parent", default="/", help="移动: 父目录 fileId(默认 / 为根目录)")
    sp.add_argument("--dry-run", action="store_true", help="只打印将发送的请求,不真发")
    sp.add_argument("--cleanup-on-miss", action="store_true", help="移动未命中时清理占位")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("restore-139", help="还原移动版 .cas 到移动云盘")
    sp.add_argument("cas")
    sp.add_argument("--parent", default="/", help="父目录 fileId(默认 / 为根目录)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--cleanup-on-miss", action="store_true")
    sp.set_defaults(func=cmd_restore_139)

    sp = sub.add_parser("restore-189", help="还原天翼版 .cas(经 OpenList 种子秒传保存)")
    sp.add_argument("cas")
    sp.add_argument("--target", required=True, help="OpenList 里天翼挂载的目标目录,如 /天翼云盘/电影")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_restore_189)

    return p


def main():
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print("文件不存在: %s" % e.filename)
        return 2
    except (ValueError, json.JSONDecodeError) as e:
        print("解析失败: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# 139 密码登录回退 / 快速登录 / 授权刷新
# 移植自 OpenList 139 驱动(drivers/139/util.go)的实现:
#   step1 邮箱密码登录(mail.10086.cn/Login/Login.ashx,密码 sha1("fetion.com.cn:"+pw))
#   step2 用 sid + RMKEY 换 artifact(smsrebuild1.mail.10086.cn/setting/s)
#   step3 thirdlogin(user-njs.yun.139.com)双层 AES 加密换 authToken
# 授权过期前 15 天内走 authTokenRefresh.do 刷新;刷新失败回退密码登录。
# AES 为纯 Python 实现(登录载荷很小,性能无关),已用 NIST 向量在测试里校验。
# ---------------------------------------------------------------------------

MAIL_LOGIN_URL = "https://mail.10086.cn/Login/Login.ashx"
MAIL_SMS_URL = "https://mail.10086.cn/s"
ARTIFACT_URL = "https://smsrebuild1.mail.10086.cn/setting/s"
THIRDLOGIN_URL = "https://user-njs.yun.139.com/user/thirdlogin"
REFRESH_URL = "https://aas.caiyun.feixin.10086.cn:443/tellin/authTokenRefresh.do"
_KEY1 = bytes.fromhex("73634235495062495331515373756c734e7253306c673d3d")  # 24B
_KEY2 = bytes.fromhex("7150714477323633586746674c337538")                  # 16B
MAIL_PUBKEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnsOHTFtwW5rq/8gGhPlM5Z3RP"
    "deN/d+FYIHb5JmcfBOCozXuT8c+0anvxtkjzghixwNlnmBuhN8OYfS789YuH/qReQbHC"
    "7OdlisLildNWPHRNUYcPa0W3lXSG3+81CXK7FDXPvXo5ubw2GqVbIsccMarI1dyfXdi"
    "4ITiCXvmM9wYBdUs9yXtoorhlpyYUI2GV8HNsQjWK9P5QZHT3ox5Qy+mjRmvv6RUFJLP"
    "OMkOS/pGZ0DwC1ypFZBxstW0/ftVupdOmGWvW7J2/e3dq3A/UvIkC4YUY/diL1wighJx"
    "1G9MiRROISjNMvNyUDSTqPJy516+l3sgHEbc067QIJx2NQIDAQAB")


# ---- 纯 Python AES(S-box 由 GF(2^8) 求逆+仿射变换生成) ----
def _gf_mul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _gf_inv(a):
    if a == 0:
        return 0
    r, base, e = 1, a, 254
    while e:
        if e & 1:
            r = _gf_mul(r, base)
        base = _gf_mul(base, base)
        e >>= 1
    return r


def _rotl8(x, n):
    return ((x << n) | (x >> (8 - n))) & 0xFF


_SBOX = []
for _i in range(256):
    _inv = _gf_inv(_i)
    _SBOX.append(_inv ^ _rotl8(_inv, 1) ^ _rotl8(_inv, 2) ^ _rotl8(_inv, 3)
                 ^ _rotl8(_inv, 4) ^ 0x63)
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = [0x01]
for _i in range(13):
    _RCON.append(_gf_mul(_RCON[-1], 2))


def _aes_expand(key):
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = [_SBOX[t[1]] ^ _RCON[i // nk - 1], _SBOX[t[2]],
                 _SBOX[t[3]], _SBOX[t[0]]]
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[x] for x in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w, nr


def _aes_block(block, w, nr, decrypt=False):
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_rk(rnd):
        for c in range(4):
            for r in range(4):
                s[r][c] ^= w[rnd * 4 + c][r]

    def sub(inv=False):
        box = _INV_SBOX if inv else _SBOX
        for r in range(4):
            for c in range(4):
                s[r][c] = box[s[r][c]]

    def shift(inv=False):
        for r in range(1, 4):
            row = [s[r][c] for c in range(4)]
            for c in range(4):
                s[r][c] = row[(c - r) % 4] if inv else row[(c + r) % 4]

    def mixcol():
        for c in range(4):
            a = [s[r][c] for r in range(4)]
            s[0][c] = _gf_mul(a[0], 2) ^ _gf_mul(a[1], 3) ^ a[2] ^ a[3]
            s[1][c] = a[0] ^ _gf_mul(a[1], 2) ^ _gf_mul(a[2], 3) ^ a[3]
            s[2][c] = a[0] ^ a[1] ^ _gf_mul(a[2], 2) ^ _gf_mul(a[3], 3)
            s[3][c] = _gf_mul(a[0], 3) ^ a[1] ^ a[2] ^ _gf_mul(a[3], 2)

    def inv_mixcol():
        for c in range(4):
            a = [s[r][c] for r in range(4)]
            s[0][c] = (_gf_mul(a[0], 14) ^ _gf_mul(a[1], 11)
                       ^ _gf_mul(a[2], 13) ^ _gf_mul(a[3], 9))
            s[1][c] = (_gf_mul(a[0], 9) ^ _gf_mul(a[1], 14)
                       ^ _gf_mul(a[2], 11) ^ _gf_mul(a[3], 13))
            s[2][c] = (_gf_mul(a[0], 13) ^ _gf_mul(a[1], 9)
                       ^ _gf_mul(a[2], 14) ^ _gf_mul(a[3], 11))
            s[3][c] = (_gf_mul(a[0], 11) ^ _gf_mul(a[1], 13)
                       ^ _gf_mul(a[2], 9) ^ _gf_mul(a[3], 14))

    if not decrypt:
        add_rk(0)
        for rnd in range(1, nr):
            sub(); shift(); mixcol(); add_rk(rnd)
        sub(); shift(); add_rk(nr)
    else:
        add_rk(nr)
        for rnd in range(nr - 1, 0, -1):
            sub(inv=True); shift(inv=True); add_rk(rnd); inv_mixcol()
        sub(inv=True); shift(inv=True); add_rk(0)
    return bytes(s[r][c] for c in range(4) for r in range(4))


def pkcs7_pad(data, bs=16):
    n = bs - (len(data) % bs)
    return data + bytes([n]) * n


def pkcs7_unpad(data):
    if not data:
        raise ValueError("pkcs7: empty")
    n = data[-1]
    if n < 1 or n > 16 or n > len(data):
        raise ValueError("pkcs7: bad padding")
    return data[:-n]


def aes_ecb_encrypt(data, key):
    w, nr = _aes_expand(key)
    return b"".join(_aes_block(data[i:i + 16], w, nr)
                    for i in range(0, len(data), 16))


def aes_ecb_decrypt(data, key):
    w, nr = _aes_expand(key)
    return b"".join(_aes_block(data[i:i + 16], w, nr, decrypt=True)
                    for i in range(0, len(data), 16))


def aes_cbc_encrypt(data, key, iv):
    w, nr = _aes_expand(key)
    out, prev = [], iv
    padded = pkcs7_pad(data)
    for i in range(0, len(padded), 16):
        blk = bytes(x ^ y for x, y in zip(padded[i:i + 16], prev))
        prev = _aes_block(blk, w, nr)
        out.append(prev)
    return b"".join(out)


def aes_cbc_decrypt(data, key, iv):
    w, nr = _aes_expand(key)
    out, prev = [], iv
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        out.append(bytes(x ^ y for x, y in zip(_aes_block(blk, w, nr, True), prev)))
        prev = blk
    return pkcs7_unpad(b"".join(out))


# ---- RSA PKCS#1 v1.5(仅 SMS 流程给登录名加密用) ----
def _der_len(data, i):
    b = data[i]
    if b < 0x80:
        return b, i + 1
    n = b & 0x7F
    return int.from_bytes(data[i + 1:i + 1 + n], "big"), i + 1 + n


def _der_tlv(data, i):
    tag = data[i]
    ln, j = _der_len(data, i + 1)
    return tag, data[j:j + ln], j + ln


def _rsa_pub_numbers(spki_b64):
    der = base64.b64decode(spki_b64)
    _, spki, _ = _der_tlv(der, 0)
    i = 0
    _, _, i = _der_tlv(spki, i)            # algorithm seq
    _, bitstr, _ = _der_tlv(spki, i)       # subjectPublicKey BIT STRING
    _, rsaseq, _ = _der_tlv(bitstr[1:], 0)
    j = 0
    _, n_bytes, j = _der_tlv(rsaseq, j)
    _, e_bytes, _ = _der_tlv(rsaseq, j)
    return (int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big"))


def rsa_encrypt_pkcs1v15(spki_b64, msg):
    n, e = _rsa_pub_numbers(spki_b64)
    k = (n.bit_length() + 7) // 8
    ps_len = k - len(msg) - 3
    if ps_len < 8:
        raise ValueError("message too long for RSA key")
    ps = bytearray()
    while len(ps) < ps_len:
        ps.extend(b for b in os.urandom(ps_len) if b != 0)
    em = b"\x00\x02" + bytes(ps[:ps_len]) + b"\x00" + msg
    c = pow(int.from_bytes(em, "big"), e, n)
    return base64.b64encode(c.to_bytes(k, "big")).decode()


# ---- Cookie 工具 ----
def parse_cookie_header(s):
    out = []
    for part in (s or "").split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            if name.strip():
                out.append((name.strip(), value.strip()))
    return out


def cookie_value(cookies, name):
    for k, v in parse_cookie_header(cookies):
        if k == name:
            return v
    return ""


def merge_cookie_header(old, set_cookies):
    pairs = parse_cookie_header(old)
    idx = {k: i for i, (k, _) in enumerate(pairs)}
    for sc in set_cookies or []:
        first = sc.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        name = name.strip()
        if name in idx:
            pairs[idx[name]] = (name, value.strip())
        else:
            idx[name] = len(pairs)
            pairs.append((name, value.strip()))
    return "; ".join("%s=%s" % kv for kv in pairs)


# ---- HTTP 小助手 ----
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_raw(method, url, body=None, headers=None, no_redirect=False, timeout=30):
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method=method)
    opener = (urllib.request.build_opener(_NoRedirect) if no_redirect
              else urllib.request.build_opener())
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _now_ms():
    return int(time.time() * 1000)


def _sha1_hex(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


class SmsVerificationNeeded(Exception):
    """登录触发短信风控且验证码已发送;mail_cookies 是合并后的最新 Cookie。"""

    def __init__(self, msg, mail_cookies=""):
        super().__init__(msg)
        self.mail_cookies = mail_cookies


_SMS_SCENES = {"PML401010062": 2, "MW0016": 4, "S025": 1, "S035": 1}


def _mail_xml_parse(body_bytes):
    txt = body_bytes.decode("utf-8", "replace")
    out = {}
    m = re.search(r"<code>([^<]*)</code>", txt)
    out["code"] = m.group(1).strip() if m else ""
    m = re.search(r"<summary>([^<]*)</summary>", txt)
    out["summary"] = m.group(1).strip() if m else ""
    m = (re.search(r'name="loginSuccessUrl"[^>]*>([^<]*)<', txt)
         or re.search(r"<loginSuccessUrl>([^<]*)</loginSuccessUrl>", txt))
    out["loginSuccessUrl"] = m.group(1).strip() if m else ""
    return out


def _mail_xml_field(name, value):
    esc = (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return '<string name="%s">%s</string>' % (name, esc)


def _mail_xml_headers(cookies):
    return {"Cookie": cookies, "Content-Type": "application/xml; charset=utf-8",
            "User-Agent": "okhttp/4.12.0"}


def _send_sms_code(state, risk_code):
    scene = _SMS_SCENES[risk_code]
    login_name = rsa_encrypt_pkcs1v15(MAIL_PUBKEY_B64,
                                      state["user"].encode("utf-8"))
    body = ("<object>" + "".join([
        _mail_xml_field("loginName", login_name),
        _mail_xml_field("fv", "4"),
        _mail_xml_field("clientId", "1003"),
        _mail_xml_field("eMode", "1"),
        _mail_xml_field("loginFailureUrl", ""),
        _mail_xml_field("loginSuccessUrl", ""),
        _mail_xml_field("verifyCode", ""),
        _mail_xml_field("version", "1.0"),
        _mail_xml_field("scene", str(scene)),
    ]) + "</object>").encode("utf-8")
    url = (MAIL_SMS_URL + "?func=" + urllib.parse.quote("login:sendSmsCodeByScene")
           + "&cguid=" + str(_now_ms()))
    st, headers, raw = _http_raw("POST", url, body,
                                 _mail_xml_headers(state["cookies"]))
    state["cookies"] = merge_cookie_header(
        state["cookies"], headers.get_all("Set-Cookie") or [])
    resp = _mail_xml_parse(raw)
    if resp["code"] == "S_OK":
        return
    if resp["code"] in ("PML401010021", "PML401010022"):
        raise RuntimeError("139 邮箱要求先过图形验证码才能发短信:%s" % resp["code"])
    if resp["code"] == "PML404010001":
        raise RuntimeError("139 短信验证码请求太频繁,请稍后再试")
    raise RuntimeError("发送 139 短信验证码失败:code=%s %s"
                       % (resp["code"], resp["summary"]))


def _verify_sms_code(state, risk_code):
    login_name = rsa_encrypt_pkcs1v15(MAIL_PUBKEY_B64,
                                      state["user"].encode("utf-8"))
    fields = [
        _mail_xml_field("clientId", "1003"),
        _mail_xml_field("version", "4"),
        _mail_xml_field("loginType", "0"),
        _mail_xml_field("authType", "2"),
        _mail_xml_field("loginName", login_name),
        _mail_xml_field("eMode", "1"),
        _mail_xml_field("loginPassword", _sha1_hex("fetion.com.cn:" + state["sms"])),
        _mail_xml_field("createAutoLoginSecretKey", "1"),
        _mail_xml_field("verifyCode", ""),
        _mail_xml_field("verifyAgentId", ""),
        _mail_xml_field("reqFrom", "3"),
        _mail_xml_field("needWCookie", "1"),
    ]
    if risk_code == "MW0016":
        fields.append(_mail_xml_field("pwdType", "1"))
    body = ("<object>" + "".join(fields) + "</object>").encode("utf-8")
    url = (MAIL_SMS_URL + "?func=" + urllib.parse.quote("/login/inlogin.action")
           + "&cguid=" + str(_now_ms()))
    st, headers, raw = _http_raw("POST", url, body,
                                 _mail_xml_headers(state["cookies"]))
    state["cookies"] = merge_cookie_header(
        state["cookies"], headers.get_all("Set-Cookie") or [])
    resp = _mail_xml_parse(raw)
    if resp["code"] != "S_OK":
        raise RuntimeError("139 短信验证码校验失败:code=%s %s"
                           % (resp["code"], resp["summary"]))
    sid = ""
    if resp["loginSuccessUrl"]:
        q = urllib.parse.parse_qs(
            urllib.parse.urlparse(resp["loginSuccessUrl"]).query)
        sid = (q.get("sid") or [""])[0]
    if not sid:
        sid = (cookie_value(state["cookies"], "Os_SSo_Sid")
               or cookie_value(state["cookies"], "sid"))
    if not sid:
        raise RuntimeError("短信校验成功但未返回 sid")
    state["sms"] = ""
    return sid


def _step1_password_login(state):
    hashed = _sha1_hex("fetion.com.cn:" + state["password"])
    cguid = str(_now_ms())
    referer = ("https://mail.10086.cn/default.html?&s=1&v=0&u=%s&m=1&ec=S001"
               "&resource=indexLogin&clientid=1003&auto=on&cguid=%s&mtime=45"
               % (base64.b64encode(state["user"].encode()).decode(), cguid))
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://mail.10086.cn",
        "Referer": referer,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"),
        "Cookie": state["cookies"],
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    form = urllib.parse.urlencode({
        "UserName": state["user"], "passOld": "", "auto": "on",
        "Password": hashed, "webIndexPagePwdLogin": "1", "pwdType": "1",
        "clientId": "1003", "authType": "2"}).encode("utf-8")
    st, resp_headers, raw = _http_raw("POST", MAIL_LOGIN_URL, form, headers,
                                      no_redirect=True)
    set_cookies = resp_headers.get_all("Set-Cookie") or []
    state["cookies"] = merge_cookie_header(state["cookies"], set_cookies)
    location = resp_headers.get("Location") or ""
    sid = ""
    if location:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        risk = (q.get("ec") or [""])[0]
        if risk:
            if risk not in _SMS_SCENES:
                raise RuntimeError("139 邮箱风控拦截(ec=%s),暂不支持该验证方式" % risk)
            if not state["sms"]:
                _send_sms_code(state, risk)
                raise SmsVerificationNeeded(
                    "139 登录触发短信验证,验证码已发送;请把收到的验证码填入"
                    "设置页『移动短信验证码』,再点『立即刷新移动授权』",
                    mail_cookies=state["cookies"])
            return _verify_sms_code(state, risk)
        sid = (q.get("sid") or [""])[0]
    if not sid:
        for sc in set_cookies:
            first = sc.split(";", 1)[0]
            if "=" in first:
                name, value = first.split("=", 1)
                if name.strip() in ("Os_SSo_Sid", "sid"):
                    sid = value.strip()
                    break
    if not sid:
        raise RuntimeError("步骤1 邮箱登录未取到 sid(账号/密码可能不对,或登录被拒)")
    return sid


def _step2_artifact(state, sid):
    rmkey = ""
    for part in state["cookies"].split(";"):
        part = part.strip()
        if part.startswith("RMKEY="):
            rmkey = part
            break
    if not rmkey:
        raise RuntimeError("邮箱 Cookie 里缺少 RMKEY,无法换取 artifact")
    url = (ARTIFACT_URL + "?func=" + urllib.parse.quote("umc:getArtifact")
           + "&sid=" + urllib.parse.quote(sid) + "&cguid=" + str(_now_ms()))
    headers = {"Cookie": rmkey, "Content-Type": "text/xml; charset=utf-8",
               "User-Agent": "okhttp/4.12.0"}
    st, resp_headers, raw = _http_raw("POST", url, b"", headers)
    try:
        dycpwd = json.loads(raw.decode("utf-8", "replace"))["var"]["artifact"]
    except Exception:  # noqa: BLE001
        dycpwd = ""
    if not dycpwd:
        raise RuntimeError("步骤2 未换到 artifact(响应:%s)" % raw[:120])
    return dycpwd


def _sorted_json(obj):
    if isinstance(obj, dict):
        return "{" + ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + _sorted_json(obj[k])
            for k in sorted(obj)) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(_sorted_json(x) for x in obj) + "]"
    return json.dumps(obj, ensure_ascii=False)


def _step3_thirdlogin(state, dycpwd):
    plain = {
        "clientkey_decrypt": "l3TryM&Q+X7@dzwk)qP",
        "clienttype": "886",
        "cpid": "507",
        "dycpwd": dycpwd,
        "extInfo": {"ifOpenAccount": "0"},
        "loginMode": "0",
        "msisdn": state["user"],
        "pintype": "13",
        "secinfo": _sha1_hex("fetion.com.cn:" + dycpwd).upper(),
        "version": "20250901",
    }
    iv = os.urandom(16)
    payload = base64.b64encode(
        iv + aes_cbc_encrypt(_sorted_json(plain).encode("utf-8"), _KEY1, iv))
    headers = {
        "hcy-cool-flag": "1",
        "x-huawei-channelSrc": "10246600",
        "x-MM-Source": "0",
        "x-UserAgent": "android|23116PN5BC|android15|1.2.6|||1440x3200|10246600",
        "x-DeviceInfo": ("4|127.0.0.1|5|1.2.6|Xiaomi|23116PN5BC||"
                         "02-00-00-00-00-00|android 15|1440x3200|android|||"),
        "Content-Type": "text/plain;charset=UTF-8",
        "User-Agent": "okhttp/3.12.2",
    }
    st, resp_headers, raw = _http_raw("POST", THIRDLOGIN_URL, payload, headers)
    raw = raw.strip()
    if raw.startswith(b"{"):
        layer1 = raw
    else:
        decoded = base64.b64decode(raw)
        layer1 = aes_cbc_decrypt(decoded[16:], _KEY1, decoded[:16])
    try:
        hex_inner = json.loads(layer1.decode("utf-8", "replace"))["data"]
    except Exception:  # noqa: BLE001
        raise RuntimeError("步骤3 第一层响应异常:%s" % layer1[:120])
    final = json.loads(pkcs7_unpad(aes_ecb_decrypt(
        bytes.fromhex(hex_inner), _KEY2)).decode("utf-8", "replace"))
    token = final.get("authToken") or ""
    account = final.get("account") or ""
    udid = final.get("userDomainId") or ""
    if not token or not account:
        raise RuntimeError("步骤3 未取到 authToken:%s" % str(final)[:160])
    auth = base64.b64encode(
        ("pc:%s:%s" % (account, token)).encode("utf-8")).decode()
    return auth, udid


def login_139_password(user, password, mail_cookies, sms_code=""):
    """完整密码登录(步骤1→2→3),返回 {auth, mail_cookies, udid, sms_code}。"""
    if not user or not password:
        raise RuntimeError("未配置移动用户名/密码")
    state = {"user": user, "password": password,
             "cookies": mail_cookies or "", "sms": sms_code or ""}
    sid = _step1_password_login(state)
    dycpwd = _step2_artifact(state, sid)
    auth, udid = _step3_thirdlogin(state, dycpwd)
    return {"auth": auth, "mail_cookies": state["cookies"],
            "udid": udid, "sms_code": state["sms"]}


def login_139_fast(user, mail_cookies):
    """邮箱 Cookie 快速登录(跳过步骤1,用现成 Os_SSo_Sid 走步骤2→3)。"""
    sid = cookie_value(mail_cookies or "", "Os_SSo_Sid")
    if not sid or not cookie_value(mail_cookies or "", "RMKEY"):
        raise RuntimeError("邮箱 Cookie 缺少 Os_SSo_Sid 或 RMKEY,无法快速登录")
    state = {"user": user or "", "password": "",
             "cookies": mail_cookies or "", "sms": ""}
    dycpwd = _step2_artifact(state, sid)
    auth, udid = _step3_thirdlogin(state, dycpwd)
    return {"auth": auth, "mail_cookies": state["cookies"],
            "udid": udid, "sms_code": ""}


def auth_expiry_ms(auth_b64):
    """解析 Authorization 里令牌的到期毫秒时间戳;解析不了返回 None。"""
    try:
        decoded = base64.b64decode(auth_b64).decode("utf-8", "replace")
        parts = decoded.split(":")
        if len(parts) < 3:
            return None
        segs = parts[2].split("|")
        if len(segs) < 4:
            return None
        return int(segs[3])
    except Exception:  # noqa: BLE001
        return None


def refresh_139_auth(auth_b64):
    """调 authTokenRefresh.do 刷新令牌,返回新的 Authorization(失败抛错)。"""
    try:
        decoded = base64.b64decode(auth_b64).decode("utf-8", "replace")
        parts = decoded.split(":")
        token, account = parts[2], parts[1]
    except Exception:  # noqa: BLE001
        raise RuntimeError("Authorization 格式不对,无法刷新")
    body = ("<root><token>%s</token><account>%s</account>"
            "<clienttype>656</clienttype></root>" % (token, account)).encode()
    st, headers, raw = _http_raw(
        "POST", REFRESH_URL, body, {"Content-Type": "application/xml"})
    txt = raw.decode("utf-8", "replace")
    m_ret = re.search(r"<return>([^<]*)</return>", txt)
    m_tok = re.search(r"<token>([^<]*)</token>", txt)
    if not m_ret or m_ret.group(1).strip() != "0" or not m_tok:
        m_desc = re.search(r"<desc>([^<]*)</desc>", txt)
        raise RuntimeError("令牌刷新失败:%s"
                           % (m_desc.group(1) if m_desc else txt[:120]))
    return base64.b64encode(
        ("%s:%s:%s" % (parts[0], account, m_tok.group(1).strip())
         ).encode("utf-8")).decode()
