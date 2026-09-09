# 📧 HeliacMail — 多邮箱 AI 助手 v2

浏览器配置邮箱，自动监控收件箱，**同时具备两套邮件理解能力**：

- **AI 智能分析**（可选用）：多 provider（OpenAI 兼容协议，可接 DeepSeek / OpenAI / Kimi / 通义 / GLM / Gemini 官方兼容端点 / Ollama 本地），输出 **分类 · 紧急度 · 摘要 · 关键点 · 待办 · 验证码 · 行动链接**，失败自动降级到下一个；
- **关键词待办**（始终保留，AI 关闭或不可用时兜底）：按 `TODO/待办/task…` 关键词提取待办。

并通过 **ntfy 免费推送** 把新邮件摘要与待办清单发到手机（借鉴 mailpilot）。

## 🚀 快速开始

```bash
# 方式一（推荐 Windows）
双击 run.bat    # 首次自动装依赖并打开 http://localhost:8090

# 方式二
py -m pip install -r requirements.txt
py app.py
```

1. **① 邮箱账户**：填邮箱 + 授权码/应用专用密码（QQ/163/Gmail 都不能用登录密码）
2. **② AI 分析（可选）**：勾选启用 → “添加 provider”→ 填 Base URL / 模型 / API Key → 「测试 AI」
3. **③ 推送同步**：手机装 **ntfy**（iOS/Android），填主题名并订阅 → 「测试推送」
4. 返回仪表板，即可看到“最近邮件简报”与“待办事项”

## ✨ 功能

- 多邮箱并发轮询，自动识别 QQ / 163 / 126 / Outlook / Foxmail / Sina / Gmail 的 IMAP
- AI 结构化分析 + 多 provider 自动降级（网络/限流自动切换下一个）
- Prompt 注入加固：正文视为不可信数据，禁止被邮件内容劫持
- 关键词待办兜底生效：启用 AI 时以 AI 判定为准（只把“未来仍需本人做”的事项记待办，其余仅提醒）；AI 不可用时关键词兜底，一条不丢
- **⏱ 邮件时间窗**：新账户首次只读取最近 N 天的未读（设置页可调），不会把几个月/几年前的旧邮件全翻出来生成待办；之后日常监控仍实时处理新邮件
- ntfy 手机推送：新邮件摘要（按紧急度分级）；未接任务软件时待办变化推清单
- **⚡ IMAP IDLE 秒级实时**：每邮箱一条常驻连接，新邮件≈秒达；不支持 IDLE 的邮箱自动退回轮询
- **Vikunja 双向待办同步**：AI/关键词生成的待办自动进手机任务 App（按紧急度设优先级），手机上勾选完成自动同步回本机；此时 ntfy 只提示"已生成新待办"
- **🖼️ 图片邮件识别**：启用支持 Vision 的模型后，邮件内嵌/附件图片会直接交给多模态模型理解
- 邮箱状态管理改进：**UID 水位线 + 已推送表**，不再“先标已读”，崩溃不丢信、不重复推
- todos / recent / state / config 全部**原子化落盘**；`${ENV}` 变量引用密钥避免明文
- 旧版 `config.yaml` 首次启动自动迁移到新结构（账户与密码原样保留）

## 📁 项目结构

```
├── app.py               # 入口：Web 服务 + API + 启动（瘦身版）
├── core/
│   ├── config.py        # 配置默认值/迁移/${ENV}展开/读写
│   ├── state.py         # UID 水位线 + 已推送表（state.json）
│   ├── storage.py       # 原子 JSON 读写
│   ├── mailparse.py     # MIME 解析（GBK/Big5 兼容、HTML 转文本、链接提取）
│   ├── imap_client.py   # IMAP 只读连接 / UID 扫描 / 取信
│   ├── extract.py       # 关键词待办提取（原功能）
│   ├── ai.py            # 多 provider 结构化分析
│   ├── notifier.py      # ntfy 推送 / 待办快照 / 新待办提醒
│   ├── tasksync.py      # Vikunja 双向同步（本地↔远端任务）
│   ├── recent.py        # 最近邮件简报（recent.json）
│   ├── todos.py         # 待办存储（todos.json）
│   └── pipeline.py      # 编排 + 监控调度（Monitor）
├── setup.html           # 设置页（账户 / AI / 推送 / 关键词）
├── dashboard.html       # 仪表板（简报 + 待办 + 同步）
├── config.example.yaml  # 配置模板（含注释）
├── tests/               # 单元测试：py -m unittest discover -s tests
├── run.bat              # 一键启动（前台运行，日志同时在 logs\app.log）
├── requirements.txt
└── utils/               # logger 等
```

## ⚙️ 配置（config.example.yaml 为准）

```yaml
monitor:
  poll_interval: 30        # 轮询秒数
  mark_seen: false         # 处理完是否标已读（默认只读）
  use_idle: true           # IMAP IDLE 秒级实时（不支持自动退回轮询）
  idle_timeout: 300        # IDLE 静默多久兜底扫一次(秒)
  lookback_days: 30        # 新账户首次只读最近 N 天邮件；0=不限
  accounts:
    - email: you@qq.com
      password: ${QQ_AUTH_CODE}   # 也可直接写授权码
todo:
  keywords: [TODO, 待办, task, action item]
ai:
  enabled: true
  language: 中文
  providers:
    - name: deepseek        # openai 兼容协议
      type: openai
      base_url: https://api.deepseek.com/v1
      model: deepseek-chat
      api_key: ${DEEPSEEK_API_KEY}
notify:
  ntfy:
    server: https://ntfy.sh
    topic: my-mail-todo     # 手机订阅这个主题
  skip_categories: [垃圾, 营销推广]
  first_batch_quiet: true   # 首轮存量邮件不逐封推送（只进简报/待办），新邮件照常提醒
sync:                       # 待办同步到真任务软件（Vikunja）
  enabled: false
  backend: vikunja
  vikunja:
    url: ""                 # 例 http://192.168.1.10:3456
    token: ""               # tk_ 令牌（Vikunja 设置→API Tokens）
    project_title: 邮箱待办
```
> AI 想识别邮件里的图片时：在“设置→② AI 分析”勾选该 provider 的 **🖼️ 支持图片识别**，
> 模型需具备 Vision（如 gpt-4o、qwen-vl、gemini、deepseek-vl）。

## ❓ 常见问题

- **收不到手机推送？** ① 在 设置→③推送同步 填主题并“测试推送”；② 手机 ntfy App 订阅同一主题；③ 检查“跳过分类”。
- **一键部署本地 Vikunja 时“永久 Token 生成失败”？** 已修复：Vikunja v2.6 要求新建 API Token 时带 `permissions`(分组→权限) 与 `expires_at`，旧代码按 v1 老格式调用被拒绝(HTTP 412)。现在会自动用官方 v2 接口 `GET /api/v2/routes + POST /api/v2/tokens` 创建 **tk_ 开头、约 20 年有效** 的永久 Token 并写入 `config.yaml`（无需手动复制）。
- **默认账号 & 想自定义账号/密码？** 默认自动创建的 Vikunja 账号是 **`vikunja_admin`**，登录密码为**首次部署时随机生成**，保存在 `vikunja_local\admin.txt`（同时写入 `config.yaml`）。想用别的账号：在 设置→③ 的“Vikunja 登录账号/登录密码”里填你自己的用户名和密码（密码 ≥8 位），点【一键部署/重新配置】即可——会自动创建该账号、生成对应永久 Token 并写入 admin.txt，手机用你填的同一账号密码登录（旧的实例不会被删除）。
- **手机连不上这台 Vikunja？** 依次排查：
  1. 手机与电脑**同一 Wi-Fi**：Vikunja App 登录地址填 `http://电脑局域网IP:3456`（设置页会自动列出可用 IP）；确保 Windows 防火墙放行 3456 端口入站。
  2. 手机与电脑**异地**：见下面「📡 跨网络访问」，推荐 Tailscale。
  3. 登录后若跳回 `127.0.0.1` 说明 Vikunja 的 `publicurl` 还是本机地址：在设置页「📶 手机怎么连」里填实际外部地址并点「应用并重启生效」。
  4. 命令行自查：`Test-NetConnection 电脑IP -Port 3456`（PowerShell）。
- **AI 一直失败？** 先点“测试 AI”看报错；DeepSeek 等端点要求 Base URL 以 `/v1` 结尾；Ollama 本地地址 `http://127.0.0.1:11434/v1`。
- **为什么旧邮件不发？** 新账户首轮只处理“最近 N 天（设置→④ 回溯天数，默认 30）”内的存量未读，并把水位线对齐当前邮箱最大 UID，之后的增量全靠水位线，历史邮件（含旧未读）不会再被翻出来推一遍。若想处理更早的邮件可调大回溯天数（0=不限）。
- **邮件仍显示未读？** 默认只读模式（推荐）。如需处理完自动标已读，在 设置→④ 勾选“标记为已读”。
- **网页打不开？** 确认程序在运行，端口 8090 未被占用；访问 `http://localhost:8090`。
- **待办怎么同步到手机并勾选完成？** 设置→③→点【⚡ 一键部署本地 Vikunja】（官方 Windows exe，免 Docker，自动建号、自动创建永久 API Token 并接入）→ 手机装 Vikunja App，登录地址见下（账号密码见 `vikunja_local\admin.txt`）。生成的待办会按紧急度设置优先级；在手机上勾完成会自动同步回仪表板。
- **安全提醒**：Web 默认只监听 `127.0.0.1`（如需局域网访问 `config.yaml` 里 `server.host` 改 `0.0.0.0`）；`config.yaml` 含明文凭据，已被 `.gitignore` 排除，请勿提交仓库；若之前泄露过授权码请立即到邮箱后台吊销重建。

## 📡 跨网络访问怎么选（手机“异地”连这台电脑）

| 方案 | 免费? | 速度/稳定性 | 手机 App 地址 | 适合 | 一句话点评 |
|---|---|---|---|---|---|
| **Tailscale / ZeroTier** | ✅ | 快(点对点)、稳定 | `http://100.x.y.z:3456` | 手机与电脑都是你的、**长期异地使用** | **首选**：两台设备登录同一账号即自动组网，加密、免配置端口；Windows 装好后在电脑端 `tailscale ip -4` 看 100.x 地址。若一直连不上，多因防火墙/魔法网络冲突，重点检查 1 楼下几条 |
| **Cloudflare Tunnel (cloudflared)** | ✅ | 中（走 CF 边缘）、需联网 | `https://xxx.trycloudflare.com` | 不想在手机装 VPN、偶尔异地看 | **次选**：`cloudflared tunnel --url http://127.0.0.1:3456` 一条命令拿到免费 https 公网地址；临时域名每次重启会变，长期建议命名隧道+自有域名 |
| 云服务器/VPS 自建 | 付费 | 快 | `http://服务器IP:3456` | 数据要 7×24 在线 | 最省心但花钱，适合长期主力 |
| cpolar / frp / ngrok | 部分免费 | 中 | 映射出的域名 | 临时演示 | 免费隧道域名会变/限速 |
| 路由器端口映射 | 免费 | 快 | `http://公网IP:3456` | 有公网 IP | 需运营商给公网 IP + 防火墙放行，暴露面较大不建议长期用 |

**“手机要一直开着 VPN”会不会麻烦？** Tailscale 手机 App 支持“按需连接/仅在需要时启用”并按 Wi-Fi 自动开/关：连自己家 Wi-Fi 时其实走局域网，根本不用 VPN，只有出门在外才经 Tailscale，日常几乎无感、也不明显费电。它是“私有网络”，Vikunja 不暴露到公网，是这几类方案里默认**暴露面最小**的。

**那 Cloudflare 是不是更好？** 它赢在**手机完全不用装任何东西**：cloudflared 从你电脑**出站**连到 Cloudflare 边缘，路由器/防火墙不开放任何入站端口，传输全程 TLS——比 cpolar / ngrok / 端口映射都更稳更安全。代价是你的 Vikunja 由“私有”变为“公网可达”，安全主要靠 Vikunja 登录密码；想更保险可在 Cloudflare 控制台加免费 **Zero Trust Access**，只允许你自己的邮箱访问。结论：个人用，**图省心选 Tailscale，图手机免装 VPN 选 Cloudflare**，两者都是安全的好选择。本工具设置页已支持：连接方式选 **Cloudflare Tunnel** → 点 **☁️ 一键安装并启动隧道**，自动下载 cloudflared、取得 https 地址并写入 Vikunja publicurl（免费 quick tunnel 地址重启会变，固定请用命名隧道+自有域名）。

**Tailscale 一直不成功的常见原因**：① 电脑 Win 防火墙默认拦 3456/41641 → 放行；② 手机与电脑必须登录**同一 Tailscale 账号**；③ 手机用移动流量而非与电脑相同的 Wi-Fi（两台都在同一 Wi-Fi 时 Tailscale 也可能走局域网路径但一样通）；④ 先用手机浏览器打开 `http://100.x:3456` 验证再配 App；⑤ 若公司网/代理拦截 UDP，可开 MagicDNS 或改走 DERP 中继。设置页会自动检测本机 100.x 并在「📶 手机怎么连」里提示。

## 与原版(mailpilot)的关系

本工具保留了“浏览器配置、多中文邮箱、关键词待办”等本地化轻量能力，并吸收 mailpilot 的
多 LLM provider 降级、结构化摘要、注入防护、水位线去重、按分类分级推送、只读不丢信等设计。
差异：mailpilot 是单账户 + IMAP IDLE 常驻的 Go 二进制；本工具是多账户轮询 + Windows 一键运行的 Python 工具。
