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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 测试数据:11MiB 确定性内容(跨 10MiB 分片边界) ----
PATTERN = b"0123456789abcdef"
CONTENT = (PATTERN * (11534336 // len(PATTERN) + 1))[:11534336]
FNAME = "测试电影.mkv"
FPATH = "/天翼云盘/" + FNAME

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
            if body.get("path") == "/":
                content = [{"name": "天翼云盘", "size": 0, "is_dir": True},
                           {"name": "移动云盘", "size": 0, "is_dir": True}]
            elif body.get("path") == "/天翼云盘":
                content = [{"name": FNAME, "size": len(CONTENT), "is_dir": False}]
            else:
                content = []
            return self._json({"code": 200, "data": {"content": content, "total": len(content)}})
        if self.path == "/api/fs/get":
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
        if self.path == "/raw":
            self.send_response(200)
            self.send_header("Content-Length", str(len(CONTENT)))
            self.end_headers()
            self.wfile.write(CONTENT)
            return
        self.send_response(404)
        self.end_headers()


class Mock139(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(ln)
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
    ct.FILE_CREATE_URL = "http://127.0.0.1:%d/hcy/file/create" % mock_139.server_address[1]

    web = ThreadingHTTPServer(("127.0.0.1", 0), cas_web.Handler)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % web.server_address[1]

    # a) 首页
    st, html, _ = req("GET", base + "/", raw=True)
    check("首页可打开", st == 200 and "CAS 双云工具".encode() in html)
    check("首页含目录选择器与保存勾选", all(x in html for x in
          ["选目录".encode(), b'id="picker"', b'id="savecas"', b"pickerPick", b"loadPicker"]))
    check("首页含批量/zip 支持", all(x in html for x in
          [b"multiple", b"unzipcas", b"batchItems", b"/api/exists"]))

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
    cas_file = job["result"]["cas_file"]
    check("gen 结果记录上传路径", job["result"].get("uploaded_to") == FPATH + ".cas", job["result"])
    with open(os.path.join(cas_web.LIB_DIR, cas_file), "rb") as f:
        cas_bytes = f.read()
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

    # h) cas 库:列表/下载/还原/删除
    st, d, _ = req("GET", base + "/api/lib")
    check("cas 库有刚生成的文件", any(it["file"] == cas_file for it in d["data"]), d)
    st, data, hdrs = req("GET", base + "/api/lib/download?name=" + urllib.parse.quote(cas_file), raw=True)
    check("cas 库下载内容一致", data == cas_bytes)
    st, d, _ = req("POST", base + "/api/lib/restore", {"file": cas_file, "to": "mobile", "parent": "/"})
    check("从库还原到移动", d["ok"], d)
    st, d, _ = req("POST", base + "/api/lib/delete", {"file": cas_file})
    check("从库删除", d["ok"])
    st, d, _ = req("GET", base + "/api/lib")
    check("删除后库为空", len(d["data"]) == 0)

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
