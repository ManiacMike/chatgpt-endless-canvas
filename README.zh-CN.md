# ChatGPT Endless Image Gen（无限画板）

**[English →](README.md)**

基于**网页版 ChatGPT** 的无限画板生图工具——不走付费 API。通过 Chrome
DevTools Protocol（CDP）驱动你自己已登录的 chatgpt.com 会话生成图片，每张
结果实时落到本地可缩放画布上，并配齐 Lovart 式迭代能力：

- 🎨 **无限画布** —— 缩放、平移、拖卡片，布局按画布持久化
- ✏️ **标注改图** —— 在图上画框/打点写修改意见，一键重生成；server 自动把
  原图作为参考图重新发给 ChatGPT
- ⭐ **风格参考生图** —— 以画板上任意图为风格锚点，生成同画风的新内容
- 📋 **计划任务批量生图** —— 提交 prompt 列表，任务串行执行、每张之间随机
  间隔 30–120 秒（拟人节奏，避免限流）
- 🌳 **谱系追踪** —— 每张生成图记录父图与当时的修改意见，画布上自动画出
  家族树（实线=改图，虚线=风格参考）
- 🗂 **多画布** —— 每个画布就是一个普通目录（图片 + `layout.json` +
  `annotations.json` + `lineage.json`），可整体拷走、可打开任意目录
- 📥 **拖拽导入** —— 本地图片直接拖进画布，参与标注/参考/谱系
- 🤖 **内置 Claude Code skill** —— 让 Claude 帮你提交 prompt、批量任务和改图

画板 server 是**纯 Python 标准库**；生图脚本只需 `pip install playwright`
（不需要 `playwright install` 下载浏览器——它附着你自己的 Chrome）。

## 工作原理

```
board.html  ──►  board_server.py  ──►  generate_chatgpt_image.py  ──►  Chrome (CDP :9222)
 （画布）        （队列/画布/谱系/上传）  （发 prompt+参考图，等待并下载 PNG）    └─ 你已登录的
                                                                        chatgpt.com 会话
```

不需要 API key、不需要复制 cookie：在专用 Chrome profile 里登录一次
chatgpt.com 即可复用。生成过程中 ChatGPT 会渐进渲染中间帧——脚本会等到
最终存储 URL 稳定才抓取，并且排除 prompt 发送前页面上已存在的所有图片
（保证不会误抓上一次会话的旧图）。

## 快速开始

```bash
git clone https://github.com/<you>/chatgpt-endless-image-gen.git
cd chatgpt-endless-image-gen

# 1. 一次性：装依赖（仅 playwright）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 启动调试 Chrome，在窗口里登录 chatgpt.com，保持开着
bash launch-chrome-debug.sh

# 3. 启动画板（幂等；nohup 脱离终端存活）
bash start.sh                      # → http://127.0.0.1:8090
```

数据在 `~/Documents/chatgpt-endless-image-gen/`：`boards.json` 是画布注册表；
每个画布目录完全自包含、可移植。

## 命令行直接生图（不走画板）

```bash
.venv/bin/python generate_chatgpt_image.py \
  --prompt "a cozy cabin in snow, watercolor" \
  --output out.png \
  [--reference style.png] [--timeout 240] [--grab-only]
```

`--grab-only` 重抓 ChatGPT 页面上已有的最新图——脚本超时但页面其实已出图时用。

## HTTP API

| 接口 | 说明 |
|---|---|
| `GET /api/state` | board/images/layout/annotations/lineage/jobs 一次拿全 |
| `POST /api/generate` | `{"prompt":"..."}` 纯生成；加 `"name":"<图名>"` 以该图为风格参考；`"prompts":[...]` 计划任务批量（≤50，随机间隔） |
| `POST /api/regenerate` | `{"name":"<图名>"}` 按待执行标注改图 |
| `POST /api/annotations` | 写入完整标注对象（0-1 归一化坐标） |
| `POST /api/upload?name=x.png` | body 为图片字节 → 当前画布 |
| `GET/POST /api/boards` | 列出 / `{"action":"create","name":..,"dir":..}` / `{"action":"open","id":..}` |
| `POST /api/layout` | 持久化卡片位置（`?board=<id>` 防止切画布时串写） |

## Claude Code skill

`skills/chatgpt-image/SKILL.md` 教 Claude Code 跑通整个闭环——检查 Chrome、
启动画板、通过 API 提交单张/批量/改图、汇报结果。安装：

```bash
mkdir -p ~/.claude/skills
cp -r skills/chatgpt-image ~/.claude/skills/
```

如果 clone 位置不是 `~/Workspace/chatgpt-endless-image-gen`，在 shell 配置里
设 `CHATGPT_IMAGE_GEN_HOME=/你的/clone/路径`（skill 会优先读它）。

## 配置

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `BOARD_PORT` | `8090` | 画板端口 |
| `IMAGE_GEN_DATA` | `~/Documents/chatgpt-endless-image-gen` | 数据根目录 |
| `CHATGPT_CDP_URL` | `http://127.0.0.1:9222` | Chrome 调试地址 |
| `BATCH_INTERVAL` | `30-120` | 批量任务随机间隔（秒） |
| `BOARD_DIR` | — | 单画布固定目录模式 |
| `DEBUG_PORT` / `PROFILE_DIR` / `CHROME_BIN` | — | launch-chrome-debug.sh 参数 |

## 限制与说明

- ChatGPT 网页 UI 经常改版，`generate_chatgpt_image.py` 里的选择器做了防御
  性 fallback，但大改版后可能需要更新。
- "改图"是参考图 + 方位化文字意见的**整图重生成**，不是真正的局部重绘。
- 连续生图可能触发 ChatGPT 静默限流（prompt 发出无响应）——批量任务的随机
  间隔就是为此设计的，遇到时等一段时间重试。
- **请使用自己的账号、仅作个人用途、风险自负。** 自动化操作 ChatGPT 网页
  可能与 OpenAI 使用条款冲突。

## 许可

[MIT](LICENSE)
