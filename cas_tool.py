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
