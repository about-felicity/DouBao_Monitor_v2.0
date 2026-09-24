# DeepSeek 对话监控（Chrome 扩展）

面向 `https://chat.deepseek.com/` 的本地监控工具。界面与 `kuake` 项目保持一致：按问题列表执行，每一轮使用独立新会话，等待回答稳定后抓取模型正文和可获得的信源标题、链接。

## 安全节奏

- 默认固定间隔 360 秒（6 分钟）。
- 发送许可时间持久化到扩展存储；页面刷新、扩展休眠和异常重试都不能绕过间隔。
- “问题已发送但页面状态不确定”时不会立即重发，仍须等到下一个 6 分钟发送窗口并换新会话。
- 一旦页面出现“操作频繁”、安全验证、登录失效或额度上限，任务自动暂停，等待人工检查。
- 默认载入 21 个产品问题，每题每天 2 轮，共 42 次独立会话；一轮完成后按 24 小时周期继续。

## 安装与启动

1. 双击 `一键启动DeepSeek监控.cmd`，启动本机接收器（`127.0.0.1:8766`）。
2. 在 Chrome 打开 `chrome://extensions/`，开启“开发者模式”。
3. 点击“加载已解压的扩展程序”，选择本项目的 `extension` 目录。
4. 打开并登录 `https://chat.deepseek.com/`，刷新一次页面。
5. 点击工具栏中的“DeepSeek 对话监控”。首次仍建议用一个问题、1 轮验证，再启用 21 题每日任务。

插件会确保“智能搜索”处于开启状态。每轮结束后，结果写入：

- `data/deepseek_results.jsonl`
- `data/results/<run_id>/*.json`
- `data/results/<run_id>/*.txt`
- `data/deepseek_monitor.log`

关闭插件弹窗不会停止任务。任务状态、最近结果和日志均可在弹窗中查看；完整结果由本机接收器长期保存。

## 信源采集

插件同时使用两条通道：

- 从回答正文、引用标记和“X 个网页”信源面板读取可见链接；
- 在页面主环境监听 fetch/XHR 返回内容，从流式响应中补充信源 URL 与标题。

内部 DeepSeek 地址、图片资源、头像等会被过滤，链接按规范化 URL 去重。

## 自检

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\run_tests.ps1
```

若 `runtime/remote_workers/deepseek_sync.json` 存在，启动脚本会自动启用统一面板回传；中心接收后会进入异步产品分析和信源正文分析队列。
