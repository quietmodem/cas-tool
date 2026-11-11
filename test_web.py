#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cas_web 端到端测试:用 mock OpenList + mock 139 接口跑通全部 API。
运行: python3 test_web.py  (全部通过时最后一行打印 ALL PASS)"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

import cas_tool as ct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 测试数据:11MiB 确定性内容(跨 10MiB 分片边界) ----
PATTERN = b"0123456789abcdef"
CONTENT = (PATTERN * (11534336 // len(PATTERN) + 1))[:11534336]
FNAME = "测试电影.mkv"
FPATH = "/天翼云盘/" + FNAME
SCAN_CAS = None  # main() 里用真实 cas_tool 编码生成

captured = {"put": [], "seed": [], "create139": []}


class MockOL(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", "0") or "0")
        body = json.loads(self.rfile.read(ln) or b"{}")
        if self.path == "/api/fs/list":
            if self.headers.get("Authorization") == "bad-token":
                return self._json({"code": 401, "message": "token is invalidated"})
            if body.get("path") == "/":
                content = [{"name": "天翼云盘", "size": 0, "is_dir": True},
                           {"name": "移动云盘", "size": 0, "is_dir": True}]
            elif body.get("path") == "/天翼云盘":
                content = [{"name": FNAME, "size": len(CONTENT), "is_dir": False}]
            elif body.get("path") == "/移动云盘":
                content = [{"name": "中转", "size": 0, "is_dir": True, "id": "CATID-999"},
                           {"name": "备份", "size": 0, "is_dir": True, "id": "LISTID-555"}]
            elif body.get("path") == "/扫描":
                content = [{"name": "a.cas", "size": len(SCAN_CAS), "is_dir": False},
                           {"name": "视频.mp4", "size": 99, "is_dir": False},
                           {"name": "子目录", "size": 0, "is_dir": True}]
            elif body.get("path") == "/扫描/子目录":
                content = [{"name": "b.cas", "size": len(SCAN_CAS), "is_dir": False}]
            else:
                content = []
            return self._json({"code": 200, "data": {"content": content, "total": len(content)}})
        if self.path == "/api/fs/get":
            if body.get("path") == "/移动盘2":
                return self._json({"code": 200, "data": {"name": "移动盘2",
                                                         "is_dir": True}})
            if body.get("path") == "/移动云盘/中转":
                return self._json({"code": 200, "data": {"name": "中转",
                                                         "is_dir": True,
                                                         "id": "CATID-999"}})
            if str(body.get("path", "")).endswith(".cas"):
                return self._json({"code": 200, "data": {
                    "name": "x.cas", "size": len(SCAN_CAS),
                    "raw_url": "http://127.0.0.1:%d/rawcas" % self.server.server_address[1]}})
            return self._json({"code": 200, "data": {
                "name": FNAME, "size": len(CONTENT),
                "raw_url": "http://127.0.0.1:%d/raw" % self.server.server_address[1]}})
        if self.path == "/api/fs/seed/capabilities":
            captured["seed"].append(("capabilities", body))
            return self._json({"code": 200, "data": {"method": "189pc_cas"}})
        if self.path == "/api/fs/seed/rapid_upload":
            captured["seed"].append(("rapid_upload", body))
            return self._json({"code": 200, "data": {"task_id": "t1"}})
        return self._json({"code": 400, "message": "unknown " + self.path})

    def do_PUT(self):
        ln = int(self.headers.get("Content-Length", "0") or "0")
        data = self.rfile.read(ln)
        if self.path == "/api/fs/put":
            captured["put"].append((self.headers.get("File-Path"), data))
            return self._json({"code": 200, "data": None})
        return self._json({"code": 400})

    def do_GET(self):
        if self.path.startswith("/api/admin/storage/list"):
            return self._json({"code": 200, "data": {"content": [
                {"mount_path": "/移动云盘", "driver": "139Yun",
                 "addition": json.dumps({"root_folder_id": "/"})},
                {"mount_path": "/移动盘2", "driver": "139Yun",
                 "addition": json.dumps({"root_folder_id": "ROOTCAT"})},
            ]}})
        if self.path == "/rawcas":
            self.send_response(200)
            self.send_header("Content-Length", str(len(SCAN_CAS)))
            self.end_headers()
            self.wfile.write(SCAN_CAS)
            return
        if self.path == "/raw":
            self.send_response(200)
            self.send_header("Content-Length", str(len(CONTENT)))
            self.end_headers()
            self.wfile.write(CONTENT)
            return
        self.send_response(404)
        self.end_headers()


M139_FILE = "139测试.mkv"
M139_LOGIN = {"step1": 0, "step2": 0, "step3": 0, "refresh": 0,
              "refresh_ok": True, "step1_pw_ok": False, "step3_plain": {}}


def _m139_token(days=30):
    exp = int(time.time() * 1000) + days * 24 * 3600 * 1000
    return "p1|p2|p3|%d" % exp


def _m139_auth(days=30):
    return base64.b64encode(
        ("pc:13800138000:%s" % _m139_token(days)).encode()).decode()


class Mock139(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/raw139":
            self.send_response(200)
            self.send_header("Content-Length", str(len(CONTENT)))
            self.end_headers()
            self.wfile.write(CONTENT)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(ln)
        upath = urllib.parse.urlparse(self.path).path
        if upath == "/Login/Login.ashx":
            M139_LOGIN["step1"] += 1
            form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            exp_pw = hashlib.sha1("fetion.com.cn:pw139".encode()).hexdigest()
            M139_LOGIN["step1_pw_ok"] = form.get("Password") == [exp_pw] \
                and form.get("UserName") == ["139user@test"]
            body = b""
            self.send_response(302)
            self.send_header("Location",
                             "https://mail.10086.cn/default.html?sid=SID-1")
            self.send_header("Set-Cookie", "Os_SSo_Sid=SID-1; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if upath == "/setting/s":
            M139_LOGIN["step2"] += 1
            data = json.dumps({"var": {"artifact": "DYCPWD-TEST"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if upath == "/user/thirdlogin":
            M139_LOGIN["step3"] += 1
            dec = base64.b64decode(raw)
            plain = ct.aes_cbc_decrypt(dec[16:], ct._KEY1, dec[:16])
            M139_LOGIN["step3_plain"] = json.loads(plain)
            final = {"authToken": _m139_token(30), "account": "13800138000",
                     "userDomainId": "UD-9"}
            layer2 = ct.aes_ecb_encrypt(
                ct.pkcs7_pad(json.dumps(final).encode()), ct._KEY2).hex()
            layer1 = json.dumps({"data": layer2}).encode()
            iv = os.urandom(16)
            data = base64.b64encode(iv + ct.aes_cbc_encrypt(layer1, ct._KEY1, iv))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if upath == "/tellin/authTokenRefresh.do":
            M139_LOGIN["refresh"] += 1
            if M139_LOGIN["refresh_ok"]:
                xml = ("<root><return>0</return><desc>ok</desc>"
                       "<token>%s</token></root>" % _m139_token(30)).encode()
            else:
                xml = b"<root><return>100</return><desc>token invalid</desc></root>"
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(xml)))
            self.end_headers()
            self.wfile.write(xml)
            return
        if self.path == "/hcy/file/list":
            body = json.loads(raw)
            parent = body.get("parentFileId") or "/"
            if parent == "/":
                items = [{"fileId": "D1", "name": "视频", "size": 0, "type": "folder"},
                         {"fileId": "F139", "name": M139_FILE, "size": len(CONTENT),
                          "type": "file"}]
            elif parent == "D1":
                items = [{"fileId": "F139B", "name": "子文件.mkv",
                          "size": len(CONTENT), "type": "file"}]
            else:
                items = []
            data = json.dumps({"code": "0000", "data": {"items": items,
                                                        "nextPageCursor": ""}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/hcy/file/getDownloadUrl":
            url = "http://127.0.0.1:%d/raw139" % self.server.server_address[1]
            data = json.dumps({"code": "0000", "data": {"url": url}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/hcy/file/create":
            captured["create139"].append((dict(self.headers), json.loads(raw)))
            data = json.dumps({"code": "0000", "data": {"fileId": "F123", "exist": True}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()


def serve(cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def req(method, url, body=None, headers=None, raw=False):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            out = resp.read()
            return resp.status, (out if raw else json.loads(out)), resp.headers
    except urllib.error.HTTPError as e:
        out = e.read()
        try:
            return e.code, (out if raw else json.loads(out)), e.headers
        except ValueError:
            return e.code, out, e.headers


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" " + str(extra) if extra and not cond else ""))
    if not cond:
        raise SystemExit("测试失败: " + name)


def main():
    mock_ol = serve(MockOL)
    mock_139 = serve(Mock139)
    ol_port = mock_ol.server_address[1]
    os.environ["OPENLIST_URL"] = "http://127.0.0.1:%d" % ol_port
    os.environ["OPENLIST_TOKEN"] = "test-token"
    os.environ["CAS139_PHONE"] = "13800138000"
    os.environ["CAS139_AUTH"] = "Basic dGVzdA=="
    os.environ["CAS_DATA_DIR"] = tempfile.mkdtemp(prefix="casweb-test-")

    import cas_tool as ct
    import cas_web
    global SCAN_CAS
    SCAN_CAS = ct.encode_cas({
        "name": "扫描到的影片.mkv", "size": 123456, "md5": "", "sliceMd5": "",
        "sha256": "b" * 64, "recover_pass": "", "create_time": "1794055893"})
    ct.FILE_CREATE_URL = "http://127.0.0.1:%d/hcy/file/create" % mock_139.server_address[1]
    ct.FILE_LIST_URL = "http://127.0.0.1:%d/hcy/file/list" % mock_139.server_address[1]
    ct.FILE_DOWNLOAD_URL = "http://127.0.0.1:%d/hcy/file/getDownloadUrl" % mock_139.server_address[1]
    _p139 = mock_139.server_address[1]
    ct.MAIL_LOGIN_URL = "http://127.0.0.1:%d/Login/Login.ashx" % _p139
    ct.MAIL_SMS_URL = "http://127.0.0.1:%d/s" % _p139
    ct.ARTIFACT_URL = "http://127.0.0.1:%d/setting/s" % _p139
    ct.THIRDLOGIN_URL = "http://127.0.0.1:%d/user/thirdlogin" % _p139
    ct.REFRESH_URL = "http://127.0.0.1:%d/tellin/authTokenRefresh.do" % _p139

    web = ThreadingHTTPServer(("127.0.0.1", 0), cas_web.Handler)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % web.server_address[1]

    # a) 首页
    st, html, _ = req("GET", base + "/", raw=True)
    check("首页可打开", st == 200 and "CAS 双云工具".encode() in html)
    check("首页含目录选择器", all(x in html for x in
          ["选目录".encode(), b'id="picker"', b"pickerPick", b"loadPicker", b"renderPicker"]))
    check("首页含批量/zip 支持", all(x in html for x in
          [b"multiple", b"unzipcas", b"batchItems", b"/api/exists", b'id="sidecar"']))
    check("首页含三选项卡与设置页", all(x in html for x in
          [b'id="tab-restore"', b'id="tab-gen"', b'id="tab-settings"', b"switchTab",
           b'id="home189"', b'id="home139"', b"/api/settings"]))
    check("首页含新选择器(面包屑/筛选)且无 cas 库", all(x in html for x in
          [b'id="pickerfilter"', b'id="crumbs"', b"renderCrumbs", b"pickerHome"])
          and b"saveToLib" not in html and b'id="lib"' not in html)
    check("首页含扫描功能与弹窗选择器", all(x in html for x in
          [b'id="scanpath"', b"/api/scan", b"scanCas", b'class="overlay"', b'class="sheet"']))
    check("首页含扫描独立列表与后台配置区", all(x in html for x in
          [b"renderScan", b"scanRestore", b'id="scanlist"', b'id="cfgurl"', b'id="cfgtoken"', b'id="cfgpw"']))
    check("首页含云盘路径校验", all(x in html for x in
          [b'id="paths189"', b'id="paths139"', b"cloudPathSet", b"inCloud", b"itemCloud"]))
    check("v1.10 还原只剩两个按钮,无 OL 按钮", all(x in html for x in
          ["还原到天翼云盘".encode(), "还原到移动云盘".encode(), b"batchall", b"batchAll"])
          and "经 OpenList 还原".encode() not in html and "OL按钮".encode() not in html)
    check("v1.10 批量列表有全选复选框", b'id="batchall"' in html and b"class=\"itchk\"" in html)

    # b) 状态
    st, d, _ = req("GET", base + "/api/status")
    check("status OpenList 连通", d["data"]["openlist_ok"] is True, d)
    check("status 移动凭据已配置", d["data"]["m139_configured"] is True)

    # c) 浏览
    st, d, _ = req("GET", base + "/api/list?path=" + urllib.parse.quote("/"))
    check("根目录两个挂载", len(d["data"]["entries"]) == 2)
    st, d, _ = req("GET", base + "/api/list?path=" + urllib.parse.quote("/天翼云盘"))
    ent = d["data"]["entries"][0]
    check("天翼目录有测试文件", ent["name"] == FNAME and ent["size"] == len(CONTENT))

    # d) 生成 cas(带上传回网盘)
    st, d, _ = req("POST", base + "/api/gen",
                   {"path": FPATH, "size": len(CONTENT), "upload_back": True})
    check("gen 任务已建", d["ok"] and d["data"]["job_id"])
    job = None
    for _ in range(60):
        st, d, _ = req("GET", base + "/api/jobs")
        job = d["data"][0]
        if job["status"] != "running":
            break
        time.sleep(0.5)
    check("gen 任务完成", job["status"] == "done", job)
    cas_bytes = base64.b64decode(job["result"]["cas_b64"])
    cas_file = FNAME + ".cas"
    check("gen 结果记录上传路径", job["result"].get("uploaded_to") == FPATH + ".cas", job["result"])
    obj = ct.decode_cas_bytes(cas_bytes)
    exp_md5 = hashlib.md5(CONTENT).hexdigest().upper()
    exp_sha = hashlib.sha256(CONTENT).hexdigest()
    pieces = [hashlib.md5(CONTENT[i:i + 10 * 1024 * 1024]).hexdigest().upper()
              for i in range(0, len(CONTENT), 10 * 1024 * 1024)]
    exp_slice = hashlib.md5("\n".join(pieces).encode()).hexdigest().upper()
    check("生成 cas 的 md5/sha256/sliceMd5 与独立计算一致",
          obj["md5"] == exp_md5 and obj["sha256"] == exp_sha and obj["sliceMd5"] == exp_slice,
          obj)
    check("fs/put 已把 cas 上传回同目录",
          len(captured["put"]) == 1 and captured["put"][0][1] == cas_bytes
          and urllib.parse.unquote(captured["put"][0][0]) == FPATH + ".cas",
          captured["put"])

    # e) inspect 移动版样本(优先用真实样品;没有时用同结构合成一份,保证自包含)
    sample = os.path.expanduser("~/workspace/user/files/2026_2160p_V2_BluRay_REMUX_HDR10_10bit_H_265_DTS_5_1_mkv.cas")
    if os.path.exists(sample):
        with open(sample, "rb") as f:
            sample_bytes = f.read()
    else:
        sample_bytes = ct.encode_cas({
            "name": "奥德赛.2026.2160p.V2.BluRay.REMUX.HDR10.10bit.H.265.DTS.5.1.mkv",
            "size": 88188041773, "md5": "", "sliceMd5": "",
            "sha256": "a645a49a267973cbf156f5d458bc0f7a08234b00e01e67a83a62831655f24775",
            "recover_pass": "", "create_time": "1794055893"})
    sample_b64 = base64.b64encode(sample_bytes).decode()
    st, d, _ = req("POST", base + "/api/inspect", {"cas_b64": sample_b64})
    check("inspect 样品=移动版", d["ok"] and d["data"]["kind"].startswith("mobile"), d)

    # f) 还原到移动(mock 命中)
    st, d, _ = req("POST", base + "/api/restore139", {"cas_b64": sample_b64, "parent": "/"})
    check("restore139 命中", d["ok"], d)

    # p) 移动直调:按目标目录换算 fileId / 无目录落根 / 手填优先
    st, d, _ = req("POST", base + "/api/restore139",
                   {"cas_b64": sample_b64, "target": "/移动云盘/中转"})
    check("直调按目录还原", d["ok"] and captured["create139"][-1][1]["parentFileId"] == "CATID-999", d)
    st, d, _ = req("POST", base + "/api/restore139", {"cas_b64": sample_b64, "target": ""})
    check("直调无目录落根目录", d["ok"] and captured["create139"][-1][1]["parentFileId"] == "/", d)
    st, d, _ = req("POST", base + "/api/restore139",
                   {"cas_b64": sample_b64, "parent": "MANUAL-ID", "target": "/移动云盘/中转"})
    check("手填 fileId 优先", d["ok"] and captured["create139"][-1][1]["parentFileId"] == "MANUAL-ID", d)

    # q) 挂载根目录:fs/get 无 id -> 存储列表 root_folder_id
    st, d, _ = req("POST", base + "/api/restore139",
                   {"cas_b64": sample_b64, "target": "/移动云盘"})
    check("挂载根目录还原到139根", d["ok"] and captured["create139"][-1][1]["parentFileId"] == "/", d)
    st, d, _ = req("POST", base + "/api/restore139",
                   {"cas_b64": sample_b64, "target": "/移动盘2"})
    check("挂载根目录用其 root_folder_id", d["ok"] and captured["create139"][-1][1]["parentFileId"] == "ROOTCAT", d)

    # r) 父目录列表取子项 id;设置里的文件夹 ID 对照最优先
    st, d, _ = req("POST", base + "/api/restore139",
                   {"cas_b64": sample_b64, "target": "/移动云盘/备份"})
    check("父目录列表取子项 id", d["ok"] and captured["create139"][-1][1]["parentFileId"] == "LISTID-555", d)
    st, d, _ = req("POST", base + "/api/settings",
                   {"mobile_dir_ids_text": "/TV/移动云盘/中转 = FID-777\n坏行\n/另一目录,FID-888"})
    check("文件夹ID对照保存", d["ok"], d)
    st, d, _ = req("GET", base + "/api/settings")
    txt = d["data"].get("mobile_dir_ids_text", "")
    check("对照回显含两条", "FID-777" in txt and "FID-888" in txt and "坏行" not in txt, d)
    st, d, _ = req("POST", base + "/api/restore139",
                   {"cas_b64": sample_b64, "target": "/TV/移动云盘/中转"})
    check("对照优先于自动解析", d["ok"] and captured["create139"][-1][1]["parentFileId"] == "FID-777", d)

    # s) 移动直连:列表 / 生成 / 状态探针
    st, d, _ = req("POST", base + "/api/m139/list", {"parent": "/"})
    names = [e["name"] for e in d["data"]["items"]] if d["ok"] else []
    check("139 直连列表", d["ok"] and "视频" in names and M139_FILE in names, d)
    fid139 = [e["id"] for e in d["data"]["items"] if e["name"] == M139_FILE][0]
    st, d, _ = req("GET", base + "/api/status")
    check("状态含移动直连已连通", d["ok"] and d["data"].get("m139_ok") is True, d)
    st, d, _ = req("POST", base + "/api/m139/gen",
                   {"file_id": fid139, "name": M139_FILE, "size": len(CONTENT)})
    check("139 直连生成任务已建", d["ok"] and d["data"]["job_id"], d)
    jid = d["data"]["job_id"]
    job = None
    for _ in range(60):
        st, d, _ = req("GET", base + "/api/jobs")
        job = [j for j in d["data"] if j["id"] == jid][0]
        if job["status"] != "running":
            break
        time.sleep(0.5)
    check("139 直连生成完成", job and job["status"] == "done", job)
    obj = ct.decode_cas_bytes(base64.b64decode(job["result"]["cas_b64"]))
    check("139 直连生成哈希正确",
          obj["md5"] == exp_md5 and obj["sha256"] == exp_sha and obj["sliceMd5"] == exp_slice,
          obj)

    # t) 移动密码登录回退(移植 OpenList 139 驱动的机制)
    check("AES NIST 向量(ECB-128)",
          ct.aes_ecb_encrypt(bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"),
                             bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
                             ).hex() == "3ad77bb40d7a3660a89ecaf32466ef97")
    st, d, _ = req("POST", base + "/api/settings", {
        "cas139_user": "139user@test", "cas139_pass": "pw139",
        "cas139_mail_cookies": "Os_SSo_Sid=SID-OLD; RMKEY=RK-1; other=x",
        "cas139_auth": _m139_auth(30)})
    check("回退三项已保存且回显到期日",
          d["ok"] and d["data"]["cas139_pass_set"]
          and d["data"]["cas139_mail_cookies_set"]
          and bool(d["data"]["cas139_auth_expiry"]), d)
    st, d, _ = req("POST", base + "/api/m139/list", {"parent": "/"})
    check("健康授权不触发登录/刷新",
          d["ok"] and M139_LOGIN["step1"] == 0 and M139_LOGIN["step3"] == 0
          and M139_LOGIN["refresh"] == 0, M139_LOGIN)
    st, d, _ = req("POST", base + "/api/settings", {"cas139_auth": _m139_auth(1)})
    st, d, _ = req("POST", base + "/api/m139/list", {"parent": "/"})
    check("临期授权自动刷新",
          d["ok"] and M139_LOGIN["refresh"] == 1 and M139_LOGIN["step1"] == 0,
          M139_LOGIN)
    M139_LOGIN["refresh_ok"] = False
    st, d, _ = req("POST", base + "/api/settings", {"cas139_auth": _m139_auth(-1)})
    st, d, _ = req("POST", base + "/api/m139/list", {"parent": "/"})
    check("过期+刷新失败回退密码登录",
          d["ok"] and M139_LOGIN["step1"] == 1 and M139_LOGIN["step2"] == 1
          and M139_LOGIN["step3"] == 1, M139_LOGIN)
    check("step1 密码哈希与账号正确", M139_LOGIN["step1_pw_ok"])
    p3 = M139_LOGIN["step3_plain"]
    check("step3 载荷正确",
          p3.get("msisdn") == "139user@test" and p3.get("dycpwd") == "DYCPWD-TEST"
          and p3.get("secinfo") == hashlib.sha1(
              "fetion.com.cn:DYCPWD-TEST".encode()).hexdigest().upper(), p3)
    saved = cas_web.load_settings()
    check("新授权与合并 Cookie 已回存",
          base64.b64decode(saved["cas139_auth"]).decode().startswith(
              "pc:13800138000:p1|")
          and "Os_SSo_Sid=SID-1" in saved["cas139_mail_cookies"],
          saved["cas139_auth"][:24])
    env_auth = os.environ.pop("CAS139_AUTH", None)
    try:
        st, d, _ = req("POST", base + "/api/settings",
                       {"clear_cas139_auth": True, "clear_cas139_pass": True})
        s1_before, s3_before = M139_LOGIN["step1"], M139_LOGIN["step3"]
        st, d, _ = req("POST", base + "/api/m139/list", {"parent": "/"})
        check("无授权走 Cookie 快速登录",
              d["ok"] and M139_LOGIN["step1"] == s1_before
              and M139_LOGIN["step3"] == s3_before + 1, M139_LOGIN)
    finally:
        if env_auth is not None:
            os.environ["CAS139_AUTH"] = env_auth
    M139_LOGIN["refresh_ok"] = True
    st, d, _ = req("POST", base + "/api/m139/relogin", {})
    check("手动刷新授权接口", d["ok"], d)
    hdrs, payload = captured["create139"][-1]
    check("139 请求带正确 contentHash", payload["contentHash"] ==
          "a645a49a267973cbf156f5d458bc0f7a08234b00e01e67a83a62831655f24775")
    check("139 请求带 Authorization 与三段签名",
          hdrs.get("Authorization", "").startswith("Basic ")
          and len(hdrs.get("Mcloud-Sign", "").split(",")) == 3, hdrs.get("Mcloud-Sign"))

    # g) 还原到天翼(经 mock OpenList 种子接口)
    st, d, _ = req("POST", base + "/api/restore189",
                   {"cas_b64": base64.b64encode(cas_bytes).decode(),
                    "cas_name": cas_file, "target": "/天翼云盘/电影"})
    check("restore189 成功", d["ok"], d)
    kinds = [k for k, _ in captured["seed"]]
    rapid = [b for k, b in captured["seed"] if k == "rapid_upload"][-1]
    check("种子接口先预检后秒传", kinds == ["capabilities", "rapid_upload"], kinds)
    check("rapid_upload 载荷正确",
          rapid["path"] == "/天翼云盘/电影" and rapid["file_name"] == cas_file
          and base64.b64decode(rapid["seed_data"]) == cas_bytes)

    # j) 经 OpenList 通用还原(移动版 cas -> 移动挂载目录,交给 OpenList 预检裁决)
    before = len([1 for k, _ in captured["seed"] if k == "rapid_upload"])
    st, d, _ = req("POST", base + "/api/restoreol",
                   {"cas_b64": sample_b64, "cas_name": "sample.cas", "target": "/移动云盘/电影"})
    check("restoreol 提交成功", d["ok"], d)
    rapids = [b for k, b in captured["seed"] if k == "rapid_upload"]
    check("restoreol 载荷指向移动挂载",
          len(rapids) == before + 1 and rapids[-1]["path"] == "/移动云盘/电影"
          and base64.b64decode(rapids[-1]["seed_data"]) == sample_bytes)

    # k) zip 解包出多个 cas + 还原后核验 + 空目标默认根目录
    import io as _io, zipfile as _zf
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("a.cas", sample_bytes)
        z.writestr("sub/b.cas", sample_bytes)
        z.writestr("readme.txt", "ignore me")
    st, d, _ = req("POST", base + "/api/unzipcas",
                   {"zip_b64": base64.b64encode(buf.getvalue()).decode()})
    check("unzipcas 取出 2 份 cas", d["ok"] and len(d["data"]) == 2
          and sorted(x["name"] for x in d["data"]) == ["a.cas", "b.cas"]
          and all(base64.b64decode(x["cas_b64"]) == sample_bytes for x in d["data"]), d)
    st, d, _ = req("POST", base + "/api/exists", {"path": "/天翼云盘", "name": FNAME})
    check("exists 命中已有文件", d["ok"] and d["data"]["found"] is True, d)
    st, d, _ = req("POST", base + "/api/exists", {"path": "/天翼云盘", "name": "nope.bin"})
    check("exists 未命中不存在文件", d["ok"] and d["data"]["found"] is False, d)
    st, d, _ = req("POST", base + "/api/restore189",
                   {"cas_b64": base64.b64encode(cas_bytes).decode(),
                    "cas_name": cas_file, "target": ""})
    rapid = [b for k, b in captured["seed"] if k == "rapid_upload"][-1]
    check("空目标默认提交到根目录 /", d["ok"] and rapid["path"] == "/", d)
    nput = len(captured["put"])
    st, d, _ = req("POST", base + "/api/sidecar",
                   {"dir": "/天翼云盘/中转", "video_name": FNAME, "cas_b64": sample_b64})
    check("sidecar 存到视频旁边", d["ok"] and len(captured["put"]) == nput + 1
          and urllib.parse.unquote(captured["put"][-1][0]) == "/天翼云盘/中转/" + FNAME + ".cas"
          and captured["put"][-1][1] == sample_bytes, d)

    # m) 扫描目录里的 cas
    st, d, _ = req("POST", base + "/api/scan", {"path": "/扫描", "recursive": True})
    check("扫描递归找到 2 份 cas", d["ok"] and len(d["data"]) == 2
          and sorted(x["cas_name"] for x in d["data"]) == ["a.cas", "b.cas"]
          and all(x["name"] == "扫描到的影片.mkv" and x["kind"] for x in d["data"])
          and all(base64.b64decode(x["cas_b64"]) == SCAN_CAS for x in d["data"]), d)
    st, d, _ = req("POST", base + "/api/scan", {"path": "/扫描", "recursive": False})
    check("扫描不递归只找到 1 份", d["ok"] and len(d["data"]) == 1, d)

    # l) 设置:主目录读写
    st, d, _ = req("GET", base + "/api/settings")
    check("设置默认为空", d["ok"] and d["data"]["tianyi_home"] == "" and d["data"]["mobile_home"] == "", d)
    st, d, _ = req("POST", base + "/api/settings",
                   {"tianyi_home": "/TV/天翼云盘在线/", "mobile_home": "/TV/移动云盘"})
    check("设置保存(尾斜杠归一)", d["ok"] and d["data"]["tianyi_home"] == "/TV/天翼云盘在线"
          and d["data"]["mobile_home"] == "/TV/移动云盘", d)
    st, d, _ = req("GET", base + "/api/settings")
    check("设置回读一致", d["ok"] and d["data"]["mobile_home"] == "/TV/移动云盘", d)
    st, d, _ = req("POST", base + "/api/settings", {"tianyi_home": "相对路径", "mobile_home": ""})
    check("设置拒绝无斜杠路径", not d["ok"], d)
    st, d, _ = req("POST", base + "/api/settings",
                   {"tianyi_paths": ["/TV/天翼云盘在线/", "/天翼云盘", "/TV/天翼云盘在线"],
                    "mobile_paths": "/TV/移动云盘\n/移动云盘"})
    check("云盘路径清单保存(去重/归一)", d["ok"]
          and d["data"]["tianyi_paths"] == ["/TV/天翼云盘在线", "/天翼云盘"]
          and d["data"]["mobile_paths"] == ["/TV/移动云盘", "/移动云盘"], d)
    st, d, _ = req("POST", base + "/api/settings", {"tianyi_paths": ["相对路径"]})
    check("云盘路径拒绝无斜杠", not d["ok"], d)

    # n) 配置存后台:页面设置优先、密钥不回显、口令可设可清
    st, d, _ = req("POST", base + "/api/settings",
                   {"openlist_url": "http://127.0.0.1:%d" % ol_port,
                    "cas139_phone": "13900001111", "openlist_token": "settings-token-xyz"})
    check("设置保存网盘配置", d["ok"] and d["data"]["openlist_url"].endswith(str(ol_port))
          and d["data"]["cas139_phone"] == "13900001111" and d["data"]["openlist_token_set"] is True, d)
    check("设置不回显密钥明文", "openlist_token" not in d["data"] and "cas139_auth" not in d["data"]
          and "web_pw_hash" not in d["data"], d["data"])
    st, d, _ = req("GET", base + "/api/status")
    check("用页面配置仍连通 OpenList", d["ok"] and d["data"]["openlist_ok"] is True, d)
    st, d, _ = req("POST", base + "/api/settings", {"web_password": "pw-from-settings"})
    check("设置页可设访问口令", d["ok"] and d["data"]["web_password_set"] is True, d)
    st, d, _ = req("GET", base + "/api/status")
    check("设口令后未登录被拒(后台口令)", st == 401, st)
    st, d, hdrs = req("POST", base + "/api/login", {"password": "pw-from-settings"})
    cookie2 = hdrs.get("Set-Cookie", "")
    check("后台口令可登录", d["ok"] and "cas_sess=" in cookie2, d)
    st, d, _ = req("POST", base + "/api/settings", {"clear_web_password": True},
                   headers={"Cookie": cookie2.split(";")[0]})
    check("可清除后台口令", d["ok"] and d["data"]["web_password_set"] is False, d)
    st, d, _ = req("GET", base + "/api/status")
    check("清除口令后恢复免登录", st == 200 and d["ok"], d)

    # o) Token 保存前校验:坏 token 不落盘、Bearer 前缀自动剥离
    st, d, _ = req("POST", base + "/api/settings", {"openlist_token": "bad-token"})
    check("无效 token 被保存校验拒绝", not d["ok"] and "token is invalidated" in d["msg"], d)
    st, d, _ = req("GET", base + "/api/settings")
    check("拒绝后旧 token 仍在生效", d["ok"] and d["data"]["openlist_token_set"] is True
          and d["data"]["openlist_token_source"] == "页面设置", d)
    st, d, _ = req("POST", base + "/api/settings", {"openlist_token": "Bearer settings-token-xyz"})
    check("粘贴带 Bearer 前缀可自动剥离", d["ok"], d)

    # i) 口令保护
    cas_web.WEB_PASSWORD = "pw123"
    st, d, _ = req("GET", base + "/api/status")
    check("设口令后未登录被拒", st == 401, st)
    st, d, _ = req("POST", base + "/api/login", {"password": "wrong"})
    check("错误口令被拒", d["ok"] is False)
    st, d, hdrs = req("POST", base + "/api/login", {"password": "pw123"})
    cookie = hdrs.get("Set-Cookie", "")
    check("正确口令发 cookie", d["ok"] and "cas_sess=" in cookie, cookie)
    st, d, _ = req("GET", base + "/api/status", headers={"Cookie": cookie.split(";")[0]})
    check("带 cookie 可访问", st == 200 and d["ok"])
    cas_web.WEB_PASSWORD = ""

    print("ALL PASS")


if __name__ == "__main__":
    main()
