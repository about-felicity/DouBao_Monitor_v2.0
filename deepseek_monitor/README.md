# DeepSeek 监控

采集链路与豆包、元宝一致：MuMu 模拟器里的 DeepSeek App 负责新建对话和发送问题，同账号的独立 Chrome 网页只负责等待会话同步、读取完整回答和引用信源。默认每两次 App 实际发送之间随机等待 60–600 秒；发送后的状态会落盘，重启任务不会立刻再次提问。

首次使用：

1. 在 MuMu 模拟器的 DeepSeek App 登录采集账号，并保持“智能搜索”可用。
2. 运行 `open_deepseek_chrome.bat`，在打开的专用 Chrome 中登录同一个 DeepSeek 账号。
3. 编辑 `product.txt`，每行一个监控问题。
4. 从统一面板启动 DeepSeek，或运行：

```powershell
python .\deepseek_loop.py --rounds 10 --resume
```

可用 `--min-interval` 和 `--max-interval` 调整间隔秒数；程序强制最短值不低于 60 秒。
