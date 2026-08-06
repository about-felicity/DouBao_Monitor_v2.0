# 分机单模型采集与回传

主机运行 `一键启动三模型监控.bat` 后，会自动监听 `8791` 并生成
`runtime/lan_result_pairing.json`。该文件含有局域网地址和回传令牌，不能提交到 Git。

每台远端电脑只运行一个模型：`deepseek`、`yuanbao`、`wenxin` 或 `afu`。

1. 在远端电脑拉取与主机相同版本的项目，并仅启动该模型所需的 MuMu 实例和 Chrome。
2. 将主机的 `runtime/lan_result_pairing.json` 复制到远端电脑。
3. 在远端执行：`远端单模型采集.bat <模型ID> <配对文件路径>`。默认连续采集；按 `Ctrl+C` 安全停止。

例如：`远端单模型采集.bat yuanbao D:\lan_result_pairing.json`。如需限制轮数，在末尾加入第三个参数，例如 `10`。

远端会先持久化每条结果到 `runtime/remote_workers/<模型>/outbox`，再由后台线程回传。
网络中断不会影响采集；只有主机按 `request_id` 确认入库后，远端才会移除该条队列。
主机将数据追加到原有模型 JSONL，统一面板无需额外配置即可看到新结果。
