<#
.SYNOPSIS
  统计 MCP 调用日志：判断 camera-use 是「被框架拉起」还是「被 agent 调用」

.DESCRIPTION
  配合 src/mcplog.go（临时诊断措施）使用。核心判据：

    只有 start / initialize / tools/list  → 框架行为（正常，会话启动时的必然动作）
    出现密集 tools/call                    → agent 真在用 → 需看是哪个工具、哪个 client

  若长期只有握手，说明「老被调用」是框架触发的，日志即可按
  src/mcplog.go 头部的撤销清单移除。

  注意：默认排除 client 名含 test 的记录（自测数据），否则会把"我测过一次"
  误判成"agent 在调用"。要包含时加 -IncludeTest。

.PARAMETER LogDir
  日志目录，默认 ..\logs

.PARAMETER Days
  只看最近 N 天，0 = 全部

.PARAMETER IncludeTest
  连自测数据（client 名含 test）一起统计

.EXAMPLE
  .\mcp-log-stat.ps1
  .\mcp-log-stat.ps1 -Days 3
  .\mcp-log-stat.ps1 -IncludeTest
#>
param(
    [string]$LogDir = (Join-Path $PSScriptRoot "..\logs"),
    [int]$Days = 0,
    [switch]$IncludeTest
)

if (-not (Test-Path $LogDir)) {
    Write-Host "日志目录不存在: $LogDir" -ForegroundColor Yellow
    Write-Host "（说明还没有 MCP 客户端启动过本程序，或日志已被清理）"
    return
}

$files = @(Get-ChildItem "$LogDir\mcp_calls_*.jsonl" -ErrorAction SilentlyContinue |
    Where-Object { $Days -le 0 -or $_.LastWriteTime -gt (Get-Date).AddDays(-$Days) })
if ($files.Count -eq 0) { Write-Host "没有日志文件（LogDir=$LogDir）" -ForegroundColor Yellow; return }

$all = @(
    foreach ($f in $files) {
        Get-Content $f.FullName | Where-Object { $_.Trim() } | ForEach-Object {
            try { $_ | ConvertFrom-Json } catch { }
        }
    }
)
if ($all.Count -eq 0) { Write-Host "日志为空" -ForegroundColor Yellow; return }

# 默认排除自测数据：client 名含 test 的（start 事件还没有 client，保留）
$events = $all
if (-not $IncludeTest) {
    $events = @($all | Where-Object { -not $_.client -or $_.client -notmatch 'test' })
}
$excluded = $all.Count - $events.Count
if ($events.Count -eq 0) {
    Write-Host "排除自测数据后没有事件（加 -IncludeTest 查看全部）" -ForegroundColor Yellow
    return
}

$first = $events | Select-Object -First 1
$last = $events | Select-Object -Last 1

Write-Host "`n=== 日志概览 ===" -ForegroundColor Cyan
Write-Host ("  文件 {0} 个　事件 {1} 条　时间范围 {2} ~ {3}" -f `
        $files.Count, $events.Count, $first.ts, $last.ts)
if ($excluded -gt 0) {
    Write-Host ("  （已排除 {0} 条自测记录；-IncludeTest 可含）" -f $excluded) -ForegroundColor DarkGray
}

# ── ① 谁在拉起 MCP 服务器 ──
Write-Host "`n=== ① 谁在拉起 MCP 服务器（start 事件）===" -ForegroundColor Cyan
$starts = @($events | Where-Object { $_.ev -eq 'start' })
if ($starts.Count -gt 0) {
    $starts | Group-Object {
        # start 发生在 initialize 之前，此时还不知道 client —— 如实标注
        $c = if ($_.client) { $_.client } else { "(握手前，未知)" }
        "{0}  [{1}]" -f $_.caller, $c
    } | Sort-Object Count -Descending | ForEach-Object {
        Write-Host ("  {0,-48} {1,3} 次" -f $_.Name, $_.Count)
    }
    Write-Host "  时间线（最近 10 次）:"
    $starts | Select-Object -Last 10 | ForEach-Object {
        Write-Host ("    {0}  pid={1}  {2}" -f $_.ts, $_.pid, $_.caller)
    }
}
else { Write-Host "  （无）" }

# ── ② 有没有真在调工具 ──
Write-Host "`n=== ② 工具调用（tools/call）—— 区分「框架加载」与「agent 使用」的关键 ===" -ForegroundColor Cyan
$calls = @($events | Where-Object { $_.ev -eq 'tools/call' })
if ($calls.Count -gt 0) {
    Write-Host ("  共 {0} 次：" -f $calls.Count)
    $calls | Group-Object tool | Sort-Object Count -Descending | ForEach-Object {
        Write-Host ("    {0,-20} {1,4} 次" -f $_.Name, $_.Count)
    }
    Write-Host "  最近 10 次:"
    $calls | Select-Object -Last 10 | ForEach-Object {
        $a = ""
        if ($_.args) { $a = ($_.args | ConvertTo-Json -Compress -Depth 3) }
        if ($a.Length -gt 52) { $a = $a.Substring(0, 52) + "…" }
        $c = if ($_.client) { $_.client } else { "?" }
        Write-Host ("    {0}  {1,-16} {2,-56} ({3})" -f $_.ts, $_.tool, $a, $c)
    }
}
else {
    Write-Host "  （无）—— MCP 只是被框架加载，没有任何 agent 调用过工具" -ForegroundColor Green
}

# ── ③ 结论 ──
Write-Host "`n=== ③ 结论 ===" -ForegroundColor Cyan
if ($calls.Count -eq 0) {
    Write-Host "  ✓ 只有框架握手（start / initialize / tools/list），没有 agent 调用" -ForegroundColor Green
    Write-Host "    → 判据 ② 不成立：'老被调用' 是框架触发的。"
    Write-Host "      若持续如此，日志可按 src/mcplog.go 头部的撤销清单整体移除："
    Write-Host "        1) 删 src/mcplog.go"
    Write-Host "        2) 删 src/mcp.go 里 4 处接入（newMCPLogger / lg.close / lg.LogPath / lg.add）"
    Write-Host "        3) 删 .gitignore 里的 logs/ 规则"
    Write-Host "        4) 删 logs/ 目录"
}
else {
    $rate = [math]::Round($calls.Count / [math]::Max(1, $starts.Count), 1)
    Write-Host ("  ⚠ 有 {0} 次工具调用（平均每次启动 {1} 次）" -f $calls.Count, $rate) -ForegroundColor Yellow
    Write-Host "    → 看 ② 的分组：哪个工具、哪个 client；再对照 ① 的 caller 定位是哪个 agent / 哪类场景"
}
Write-Host ""
