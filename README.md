📧 HeliacMail · 多邮箱 AI 助手
=====================

一个**常驻本机**的邮箱智能助手：浏览器网页配置邮箱 → 自动监控收件箱 → **AI 分析邮件** → 生成**待办** → **手机推送**（ntfy）→ 可选 **Vikunja 双向同步**。

> 不配置 AI 也能用：内置关键词模式自动提取待办。

---

## ✨ 功能

- **多邮箱**：QQ / 163 / Gmail 等任意 IMAP 邮箱，一个面板统一管理
- **AI 邮件分析**：OpenAI 兼容接口（DeepSeek / Kimi / 通义 / GLM / OpenAI…），自动分类 + 摘要 + 提取待办
- **关键词兜底**：没配 AI 时，按关键词（待办/需要/请…）自动提取待办
- **手机推送**：ntfy.sh，无需注册，Android/iOS 装个 App 订阅主题即可
- **待办管理**：网页勾选完成 / 删除，自动同步到手机
- **Vikunja 同步**（可选）：一键部署本地 Vikunja 或接远程实例，待办双向同步
- **Cloudflare 隧道**（可选）：一键获得公网 https 地址，出门在外也能用
- **隐私**：默认仅本机访问（127.0.0.1），可设访问令牌；数据全在本地

---

## 🚀 快速开始（源码运行）

**Windows**：

```bat
双击 run.bat
```

首次自动装依赖并打开 http://localhost:8090 （如需修改端口，编辑 `config.yaml` 的 `server.port`）。

**手动（任意系统）**：

```bash
pip install -r requirements.txt
python app.py
```

打开 http://localhost:8090 ，按页面引导添加邮箱、配置 AI/推送。

---

## 📦 Windows 免安装版（exe）

发布页提供单文件 exe：下载 **HeliacMail.exe**，双击即可运行（无需安装 Python）。

- 配置 / 数据 / 日志保存在 **exe 同目录**（config.yaml、logs/ 等），更新版本时请保留这些文件
- 首次运行自动生成默认配置，浏览器自动打开 http://localhost:8090
- 可放任意目录；右键可创建桌面快捷方式

> 提示：杀毒软件可能对 PyInstaller 打包的程序误报，添加信任即可。

---

## ⚙️ 配置

程序首次运行自动生成 `config.yaml`（也可参照 `config.example.yaml`）：

| 段 | 说明 |
| --- | --- |
| `server` | 监听地址 / 端口 / 访问令牌 |
| `monitor` | 轮询间隔、每轮上限、是否标记已读、回看天数 |
| `ai` | 是否启用、语言、偏好、分类权重、providers |
| `notify` | ntfy 服务器 / 主题 / 优先级 / 跳过分类 |
| `sync` | Vikunja 地址 / Token / 项目 / 优先级 |
| `todo` | 关键词列表 |

### 邮箱授权码

- **QQ 邮箱**：设置 → 账户 → 开启 IMAP/SMTP → 生成授权码（不是 QQ 密码）
- **163 邮箱**：设置 → POP3/SMTP/IMAP → 开启 → 客户端授权密码
- **Gmail**：Google 账号 → 安全性 → 开启两步验证 → 应用专用密码

### AI Provider 示例

| 服务 | Base URL | Model 示例 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |

---

## 🧪 测试

```bash
python tests/run_tests.py     # 单元测试
python tests/smoke_sanity.py  # 冒烟：核心模块
python tests/smoke_web.py     # 冒烟：Web 服务
```

---

## 🔐 安全

- 密码 / API Key **明文**存在 `config.yaml`（已在 `.gitignore` 忽略），请勿提交或外传
- 默认只监听 `127.0.0.1`；改为局域网访问前，务必在 `server.auth_token` 设置访问令牌
- IMAP 凭据通过 TLS 发送给邮箱服务商

---

## 📄 许可

MIT License（见仓库 LICENSE，如无则保留版权声明）。
