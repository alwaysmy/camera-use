// lifecycle.go — 按需打开 / 空闲释放（MCP 即用即开）
//
// 为什么需要：MCP 进程是常驻的（stdio 长连接），但**相机和旁路不该常驻占用**——
// 开着 Hello 就登不了、别的程序也用不了摄像头，而且白烧 CPU。
//
// 策略：
//   ① 相机：任何需要画面的调用先 EnsureOpen()；空闲 idleCamSecs 秒无请求 → 自动关闭
//   ② 旁路（YOLO/骨架/手部）：只有调用里**明确要检测**时才拉起；
//      空闲 idleSidecarSecs 秒 → 自动关掉（模型加载要好几秒，不值得常驻）
package main

import (
	"fmt"
	"os"
	"os/exec"
	"sync"
	"time"
)

const (
	idleCamSecs     = 60  // 相机空闲多久自动释放
	idleSidecarSecs = 120 // 旁路空闲多久自动关闭
)

var (
	lifeMu       sync.Mutex
	lastUse      time.Time
	sidecarCmd   *exec.Cmd
	sidecarFPS   float64
	sidecarSince time.Time
)

// Touch —— 标记"刚被用过"，推迟空闲释放
func Touch() {
	lifeMu.Lock()
	lastUse = time.Now()
	lifeMu.Unlock()
}

// EnsureOpen —— 相机没开就开（懒加载：MCP 服务本身可以毫秒级起来，代价留到第一次调用）
func (e *Engine) EnsureOpen() error {
	Touch()
	e.mu.RLock()
	has := e.rgb != nil
	cfg := e.wantCfg
	e.mu.RUnlock()
	if has {
		return nil
	}
	return e.Start(cfg.Codec, cfg.W, cfg.H, cfg.IR)
}

// closeCameras —— 释放相机（下次调用会重新打开）
func (e *Engine) closeCameras() {
	e.mu.Lock()
	rgb, ir := e.rgb, e.ir
	e.rgb, e.ir = nil, nil
	e.mu.Unlock()
	if rgb != nil {
		rgb.Stop()
	}
	if ir != nil {
		ir.Stop()
	}
}

// idleLoop —— 后台巡检：相机/旁路空闲就释放
func (e *Engine) idleLoop() {
	t := time.NewTicker(5 * time.Second)
	defer t.Stop()
	for range t.C {
		lifeMu.Lock()
		lu := lastUse
		sc := sidecarCmd != nil
		lifeMu.Unlock()

		if e.MCPMode() {
			// 只有 MCP 模式才做空闲释放；HTTP 模式下用户正在看画面，别关
			if !lu.IsZero() && time.Since(lu) > idleCamSecs*time.Second {
				e.mu.RLock()
				has := e.rgb != nil
				e.mu.RUnlock()
				if has {
					e.closeCameras()
					fmt.Fprintln(os.Stderr, "[life] 相机空闲，已释放（下次调用自动重开）")
				}
			}
			if sc && time.Since(lu) > idleSidecarSecs*time.Second {
				StopSidecar()
			}
		}
	}
}

// ── 旁路进程：按需拉起 / 空闲关闭 ──

// EnsureSidecar —— 需要 YOLO/骨架/手部时才拉起旁路
func EnsureSidecar(fps float64) error {
	lifeMu.Lock()
	running := sidecarCmd != nil
	if fps > 0 {
		sidecarFPS = fps
	}
	lifeMu.Unlock()
	if running {
		return nil
	}
	if _, err := os.Stat("tools/vision_sidecar.py"); err != nil {
		return fmt.Errorf("缺少 tools/vision_sidecar.py")
	}
	if _, err := os.Stat("models/yolov8n-pose.onnx"); err != nil {
		return fmt.Errorf("缺少 ONNX 模型（先跑 python tools/export_models.py）")
	}
	f := sidecarFPS
	args := []string{"tools/vision_sidecar.py"}
	if f > 0 {
		args = append(args, "--fps", fmt.Sprintf("%.1f", f))
	}
	cmd := exec.Command("python", args...)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("拉起旁路失败: %w", err)
	}
	lifeMu.Lock()
	sidecarCmd = cmd
	sidecarSince = time.Now()
	lifeMu.Unlock()
	fmt.Fprintf(os.Stderr, "[life] 旁路已按需启动 pid=%d（空闲 %d 秒后自动关闭）\n", cmd.Process.Pid, idleSidecarSecs)
	go func() { _ = cmd.Wait(); lifeMu.Lock(); sidecarCmd = nil; lifeMu.Unlock() }()
	return nil
}

// StopSidecar —— 关掉旁路（空闲超时或参数全关时）
func StopSidecar() {
	lifeMu.Lock()
	cmd := sidecarCmd
	sidecarCmd = nil
	lifeMu.Unlock()
	if cmd != nil && cmd.Process != nil {
		_ = cmd.Process.Kill()
		fmt.Fprintln(os.Stderr, "[life] 旁路空闲，已关闭")
	}
}

// SidecarInfo —— 供 /api/state 与 MCP 汇报
func SidecarInfo() (bool, float64) {
	lifeMu.Lock()
	defer lifeMu.Unlock()
	if sidecarCmd == nil {
		return false, 0
	}
	return true, time.Since(sidecarSince).Seconds()
}
