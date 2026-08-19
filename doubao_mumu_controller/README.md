# 逍遥豆包发送 + 网页抓取

这个目录包含三套入口：

- `打开豆包逍遥控制面板.bat`：逍遥模拟器推荐入口。管理大量问题、每题独立重复次数、立即运行和 Windows 定时任务。
- `打开豆包MuMu控制面板.bat`：保留的 MuMu 兼容入口，功能相同。
- `一键启动_手机发送并网页抓取.bat`：不打开面板，直接运行 `questions.txt`。自动识别逍遥、核对手机/网页账号、在逍遥发送，再从网页抓取同一条新回答并写入 monitor 原有结果文件。
- `open_doubao_mumu_loop.bat`：只控制 MuMu 发送和读取回答，不运行网页抓取。

## 控制面板

逍遥模拟器双击 `打开豆包逍遥控制面板.bat`。面板支持：

- 添加、编辑、删除和 TXT 批量导入问题；
- 每个问题单独设置重复次数；
- 交叉提问或顺序提问；
- 设置每轮等待、超时、重试和随机冷却；
- 检测逍遥或 MuMu 实例、App 账号 UID 和网页账号是否一致；
- 自动并行管理全部已启动的模拟器；“实例”留空表示全部，也可填写
  `0,1,3` 只选择部分实例；
- 每个模拟器实例对应一个独立 Chrome 用户目录、调试端口和豆包账号，
  多账号之间不会串会话；
- 立即执行整批任务并显示实时日志；
- 安装每天固定时间、每隔若干分钟或仅一次的 Windows 定时任务；
- 查询和删除定时任务；
- 防止两次整批任务重叠运行。

面板配置保存在 `doubao_mumu_panel_config.json`。定时任务由
`doubao_mumu_scheduled_job.py` 执行，所以安装定时后不需要一直开着面板。

## 账号识别方式

脚本不会通过昵称或头像猜账号：

1. 用逍遥 `memuc.exe` 或 `MuMuManager.exe` 获得目标实例的真实 ADB 地址。
2. 临时执行 `adb root`，优先只读当前登录状态文件
   `/data/user/0/com.larus.nova/shared_prefs/com.bytedance.sdk.account_setting.xml`。
3. 直接读取当前登录的数字 `user_id` 和昵称；仅在状态文件不可用时，才使用
   `account_db` 连同 WAL 的一致性快照兜底，避免把历史登录账号误认成当前账号。
4. 若程序临时开启了 root，则读取后恢复原状态；逍遥原本已开启 root 时不反复重启 ADB。
5. 从豆包网页登录状态读取数字 UID，必须与 App UID 完全相同才继续。

日志和面板只显示脱敏 UID，不保存 Cookie、令牌或 `sec_uid`。

## 简单一键使用

1. 启动逍遥模拟器，并在各实例的豆包 App 中登录账号。
2. 编辑 `questions.txt`，一行一个问题。
3. 双击 `一键启动_手机发送并网页抓取.bat`。

脚本会自动完成：

1. 通过逍遥 `memuc.exe`/ADB 或 `MuMuManager.exe` 发现已启动实例和真实 ADB 端口。
2. 只读豆包 `account_db` 及 WAL 中最后登录账号的数字 UID，并保留模拟器原 root 状态。
3. 为每个模拟器实例分配独立 Chrome 调试端口，要求该网页 UID 与对应
   App UID 完全一致。
4. 若找不到匹配网页，会按“账号 UID + 模拟器实例”建立独立 Chrome
   配置并打开豆包；每个账号第一次需要人工登录一次。全部匹配后才允许开始。
5. 在网页端记录发送前的会话集合。
6. 控制模拟器新建对话、输入、校验、发送并等待回答稳定。
7. 只接受“发送后新增、且页面问题与本轮问题完全相同”的网页会话，避免抓错旧对话。
8. 调用 monitor 原来的 `run_doubao_latest_grab.py`、浏览器扩展和保存脚本，继续写入原有 CSV/待处理队列。

日志只显示脱敏 UID（例如 `7448…8330`），不会输出 Cookie、令牌或 `sec_uid`。

## 异常恢复

- 每个模拟器实例使用独占文件锁；多实例可以并行，同一实例不会被重复抢控。
- 多实例分别写独立运行日志和诊断目录；正式 CSV/分析数据通过跨进程锁
  安全合并，局域网上传队列也会串行提交，避免相互覆盖。
- 整批定时任务还有第二层非阻塞锁；上次未结束时，新定时触发会直接跳过。
- ADB 断开、Appium 会话失效、控件暂时消失、网页未同步、Chrome 退出、抓取或保存失败都会自动重试。
- 手机端已确认发送后，网页抓取失败不会再次发送同一问题。
- `--max-round-retries 0` 为持续恢复，默认不会因单次异常退出。
- 只有人工关闭窗口或按 `Ctrl+C` 才会安全停止。
- GitHub 克隆到新电脑后，先安装并启动 Chrome、逍遥和豆包，再以管理员身份
  双击仓库根目录的 `新电脑首次安装逍遥抓取环境.bat`。脚本会安装/核验 Python 依赖、Java、
  Appium 2.19.0 和 UiAutomator2 4.2.9，并执行完整环境自检。

## 常用命令

问题列表各运行一次：

```powershell
python .\doubao_mumu_web_pipeline.py --questions-file .\questions.txt
```

循环运行问题列表：

```powershell
python .\doubao_mumu_web_pipeline.py --questions-file .\questions.txt --forever
```

指定模拟器多开实例：

```powershell
python .\doubao_mumu_web_pipeline.py --device-index 1 --questions-file .\questions.txt
```

只运行一个问题：

```powershell
python .\doubao_mumu_web_pipeline.py --question "推荐一款染发剂" --rounds 1
```

## 输出

- `doubao_mumu_web_pipeline.log`：完整滚动日志。
- `doubao_mumu_web_results.jsonl`：每轮全链路结果与异常。
- `doubao_mumu_scheduled_job.log`：面板和定时整批任务日志。
- `doubao_mumu_panel_config.json`：面板问题、次数、运行与定时配置。
- `doubao_mumu_web_diagnostics\`：异常时的模拟器 XML、截图和说明。
- monitor 根目录原有的 `doubao_answers_result.csv`、`doubao_refs_result.csv`、`doubao_products_result.csv`：沿用原抓取保存流程。

只控制 MuMu 的旧入口仍会写入 `doubao_mumu_results.jsonl`、`doubao_mumu_state.json` 和 `doubao_mumu_diagnostics\`。

## 两台电脑局域网部署

主机（本目录所在电脑）：

1. 双击 `启动主机局域网接收服务.bat`。
2. 首次部署时，以管理员身份运行
   `配置主机接收端防火墙_管理员运行.ps1`，仅放行专用网络 TCP 8790。
3. 接收服务同时启动原有实时面板。局域网地址为
   `http://主机IP:8765/`，接收接口为 `http://主机IP:8790/`。
4. 执行 `python build_doubao_remote_package.py` 生成完整远端 ZIP。

远端电脑：

1. 安装 Google Chrome 和逍遥模拟器；完整部署包已经包含便携 Python、
   Appium 和 ADB。
2. 启动逍遥，在豆包 App 登录并保持在豆包页面。
3. 解压生成的 ZIP，双击根目录的 `一键启动独立采集.bat`。
4. 程序会发现所有已启动逍遥实例，并为每台打开一个专用 Chrome。分别登录
   对应的豆包账号；面板逐台核对数字 UID，全部一致后自动允许并行启动。

每次抓取先写远端本地结果，再进入持久上传队列。主机断电、Wi-Fi
中断或接口暂时不可用时不会丢失数据；下一次运行会自动补传。主机按
`request_id` 去重，因此重试不会重复入库。配对文件和部署 ZIP 内含上传
密钥，应只在可信的同一局域网和自己的电脑之间传递。

主机接收端输出：

- `doubao_lan_receiver.log`：接收、处理和异常恢复日志。
- `lan_receiver_queue\inbox`：已收到、尚未完成处理的数据。
- `lan_receiver_queue\done`：已经写入正式面板数据的回执。
- `lan_receiver_queue\errors`：最近一次处理异常，后台会继续重试。

远端输出：

- `lan_upload_outbox`：断网时待续传的数据。
- `lan_upload_sent`：主机已确认接收的回执。
- `doubao_lan_sync_agent.log`：每 5 秒后台回传、确认和重试日志。
- `doubao_remote_startup.log`：一键启动和环境自检日志。

远端完成配对后，也可以直接双击 `远端豆包采集并回传.bat`。该入口只执行
豆包采集任务并把结果回传主电脑，不启动远端数据面板；主电脑暂时离线时，
结果保留在 `lan_upload_outbox`，网络恢复后的下一次运行会自动续传。
