# 离线构建脚本 —— GOPROXY=off 是刻意的：
# 本模块零第三方依赖，关掉代理正好证明"构建不需要网络"。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$env:GOFLAGS = "-mod=mod"
$env:GOPROXY = "off"
$env:GOSUMDB = "off"
$env:CGO_ENABLED = "0"     # 纯 Go，不需要 gcc

Write-Host "[build] 无外部依赖，GOPROXY=off 离线构建..." -ForegroundColor Cyan
go build -trimpath -ldflags "-s -w" -o camera_backend.exe ./src
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

$f = Get-Item camera_backend.exe
Write-Host ("[build] OK  {0}  {1:N2} MB" -f $f.Name, ($f.Length / 1MB)) -ForegroundColor Green
Write-Host "[build] 运行:  .\camera_backend.exe -open" -ForegroundColor Green
