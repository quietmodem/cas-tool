# CAS 双云工具 — 天翼 / 移动(139)云盘双支持 .cas

两个形态,同一套核心:

- **Web 后台(推荐)**:`cas_web.py` + Docker 部署在群晖上,手机浏览器打开就能
  浏览网盘、生成 cas、上传/还原 cas。网盘访问走你现有 OpenList 的接口
  (地址+令牌复制过来即可,等于复用 OpenList 里的全部挂载)。
- **命令行**:`cas_tool.py`,在电脑/NAS 终端里直接用。

纯 Python 3 标准库,无第三方依赖。

## Docker 部署(Web 后台)

从 GitHub 拉取(群晖 SSH):

```bash
cd /volume1/docker
git clone https://github.com/quietmodem/cas-tool.git
cd cas-tool
# 后续更新: git pull 后 docker compose up -d --build 即可
```

或手动:把 `cas_tool.py`、`cas_web.py`、`Dockerfile`、`docker-compose.yml`
放进群晖同一个目录(如 `/volume1/docker/cas-tool/`)。然后:

1. 改 `docker-compose.yml` 里的 `OPENLIST_URL` / `OPENLIST_TOKEN`
   (复制你 OpenList 的地址和后台令牌),建议顺手填上 `WEB_PASSWORD` 页面口令;
   移动还原要用再填 `CAS139_PHONE` / `CAS139_AUTH`
2. 群晖 Container Manager 里以「项目」方式导入,或 SSH 下 `docker compose up -d --build`
3. 手机 Safari 打开 `http://群晖IP:8099`,可添加到主屏幕当 App 用

页面功能(四块):

- **还原 cas**:选 .cas 文件(或粘贴 Base64 文本)→ 解析看类型 → 一键还原到移动 / 天翼
- **浏览网盘生成 cas**:按 OpenList 挂载逐层浏览,对任意文件点「生成 cas」,
  后台流式读一遍算哈希(带进度),cas 存进服务端 cas 库;勾选的话还会把 .cas
  上传回网盘里原文件旁边
- **cas 库**:群晖上存着的 cas 随时下载 / 还原 / 删除
- **任务**:生成进度实时刷新

生成的 cas 是服务端算的,手机只发指令,大文件也不耗手机流量和电量
(流量走的是群晖 ↔ OpenList 那一段局域网/服务端链路)。

一份 `.cas` 是一个容器(Base64 编码的紧凑 JSON),字段固定为:
`name / size / md5 / sliceMd5 / sha256 / recover_pass / create_time`。
天翼版填 `md5`+`sliceMd5`,移动版填 `sha256`,谁的那组不用谁留空。
本工具生成的也是这个统一格式,和网友流通的双云 cas 完全同构(已和真实样品逐字段对比)。

## 命令一览

```bash
# 1) 解析一份 cas,自动判断是天翼版还是移动版
python3 cas_tool.py inspect 某文件.cas

# 2) 给本地文件生成 cas(默认 both:两组哈希都填,两家都能认)
python3 cas_tool.py gen 电影.mkv
python3 cas_tool.py gen 电影.mkv --target mobile    # 只填 sha256(和网友移动版一模一样)
python3 cas_tool.py gen 电影.mkv --target tianyi    # 只填 md5/sliceMd5
python3 cas_tool.py gen 电影.mkv --out D:/out.cas --recover-pass 口令

# 3) 拿本地原文件校验一份 cas 是不是这个文件的
python3 cas_tool.py verify 某文件.cas 电影.mkv

# 4) 还原:按 cas 内容自动分流(双填版需 --to 指定去向)
python3 cas_tool.py restore 某文件.cas                          # 单边版自动分流
python3 cas_tool.py restore 某文件.cas --to mobile              # 双填版 -> 移动
python3 cas_tool.py restore 某文件.cas --to tianyi --target /天翼云盘/电影
# 也可以直接指定分支:
python3 cas_tool.py restore-139 某文件.cas [--parent 目录fileId] [--dry-run] [--cleanup-on-miss]
python3 cas_tool.py restore-189 某文件.cas --target /天翼云盘/电影 [--dry-run]
```

所有还原命令都支持 `--dry-run`:只打印将要发送的请求,不真的发,先核对再动手。

## 还原前要配的环境变量(凭据只走环境变量,不落盘、不打印)

**移动云盘(restore-139):**

```bash
export CAS139_PHONE=你的手机号
export CAS139_AUTH='Basic xxxxx'
```

`CAS139_AUTH` 的获取:浏览器登录 <https://yun.139.com> → F12 → 网络 → 刷新后点任一请求 →
请求头里的 `Authorization`(形如 `Basic xxxxx`,整段填或只填 xxxxx 部分都行;
也兼容青龙脚本的 `YDYP_PHONE` / `YDYP_AUTH` 变量名)。

原理:向 139 新个人云接口 `file/create` 提交 `contentHash=sha256`,
服务端池里有这个文件就直接建檔(秒传命中);没有就返回上传分片计划——
本工具**只做秒传,不会上传文件本体**,未命中会明确告诉你,并可用
`--cleanup-on-miss` 清掉未完成占位。

**天翼云盘(restore-189,经你已部署的 OpenList):**

```bash
export OPENLIST_URL=http://192.168.111.x:5244
export OPENLIST_TOKEN=你的OpenList令牌
```

令牌在 OpenList 后台「设置 → 其他 → 令牌」。工具会先调
`/api/fs/seed/capabilities` 预检,再调 `/api/fs/seed/rapid_upload` 秒传保存到
`--target` 指定的天翼挂载目录。

## 生成规则(已核准来源)

- 天翼 `sliceMd5`:分片大小由文件大小决定(≤~9.77GiB 用 10MiB,≤~19.5GiB 用 20MiB,
  更大按 1999 片上限向上取 10MiB 整数倍、最小 50MiB);单片时 `sliceMd5=整文件MD5`,
  多片时 `= MD5(逐片MD5大写以换行拼接)`。与 OpenList 189pc 驱动
  `partSize` / `torrent.CASSliceSize` 的实现一致,已用独立脚本对 11MiB 文件复算验证。
- 移动 `sha256`:整文件 SHA256 小写十六进制,即 139 接口的 `contentHash`。
- 计算只读文件一遍,同时算完 MD5/逐片 MD5/SHA256;82GiB 这种大文件在 NAS 上
  纯粹是读盘速度问题,内存占用只有一片大小。

## 关于"网盘里的文件生成 cas"

哈希必须读到文件字节才能算:移动云盘文件列表不给任何哈希,天翼列表只给整文件
MD5、给不了分片 MD5,所以**任何**工具想给网盘里的文件生成 cas,都得先把文件
读一遍(下载到本地/NAS)。实操:用 OpenList 把文件下载到群晖,再对本地路径
`gen` 即可;或把网盘用 rclone/WebDAV 挂成路径,直接对挂载路径 `gen`(效果相同,
就是走网络读一遍)。

## 已验证 / 未验证(如实标注)

已在本机实测通过:

- 解析你提供的移动版样品 cas(奥德赛 REMUX,字段、时间、大小全对)
- gen 三种 target 的输出与样品**字段顺序和结构逐项一致**
- 小文件/11MiB 文件的 md5、sha256 与系统 `md5sum`/`sha256sum` 对齐;
  多片 sliceMd5 与独立复算一致;分片档位边界值全对
- verify 对被改过的文件能正确报出不一致;拿天翼 cas 去走移动分支会被拒绝,反之亦然
- 两个还原分支的 `--dry-run` 输出的请求体与接口要求逐项核对无误

Web 后台已用 mock OpenList + mock 139 接口做完整端到端测试
(`python3 test_web.py`,26 项全过):浏览、生成(哈希与独立计算一致)、
上传回网盘、两种还原的请求载荷、cas 库增删、口令登录,都验证无误。
本机环境没有 Docker,镜像没有实际 build 过;Dockerfile 只是
`python:3.12-alpine` + 拷贝两个 .py + 启动已实测可运行的 `cas_web.py`,
compose 文件已做 YAML 语法校验,群晖上 build 失败的话把报错发我。

尚未用真实账号实测(需要你的凭据,我这边没有):

- restore-139 的真实命中:接口、签名(Mcloud-Sign)、请求头均按公开实测脚本
  deeplee/personal-scripts 的 `139_yunduo.py` 移植,那份脚本是在真实账号上
  跑上传任务的;但移动版 cas 还原这条路径第一次真发时,请先拿一个小文件 cas 试。
- restore-189 的真实保存:接口按 OpenList 官方种子文档
  (`/api/fs/seed/capabilities`、`rapid_upload`)实现。提醒一句你已知的老情况:
  OpenList 主线对分片信息不足的旧 cas 可能在预检就报
  `target driver cannot reuse the available hashes`,那种文件继续用你验证过的
  油猴脚本还原即可;本工具 gen 出来的 cas 带完整聚合 sliceMd5,属于新格式。

版本: CLI v1.0.0 / Web v1.0.0(2026-11-11)
