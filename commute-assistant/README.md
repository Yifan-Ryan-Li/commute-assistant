# 上海自驾通勤助手

这个项目用于查询上海自驾路线、查询时的实时路况和天气，并生成本地 Markdown 通勤报告。路线查询使用高德 Web 服务 API；AI 简报使用 OpenAI API。路况代表查询时刻，不是未来早晚高峰预测。

## 配置

Windows PowerShell 中从仓库根目录进入项目目录：

```powershell
cd .\commute-assistant
py -3.15 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
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
& .\.venv\Scripts\python.exe .\commute_web.py
```

本机浏览器打开 `http://localhost:8765`。也可以用 `run_commute_web.ps1`，但它要求已通过计划任务设置脚本保存高德 Key。

网页提供“使用当前位置”按钮。浏览器会先请求用户授权，GPS 坐标经高德坐标转换后用于当次路线查询，不保存为历史样本。目的地可先限定为上海市内或其他城市；目的地关键词通过高德输入提示搜索，并严格限制在所选城市内，再由用户确认候选地点。

路线表单可添加多个途经点，每个途经点都可独立选择“上海市内”或“其他城市”；选择其他城市时需填写城市名。地点候选会限制在各自选定的城市范围内，并按填写顺序规划。未来出发时间估算也会按途经点坐标和顺序匹配历史样本，不会混用不同停靠点的路线记录。

确认目的地后，助手会尝试搜索附近名称匹配的停车场，并让用户选择直达目的地或导航到停车场。路线结果页可按需搜索目的地或当前位置周边 5 公里内的停车场、加油站、充电站、公共厕所、药房、便利店、医院、汽车维修点和餐饮/快餐店；当前位置查询会重新请求浏览器定位，不会沿用出发时的坐标。结果标注直线距离。目的地周边地点可设为新路线终点；当前位置周边地点会生成从当前位置到该地点的路线。上述 POI 功能需要高德 Web 服务 Key 具有 POI 搜索权限；接口不可用时仍可继续规划原目的地。

可选服务故障时会尽量保留已成功的数据：停车场或周边 POI 搜索失败会显示“暂不可用”，不会误报成“附近没有结果”；本机历史记录读写失败不会阻止实时路线显示，但会提示历史样本未能读取或保存。定时报告会分别查询天气和路线；其中一项失败时仍会生成另一项的简报，并明确标出缺失数据。AI 摘要接口报错、超时或返回空内容时会改用本地规则整理；若天气和路线都不可用，也会保存一份说明情况的诊断报告并以非零退出码结束，方便计划任务日志识别失败。

逐段路线指引优先采用高德返回的明确导航动作（如左转、右转、掉头）；有动作信息时，会把“向东南行驶”这类方位描述简化为“继续行驶”，并补上下一步动作。如果接口没有返回动作信息，则保留原始指引，避免仅凭东南西北猜测左转或右转。

浏览器定位要求安全上下文：本机 `localhost` 可用于电脑端验证；手机通过电脑局域网 IP 的 HTTP 地址访问时，通常会被浏览器阻止，需要 HTTPS 才能使用定位。

网页路线表单提供“填入去程”和“填入返程”两个快捷按钮，分别填写“上海市普陀区桃浦镇”与上海交通大学闵行校区的两个方向。起点和目的地始终可自由编辑；编辑后仍可正常查询，结果按实际起终点显示为“当前路线”。定时简报目前仍使用配置文件中的单一方向，尚未按早晚自动切换。

实时路线结果显示每条路线的预计耗时、距离和收费，并将路线1作为明确标出的比较基准，展示其他路线相对它节省/增加的预计时间和收费。差值仅在高德同时返回对应数据时显示；预计数据不等于实际行程结果。

### iPhone 通过局域网使用定位

本地 HTTPS 仅用于开发和家庭 Wi-Fi 测试。电脑与 iPhone 需连接同一 Wi-Fi。安装 [mkcert](https://github.com/FiloSottile/mkcert) 后，在 PowerShell 中运行：

```powershell
ipconfig
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_commute_https.ps1 -LanIPv4 "电脑的 Wi-Fi IPv4 地址"
```

以上命令在 `commute-assistant` 项目目录中运行；如果当前位于仓库根目录，先运行 `cd .\commute-assistant`。

脚本会在本机创建 HTTPS 证书，并将仅含公开根证书的文件放到 `phone-setup\commute-root-ca.pem`。不要分享 mkcert 的 `rootCA-key.pem` 或项目里的 `commute-local-key.pem`。

在第二个 PowerShell 窗口中，从 `commute-assistant` 文件夹启动一个仅提供该证书目录的临时下载服务：

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

网页的出发时间可以留空查询当前路况，也可以选择未来 14 天内的时间。未来时间页面展示的是本机历史高德查询中、相同起终点、途经点顺序、路线策略、星期和相近时段的地图预计值统计；它不是实际行程耗时，也不是未来实时路况预测。路线按高德返回的完整路线几何生成标识，不再用“第 1/2/3 条”作为身份，因此高德调整返回顺序时不会把不同路线混在一起。路线几何必须严格一致，无法确认的几何变化会作为不同路径处理。实时网页查询与定时简报都会积累地图预计值样本；每条路线至少需要 3 个不同日期，且同一天的多次查询先合并，再计算跨日中位数。这只是程序门槛，不代表统计上已经可靠。旧记录没有保存路线几何，无法可靠对应具体路线，因此不会混入新路线的估算。记录保存在 `commute_history.sqlite3`，不会上传到 GitHub。结果不能反映临时事故、天气、施工或交通管制。高德官方未来路径规划接口目前标注为企业开发者服务。

## 上传到 GitHub 前

`.gitignore` 已排除本机 API Key、真实地点配置、报告、日志、虚拟环境和生成的数据。请勿上传 `.local_secrets`、`.env` 或 API Key。

## 无 Key 离线演示

在本项目目录运行以下命令，可打开不需要 API Key 的演示网页：

```powershell
& .\.venv\Scripts\python.exe .\commute_web.py --demo
```

页面使用内置合成地点和路线，不访问网络、不写入真实历史数据库。页面中的路线、交通状态、耗时和收费仅用于演示，不可用于实际出行。浏览器地址为 `http://localhost:8766`；按 `Ctrl+C` 停止。

更多说明：[数据流程](./docs/data-flow.md)、[常见问题排错](./docs/troubleshooting.md)。
