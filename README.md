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
2. 文件不用改任何内容,群晖 Container Manager 里以「项目」方式选这个文件
   启动,或 SSH 下 `docker compose up -d`
3. 打开 `http://群晖IP:8099`,到「设置」页填 OpenList 地址/Token(和可选的
   移动凭据、页面口令),配置存服务端,以后都不用碰 yaml
4. 手机 Safari 打开同一地址,可添加到主屏幕当 App 用
5. 更新版本:`docker compose pull && docker compose up -d` 就完事

开发者改代码走 git: `git clone https://github.com/quietmodem/cas-tool.git`,
本地构建用仓库里的 Dockerfile。

页面功能(四块):

- **还原 cas**:选 .cas 文件(或粘贴 Base64 文本)→ 解析看类型 → 点一个按钮就还原,
  只有两个按钮:「还原到天翼云盘」(经 OpenList 种子秒传)、「还原到移动云盘」
  (直调 139 接口;OpenList 主线不支持移动种子秒传,所以移动只能走这条)。
  解析出的多份 cas 列表顶部有「全选」复选框,取消勾选的不还原。
  目标目录留空时按按钮自动用对应主目录(设置页里配)。
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

版本: CLI v1.0.0 / Web v2.2.0(2026-11-11)。Web v2.2.0:设置页重组为
「☁️ 天翼云盘」(接入方式 OpenList:地址/Token + 挂载目录 + 额外目录)
与「📱 移动云盘」(接入方式直连:手机号/AUTH/密码登录回退 + 挂载目录 +
直连主目录 + 额外目录 + 文件夹 ID 对照)两区,口令移到「页面与安全」;
原「主目录」字段即挂载目录(数据不变)。首页目录选择器(还原目标、
扫描目录)改为限定模式:先选云盘挂载目录(两边都设了才有选择层),
只能在挂载目录及其子目录内选,面包屑以挂载目录为根、不能上溯;
设置页内的选目录仍可自由浏览(挂载目录本身要在那里选)。生成页直达
按钮文案同步改为挂载目录。
Web v2.1.2:「立即刷新
移动授权」按钮改为先保存设置再刷新(填完三项直接点按钮即可,不必先
单独保存);未配置回退时报错写明要复制哪三项;刷新失败但现有授权尚未
过期时,按钮返回兜底说明(有效到哪天、暂时不受影响),不再只报一句失败。
Web v2.1.1:新增「移动主
目录(直连)」——还原页的 139 直连选目录弹窗里多一个「设为主目录」,
按文件夹 ID 保存;设了之后移动还原目标留空直接按 ID 落主目录,不再
经 OpenList 路径解析;生成页直连浏览加「⭐ 主目录」一键跳。原 OpenList
路径版移动主目录保留,只剩浏览跳转与目录归属校验两个用途。
Web v2.1.0:移动「密码登录
回退」,移植 OpenList 139 驱动的鉴权机制——设置页新增与 OpenList 同名
同格式的三项(Username / Password / MailCookies,外加 SmsCode),可把
OpenList 里 139 存储的配置原样复制过来。授权到期 15 天内自动调
authTokenRefresh 刷新,刷新失败回退登录(先邮箱 Cookie 快速登录、再
账号密码三步登录:mail.10086.cn 密码登录 → artifact → thirdlogin 双层
AES),新授权与合并后的邮箱 Cookie 自动回存设置;设置页可看授权到期日,
并有「立即刷新移动授权」按钮。登录触发短信风控时验证码发到手机,填
SmsCode 再点刷新即可。AES 为内置纯 Python 实现(NIST 向量校验),镜像
无新依赖。天翼直连顺延后续版本。
Web v2.0.0:双云直连阶段一——
移动云盘全直连(不再经 OpenList):生成页可切换「OpenList 网盘 / 移动云盘
(直连)」,直连模式经 139 的 file/list 浏览(条目自带 fileId)、
file/getDownloadUrl 取直链流式生成 cas;还原页新增「从移动云盘选目录
(直连)」弹窗,按文件夹 ID 选还原落点,彻底绕开 OpenList 路径换算;顶部
状态新增移动直连连通探针(AUTH 失效会直接标红)。接口形状对齐 OpenList
139 驱动源码(personalGetFiles/personalGetLink)。已知限制:直连生成的
cas 暂不支持自动旁存到视频旁(在任务里下载),还原旁存不受影响。
阶段二(天翼 189 直连)后续版本跟进。
Web v1.10.2:移动直调目录解析
再加两层——设置里新增「移动文件夹 ID 对照」(一行 `目录 = fileId`,直调还原
最优先按对照落目录,ID 从 yun.139.com 的 file/list 请求 parentFileId 取);
自动解析新增"父目录列表取子项 id"(顺带修 ol_list 此前把条目 id 丢弃的
问题);全部落空时报错会带上 fs/get 实际给的 id 并指向对照设置。
Web v1.10.1:修移动直调对挂载
根目录的解析——目标是挂载根(如 /TV/移动云盘)时 fs/get 不给 id,旧逻辑直接
报错;现在先用 fs/get 取子目录 id,取不到再查 OpenList 存储列表、用该挂载
的 root_folder_id(没设即 /)。另:还原按钮按已勾选 cas 的类型亮/灭——勾了
天翼 cas 只亮天翼按钮,勾了移动 cas 只亮移动按钮,双填两边都亮(上传区与
扫描区一致)。
Web v1.10.0:界面简化——还原
按钮从三个减到两个(「还原到天翼云盘」「还原到移动云盘」,上传区与扫描区
一致;原来那个「经 OpenList 还原」与天翼按钮是同一条种子接口、且对移动
根本不可用,只增困惑,已去掉,后端路由保留);解析出的批量列表顶部加「全选」
复选框;顺带修:移动还原时目标目录留空改走移动主目录(之前直接落根目录),
且移动还原也纳入目录归属校验;
设置页已保存的密钥(Token/移动 AUTH/页面口令)在框里显示为星号掩码,不动
或清空都算不修改(要删仍用清除勾选),不再是看着像没填的空白框。
Web v1.9.0:移动直调还原优化——
不再默认落移动云盘根目录:与天翼共用目标目录,后端经 OpenList fs/get 把目录
换算成 139 的 fileId 再还原(手填 fileId 仍可覆盖);直调成功后同样按勾选把
.cas 存到目标目录视频旁边。背景:直调接口 2026-11-11 首次真实账号实测即
秒传命中,只是旧逻辑 parent 只认 fileId、留空即根目录,与用户预期的
OpenList 目录对不上。
Web v1.8.1:修「后台填了 Token
反而连不上(token is invalidated)」——页面里存的 Token 若失效会盖住 compose
里可用的那个;现在保存 Token/地址前先实测这组配置,不通直接拒绝保存并说明
原因,粘贴时带 Bearer 前缀自动剥离,设置页可勾选清除页面 Token 回退环境变量,
Token 状态显示来源(页面设置/环境变量)。注:要填 OpenList 后台「设置 → 其他
→ 令牌」的长期令牌,F12 抓的登录 token 是临时的,会过期。
Web v1.8.0:还原前目录校验——
设置页新增天翼/移动「云盘路径」清单(可多个,一行一个,可点选追加;主目录
自动算作本云路径);经 OpenList 还原时,天翼 cas 的目标不在天翼路径下、
移动 cas 的目标不在移动路径下、双填 cas 两边都不在,会先弹窗列出提醒,
确认后才继续;清单留空的云不校验。直调 139 还原不涉及 OpenList 路径,不校验。
Web v1.7.0:① 配置全部搬到
后台:OpenList 地址/Token、移动凭据、页面口令都在「设置」页里配(存服务端
settings.json,口令只存哈希,密钥不回显明文),页面设置优先于环境变量,
环境变量退为兜底(口令忘了可用 compose 里的 WEB_PASSWORD 救急登录);
docker-compose.yml 精简为不用改的固定文件;② 扫描结果改为独立列表先展示,
默认不勾选,用户勾选后在扫描区内直接还原,不再自动塞进上方还原框。
Web v1.6.0:① 目录选择器改为
居中弹窗(修 PC 上点选目录跳到页面顶部的问题),点遮罩可关;② 新增「扫描
网盘目录里的 .cas」:指定目录(可递归子目录,最多 6 层 / 300 份),服务端
逐个下载解析出源文件名/类型/大小,结果并入待还原列表(每份可勾选),
之后用同一组按钮还原到目标目录并逐个回查、旁存。
Web v1.5.0:按使用反馈大改——
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
