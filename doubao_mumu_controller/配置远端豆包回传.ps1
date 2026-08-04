param([Parameter(Mandatory=$true)][string]$PairingFile)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pairing = Get-Content -LiteralPath $PairingFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $pairing.enabled -or -not $pairing.receiver_url -or -not $pairing.token) {
    throw "配对文件缺少 enabled、receiver_url 或 token。"
}
$config = [ordered]@{
    version = 1
    enabled = $true
    receiver_url = [string]$pairing.receiver_url
    receiver_urls = @($pairing.receiver_urls)
    receiver_host = [string]$pairing.receiver_host
    token = [string]$pairing.token
    device_name = $env:COMPUTERNAME
    upload_timeout = 3
}
$config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $root "doubao_remote_sync_config.json") -Encoding UTF8
Write-Host "远端豆包数据回传已配置。后台每 5 秒检查待传队列，网络异常不会阻塞采集。"
