# 上海自驾通勤助手

这个项目用于查询上海自驾路线、查询时的实时路况和天气，并生成本地 Markdown 通勤报告。路线查询使用高德 Web 服务 API；AI 简报使用 OpenAI API。路况代表查询时刻，不是未来早晚高峰预测。

## 配置

Windows PowerShell 中进入项目目录：

```powershell
cd "C:\Users\12293\Documents\Codex\2026-10-13\ai-bot-linkedin\outputs\commute-assistant"
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .\commute_config.example.json .\commute_config.json
```

按需编辑 `commute_config.json` 中的地点和路线设置。设置 API Key 时不要把值写进代码或配置文件：

```powershell
$env:AMAP_WEB_KEY = "你的高德 Web 服务 Key"
$env:OPENAI_API_KEY = "你的 OpenAI API Key"
```

## 生成通勤报告

```powershell
.\.venv\Scripts\python.exe .\commute_assistant.py
```

报告保存在 `commute_reports` 文件夹。交互式路线查询可运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run_commute_web.ps1
```

## 定时生成报告

运行以下脚本，按提示输入两个 API Key。Key 会使用当前 Windows 账户加密保存在 `.local_secrets`，不会加入 Git：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_commute_schedule.ps1
```

脚本创建每天 08:00 和 18:00 的 Windows 计划任务。电脑需开机且当前用户已登录。报告保存在 `commute_reports`，运行记录保存在 `commute_logs`。

## 上传到 GitHub 前

`.gitignore` 已排除本机 API Key、真实地点配置、报告、日志、虚拟环境和生成的数据。请勿上传 `.local_secrets`、`.env` 或 API Key。
