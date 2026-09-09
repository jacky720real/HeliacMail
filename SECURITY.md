# 安全与隐私说明

本仓库只包含**源代码与示例配置**，可以公开上传到 GitHub。

## 不会提交的内容（首次运行在本机自动生成，请勿提交）

| 路径/文件 | 说明 |
|---|---|
| `config.yaml` | 你的真实邮箱账号/授权码、AI API Key、Vikunja Token（明文） |
| `state.json` / `todos.json` / `recent.json` | 你的邮件水位线、待办、最近简报 |
| `logs/` | 运行日志（含本机路径、时间与账号提示） |
| `vikunja_local/` | 本地 Vikunja 程序、数据库、`admin.txt` 账号凭据、`config.yml` |
| `.env` | 真实环境变量密钥 |

上述路径均已写入 `.gitignore`，配置请参照 `config.example.yaml` / `.env.example` 中的占位示例
（`you@qq.com`、`${QQ_AUTH_CODE}`、`tk_xxx` 等均为示例，不含任何真实数据）。

## 本地默认账号

- 程序内置**本地默认** Vikunja 账号：`vikunja_admin`；登录密码为**首次部署时随机生成**
  （`secrets.token_urlsafe(12)`），仅用于本地一键部署，生成后写入 `config.yaml` 与
  `vikunja_local\admin.txt`（请妥善保管）。
- 请在自己的实例上，通过设置页「Vikunja 登录账号/登录密码」改为你个人的账号密码
  （填写后点「💾 保存同步设置」会自动创建/重置账号并换发 Token，密码要求 ≥8 位）。

## 所需软件环境（本仓库不做预装/不捆绑第三方程序）

- 运行环境：**Windows 10/11 + Python 3.9+**（安装 Python 时勾选 `Add python.exe to PATH`）。
- Python 依赖（`requirements.txt`）：`PyYAML`、`requests`、`psutil`。
  - 全新克隆且**未安装任何依赖**时，直接双击 `run.bat` 会自动检测并 `pip install -r requirements.txt`；
  - 直接执行 `python app.py` 而缺依赖时，程序会给出明确的安装提示而不是一堆报错。
- 功能需要联网下载的（按需，非预装）：
  - 本地 Vikunja Windows 版（约 40MB，官方 `dl.vikunja.io`）；
  - Cloudflare Tunnel 客户端 `cloudflared.exe`（约 60MB，GitHub Release）；
  - 两者均保存在项目本地文件夹内，不写入系统目录。
- 所有邮箱授权码、AI API Key、Vikunja 账号密码等均由**你自己填写**，只存在本机 `config.yaml`/`.env`。

## 安全建议

- 不要把真实 `config.yaml`、`.env`、`logs/`、`vikunja_local/` 一起打包或上传到任何公开仓库/网盘。
- 使用 GitHub 时建议用 `.env` + `${ENV}` 占位引用密钥（程序支持 `${环境变量名}` 展开），减少明文落盘。
