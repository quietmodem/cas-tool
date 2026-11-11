# CAS 双云工具 — 天翼 / 移动(139)云盘双支持 .cas

两个形态,同一套核心:

- **Web 后台(推荐)**:`cas_web.py` + Docker 部署在群晖上,手机浏览器打开就能
  浏览网盘、生成 cas、上传/还原 cas。网盘访问走你现有 OpenList 的接口
  (地址+令牌复制过来即可,等于复用 OpenList 里的全部挂载)。
- **命令行**:`cas_tool.py`,在电脑/NAS 终端里直接用。

纯 Python 3 标准库,无第三方依赖。

## Docker 部署(Web 后台)—— 一个 YAML 文件搞定

镜像由 GitHub Actions 每次推送自动构建到 GHCR,部署端**不用源码、不用构建**:

1. 只下载这一个文件:
   `https://raw.githubusercontent.com/quietmodem/cas-tool/main/docker-compose.yml`
   (或把仓库里的 `docker-compose.yml` 内容整段复制出来),放到群晖任意目录
   (如 `/volume1/docker/cas-tool/`)
2. 改文件里 `OPENLIST_URL` / `OPENLIST_TOKEN`(复制你 OpenList 的地址和后台令牌),
   建议顺手填上 `WEB_PASSWORD` 页面口令
3. 群晖 Container Manager 里以「项目」方式选这个文件启动,或 SSH 下 `docker compose up -d`
4. 手机 Safari 打开 `http://群晖IP:8099`,可添加到主屏幕当 App 用
5. 更新版本:`docker compose pull && docker compose up -d` 就完事

开发者改代码走 git: `git clone https://github.com/quietmodem/cas-tool.git`,
本地构建用仓库里的 Dockerfile。

页面功能(四块):

- **还原 cas**:选 .cas 文件(或粘贴 Base64 文本)→ 解析看类型 → 还原到移动 / 天翼;
  另有「经 OpenList 还原」:把 cas 交给 OpenList 的种子秒传保存,目标目录可以是
  任意挂载 —— 移动云盘如果也挂在 OpenList 上,可以用它试还原到移动挂载,
  成不成以 OpenList 的预检结果为准(走通了就不用再配 139 直调凭据)
- **浏览网盘生成 cas**:按 OpenList 挂载逐层浏览,对任意文件点「生成 cas」,
  后台流式读一遍算哈希(带进度);完成后在任务里直接下载 .cas,勾选的话还会
  把 .cas 上传回网盘里原文件旁边(v1.5.0 起不再存服务端本地库)
- **任务**:生成进度实时刷新,完成后可下载 .cas

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
(`python3 test_web.py`):浏览、生成(哈希与独立计算一致)、
上传回网盘、三种还原的请求载荷、回查、设置、口令登录,都验证无误。
Docker 镜像由 GitHub Actions 构建并推到 `ghcr.io/quietmodem/cas-tool:latest`
(amd64 + arm64),部署端只拉镜像。

尚未用真实账号实测(需要你的凭据,我这边没有):

- restore-139 的真实命中:接口、签名(Mcloud-Sign)、请求头均按公开实测脚本
  deeplee/personal-scripts 的 `139_yunduo.py` 移植,那份脚本是在真实账号上
  跑上传任务的;但移动版 cas 还原这条路径第一次真发时,请先拿一个小文件 cas 试。
- restore-189 的真实保存:接口按 OpenList 官方种子文档
  (`/api/fs/seed/capabilities`、`rapid_upload`)实现。提醒一句你已知的老情况:
  OpenList 主线对分片信息不足的旧 cas 可能在预检就报
  `target driver cannot reuse the available hashes`,那种文件继续用你验证过的
  油猴脚本还原即可;本工具 gen 出来的 cas 带完整聚合 sliceMd5,属于新格式。

版本: CLI v1.0.0 / Web v1.5.0(2026-11-11)。Web v1.5.0:按使用反馈大改——
① 移除 cas 库(不再存群晖本地):生成的 .cas 改为在任务里直接下载,还原的
.cas 仍旁存到视频同目录;② 还原回查加固:轮询约 30 秒、强制刷新目录列表、
名字大小写不敏感,修掉"文件已落盘却显示未确认、旁存没跟上";③ 目录选择器
重做:面包屑一键回跳、只列文件夹、关键字筛选、主目录直达、确认栏置顶;
④ 界面整体美化:分段式选项卡(吸顶)、卡片/按钮/输入框重新设计。
Web v1.4.0:界面改为三个
选项卡——「cas 还原」(含 cas 库)、「生成 cas」(含任务)、「设置」;设置页
可保存天翼/移动主目录(存服务端 data/settings.json,全设备共用),还原时
目标留空自动按按钮取对应主目录,生成页可一键跳到主目录,目录选择器全页共用。
Web v1.3.1:经 OpenList 的
还原在确认视频已落盘后,可把 .cas 作为侧车存到目标目录视频旁边
(<视频名>.cas,默认开;cas 库的还原同样支持)。另注:OpenList 主线种子
秒传的驱动清单里没有 139,移动云盘无法经 OpenList 种子还原(上游未实现),
移动请用直调 139 接口(需 CAS139_PHONE/AUTH)。
Web v1.3.0:还原区支持多选
.cas 批量解析/批量还原,也可上传含多个 .cas 的 .zip(服务端解包);还原
只进所选的那一个目标目录,不填目录 = OpenList 根目录 /;经 OpenList 的
还原提交后会回查目标目录确认文件真的出现(提交≠成功,不再假报 ✓)。
Web v1.2.0:还原区目标目录
可点「📁 选目录」逐级浏览挂载文件夹点选(不用手敲路径);新增「还原时同时
把这份 .cas 存入 cas 库」勾选(默认开);cas 库里的还原也优先用该目录。
Web v1.2.1:「生成后把 .cas 上传回同目录」改为默认勾选——生成的 cas 会
自动落到源文件旁边,库里那份只作手机端的索引/备份。
