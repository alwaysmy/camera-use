# start.ps1 —— 一键启动：检查依赖 → 停旧实例 → 起后端（自动拉旁路）→ 开浏览器
#
#   .\start.ps1                推荐配置：NV12 640x480 + IR + YOLO旁路，自动开浏览器
#   .\start.ps1 -NoVision      只要原生功能（人脸用内置 Viola-Jones，不跑 Python 旁路）
#   .\start.ps1 -Codec mjpg -W 1280 -H 720     1080p/720p（未压缩码流上不去，见 README）
#   .\start.ps1 -Hires         640x480 YUY2 单路最高画质（IR 会自动关：带宽不够）
param(
  [switch]$NoVision,
  [switch]$Hires,
  [switch]$NoBrowser,
  [string]$Codec = "nv12",
  [int]$W = 640,
  [int]$H = 480,
  [double]$VisionFPS = 0   # 0 = 不限速（跑满为止）
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($t, $c = "Gray") { Write-Host $t -ForegroundColor $c }

Say "=== camera_use 原生后端 ===" "Cyan"

# ── 1) 依赖检查 ──
if (-not (Test-Path "camera_backend.exe")) {
  Say "[1/5] 没有 camera_backend.exe，先构建…" "Yellow"
  .\build.ps1
} else {
  Say "[1/5] 可执行文件 OK（$([math]::Round((Get-Item camera_backend.exe).Length/1MB,1)) MB）" "Green"
}

$need = @("models\yolov8n-face.onnx", "models\yolov8n-pose.onnx", "models\mediapipe\hand_landmarker.task", "tools\vision_sidecar.py")
$miss = $need | Where-Object { -not (Test-Path $_) }
if ($miss.Count -gt 0) {
  Say "[2/5] 缺旁路文件：$($miss -join ', ') → 只启动原生功能" "Yellow"
  Say "       要启用 YOLO 人脸/骨架/手势，先跑： python tools\export_models.py" "Yellow"
  $NoVision = $true
} else {
  Say "[2/5] 旁路模型 OK（face+pose+hands）" "Green"
}

if (-not $NoVision) {
  $py = Get-Command python -ErrorAction SilentlyContinue
  if (-not $py) {
    Say "       没找到 python → 关掉旁路" "Yellow"; $NoVision = $true
  } else {
    $ok = & python -c "import onnxruntime, mediapipe; print('ok')" 2>$null
    if ($ok -ne "ok") {
      Say "       缺 python 包（onnxruntime / mediapipe）→ 关掉旁路" "Yellow"
      Say "       装：pip install onnxruntime-directml mediapipe" "Yellow"
      $NoVision = $true
    }
  }
}

# ── 2) 停旧实例 ──
$old = Get-Process camera_backend -ErrorAction SilentlyContinue
if ($old) {
  Say "[3/5] 停掉旧实例 (pid $($old.Id -join ','))" "Yellow"
  $old | Stop-Process -Force
  Start-Sleep -Seconds 3
} else {
  Say "[3/5] 没有旧实例" "Green"
}
# 旁路是后端的子进程，可能残留
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*vision_sidecar*" } |
  ForEach-Object { Say "       清掉残留旁路 pid $($_.ProcessId)" "Yellow"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# ── 3) 组装参数 ──
if ($Hires) { $Codec = "yuy2"; $W = 640; $H = 480 }
$args = @("-port", "8770", "-codec", $Codec, "-w", "$W", "-h", "$H")
if ($Hires) { $args += "-no-ir" }
if (-not $NoBrowser) { $args += "-open" }
if (-not $NoVision) { $args += @("-vision", "-vision-fps", "$VisionFPS") }
Say "[4/5] 启动： camera_backend.exe $($args -join ' ')" "Cyan"

$env:GOPROXY = "off"
$p = Start-Process .\camera_backend.exe -ArgumentList $args -WorkingDirectory $PSScriptRoot -PassThru `
  -RedirectStandardOutput "$env:TEMP\camera_backend.out.log" -RedirectStandardError "$env:TEMP\camera_backend.err.log" `
  -WindowStyle Hidden

# ── 4) 等就绪 ──
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 500
  try {
    $st = Invoke-RestMethod "http://127.0.0.1:8770/api/state" -TimeoutSec 3
    if ($st.data.stats.rgb_fps -gt 1) { $ok = $true; break }
  } catch { }
}
if (-not $ok) {
  Say "[5/5] 启动失败或相机没就绪，看日志：" "Red"
  Say "      $env:TEMP\camera_backend.err.log" "Red"
  Get-Content "$env:TEMP\camera_backend.err.log" -ErrorAction SilentlyContinue | Select-Object -Last 6
  exit 1
}

$s = $st.data
Say "[5/5] 就绪 ✓" "Green"
Say ("      RGB {0:N1}fps ({1}) + IR {2:N1}fps | 拼图流 {3:N1}fps" -f `
  $s.stats.rgb_fps, $s.stats.subtype, $s.stats.ir_fps, $s.stats.stream_fps) "Gray"
Say ("      暗场 RGB={0} IR={1}" -f $s.stats.dark_rgb, $s.stats.dark_ir) "Gray"
if (-not $NoVision) {
  Start-Sleep -Seconds 6
  $n = Invoke-RestMethod "http://127.0.0.1:8770/api/state" -TimeoutSec 3
  $v = $n.data.stats
  Say ("      旁路 {0}（人脸 {1} 骨架 {2} 手 {3} {4}）" -f `
    $(if ($v.vision_ok) { "运行中" } else { "启动中/未连上" }), $v.yolo_faces, $v.yolo_poses, $v.yolo_hands, $v.gestures) "Gray"
}
Say ""
Say "  控制台   http://127.0.0.1:8770/" "Cyan"
Say "  停止     Get-Process camera_backend | Stop-Process -Force" "Gray"
Say "  日志     $env:TEMP\camera_backend.err.log" "Gray"
Say "  提示     首次用请点「暗场校准」并遮住镜头；灯光变了要重跑「相机白平衡 → 自动校准」" "Gray"
