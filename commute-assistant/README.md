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

网页提供“使用当前位置”按钮。浏览器会先请求用户授权，GPS 坐标经高德坐标转换后用于当次路线查询，不保存为历史样本。目的地可先限定为上海市内或其他城市；目的地关键词通过高德输入提示搜索，并严格限制在所选城市内，再由用户确认候选地点。

路线表单可添加多个途经点，每个途经点都可独立选择“上海市内”或“其他城市”；选择其他城市时需填写城市名。地点候选会限制在各自选定的城市范围内，并按填写顺序规划。未来出发时间估算也会按途经点坐标和顺序匹配历史样本，不会混用不同停靠点的路线记录。

确认目的地后，助手会尝试搜索附近名称匹配的停车场，并让用户选择直达目的地或导航到停车场。路线结果页可按需搜索目的地或当前位置周边 5 公里内的停车场、加油站、充电站、公共厕所、药房、便利店、医院、汽车维修点和餐饮/快餐店；当前位置查询会重新请求浏览器定位，不会沿用出发时的坐标。结果标注直线距离。目的地周边地点可设为新路线终点；当前位置周边地点会生成从当前位置到该地点的路线。上述 POI 功能需要高德 Web 服务 Key 具有 POI 搜索权限；接口不可用时仍可继续规划原目的地。

逐段路线指引优先采用高德返回的明确导航动作（如左转、右转、掉头）；有动作信息时，会把“向东南行驶”这类方位描述简化为“继续行驶”，并补上下一步动作。如果接口没有返回动作信息，则保留原始指引，避免仅凭东南西北猜测左转或右转。

浏览器定位要求安全上下文：本机 `localhost` 可用于电脑端验证；手机通过电脑局域网 IP 的 HTTP 地址访问时，通常会被浏览器阻止，需要 HTTPS 才能使用定位。

### iPhone 通过局域网使用定位

本地 HTTPS 仅用于开发和家庭 Wi-Fi 测试。电脑与 iPhone 需连接同一 Wi-Fi。安装 [mkcert](https://github.com/FiloSottile/mkcert) 后，在 PowerShell 中运行：

```powershell
ipconfig
cd "C:\Users\12293\Documents\Codex\2026-10-13\ai-bot-linkedin\outputs\job-bot"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_commute_https.ps1 -LanIPv4 "电脑的 Wi-Fi IPv4 地址"
```

脚本会在本机创建 HTTPS 证书，并将仅含公开根证书的文件放到 `phone-setup\commute-root-ca.pem`。不要分享 mkcert 的 `rootCA-key.pem` 或项目里的 `commute-local-key.pem`。

在第二个 PowerShell 窗口中，从 `job-bot` 文件夹启动一个仅提供该证书目录的临时下载服务：

```powershell
.\.venv\Scripts\python.exe -m http.server 8766 --bind 0.0.0.0 --directory .\phone-setup
```

在 iPhone Safari 打开 `http://电脑的 Wi-Fi IPv4 地址:8766/commute-root-ca.pem` 并下载证书。然后前往“设置”>“已下载描述文件”安装；再前往“设置”>“通用”>“关于本机”>“证书信任设置”，为该根证书启用完全信任。停掉临时下载服务后，启动网页助手，在 iPhone 打开 `https://电脑的 Wi-Fi IPv4 地址:8765`，并允许 Safari 使用位置。Windows 防火墙如询问，请只允许专用网络访问。

首次安装本地根证书代表你在 iPhone 上信任这台开发电脑签发的证书。完成测试后，如果不再使用本地 HTTPS，可在“设置”>“通用”>“VPN 与设备管理”移除该证书描述文件。

## 定时生成报告

运行以下脚本，按提示输入两个 API Key。Key 会使用当前 Windows 账户加密保存在 `.local_secrets`，不会加入 Git：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_commute_schedule.ps1
```

脚本创建每天 08:00 和 18:00 的 Windows 计划任务。电脑需开机且当前用户已登录。报告保存在 `commute_reports`，运行记录保存在 `commute_logs`。

## 未来出发时间估算

网页的出发时间可以留空查询当前路况，也可以选择未来 14 天内的时间。未来估算根据本机保存的相同起终点、途经点顺序、路线策略、星期和相近时段的路线记录计算历史中位数。实时网页查询与定时简报都会积累样本；至少需要 3 个不同日期的记录。记录保存在 `commute_history.sqlite3`，不会上传到 GitHub。样本不足时页面会提示继续积累，不会把实时路况说成未来预测。结果只是历史参考，不能反映临时事故、天气、施工或交通管制。高德官方未来路径规划接口目前标注为企业开发者服务。

## 上传到 GitHub 前

`.gitignore` 已排除本机 API Key、真实地点配置、报告、日志、虚拟环境和生成的数据。请勿上传 `.local_secrets`、`.env` 或 API Key。
