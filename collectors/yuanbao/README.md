# 腾讯元宝监控系统

项目由 Python 采集端和统一 React 监控面板组成。采集端优先自动识别逍遥模拟器
（同时兼容 MuMu），控制元宝 App
发送问题，并从同账号元宝网页抓取完整回答与信源；面板与豆包数据共用一个界面，
按北京自然日、问题、设备、媒体、信源类型、品牌和产品展示结果。

## 当前数据

已从空数据完成 10 轮正式采集：

- 推荐一款染发剂：4 轮；
- 推荐一款眉毛增长液：3 轮；
- 推荐一款睫毛增长液：3 轮；
- 10 轮全部成功，共抓取 153 条信源、50 个唯一链接。

正式结果位于：

- `yuanbao_results.jsonl`：全量逐轮数据；
- `web_results/`：每轮网页回答和信源；
- `yuanbao_diagnostics/`：每轮 App XML；
- `yuanbao_results.xlsx`：Excel 导出结果；
- `dashboard/public/data/dashboard.json`：React 面板聚合数据。

## 打开监控面板

双击根目录 `一键启动双模型监控.bat`（或本目录 `dashboard.bat`），浏览器会打开：

`http://localhost:3000`

面板已补齐豆包监控面板的核心信息结构：

- 左侧问题与设备范围；
- 总览和运行质量；
- 问题排行与设备概览；
- 信源类型、媒体集中度和信源证据；
- 品牌提及率与按问题拆分；
- 逐轮回答审计。
- 北京时间逐日归档与每日产品、信源趋势；
- 豆包/元宝汇总对比与统一采集启动、停止。

面板每 10 秒读取一次最新数据。采集程序每成功一轮也会立即重新生成面板数据。

## 再运行 10 轮

双击 `运行10轮并打开监控面板.bat`。

命令行方式：

```powershell
python .\yuanbao_loop.py --questions-file .\product.txt --rounds 10 --collect-web --max-retries 3
```

`product.txt` 使用 UTF-8 编码，一行一个问题。多台在线逍遥/MuMu 会自动并行，
每台设备使用独立断点、诊断目录和 Chrome 账号目录。

启动元宝远端控制面板后，程序会用逍遥官方 `memuc listvms --running`
识别所有运行实例，并为每台分配独立 Chrome 调试端口。启动时自动检查一次
App 与网页登录；未登录或账号不一致时只提醒，不会开始提问。完成登录后由用户
点击“打开并重新检测”，全部实例通过后才能启动批量循环与数据回传。

## 清空数据重新开始

关闭正在运行的采集任务后，删除以下运行产物即可：

- `yuanbao_results.jsonl`
- `yuanbao_results.xlsx`
- `yuanbao_state/`
- `yuanbao_diagnostics/`
- `web_results/`

然后运行：

```powershell
python .\build_dashboard_data.py
```

## DeepSeek 品牌与产品识别

将 `yuanbao_api.env.example` 复制为 `yuanbao_api.env` 并填写
`DEEPSEEK_API_KEY`。默认使用 `deepseek-v4-flash`，面板数据重建时会：

1. 只提交正文中的疑似商品行，减少输入 token；
2. 将模型结果逐条反查到原文证据；
3. 按产品首次出现位置计算正文排名；
4. 按正文哈希写入本地缓存，旧正文不会重复调用模型。

真实密钥文件和 AI 缓存文件已加入 `.gitignore`。

面板会恢复为空数据状态。
