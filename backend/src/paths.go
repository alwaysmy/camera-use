// paths.go — 路径锚定：一切资源都相对**可执行文件所在目录**，而不是"当前工作目录"
//
// 为什么必须这么做：
//   `camera_backend.exe` 从桌面快捷方式、任务计划、别的目录启动时，CWD 不是程序目录，
//   那么 `models/`、`calib/`、`captures/`、`tools/` 全部会找不到 —— 这是稳健性问题，
//   不是配置问题。锚定到 exe 目录后，从任何地方启动都能跑。
//
// 兜底顺序：
//   ① -root 显式指定（测试 / 特殊部署）
//   ② 可执行文件所在目录（正常情况）
//   ③ 当前工作目录（`go run` 时没有稳定的 exe 目录）
package main

import (
	"os"
	"path/filepath"
)

var baseDir string

func init() { initBaseDir("") }

// initBaseDir —— 允许 main 里用 -root 覆盖
func initBaseDir(root string) {
	if root != "" {
		if abs, err := filepath.Abs(root); err == nil {
			baseDir = abs
			return
		}
	}
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		// 只在 exe 目录看起来确实像程序根目录时才用它
		// （`go run` 的 exe 在临时目录里，那种情况下退回 CWD）
		if hasResource(dir) {
			baseDir = dir
			return
		}
	}
	wd, _ := os.Getwd()
	baseDir = wd
}

// hasResource —— 判断该目录是否是资源根目录
func hasResource(dir string) bool {
	for _, probe := range []string{"models", "src", "tools"} {
		if st, err := os.Stat(filepath.Join(dir, probe)); err == nil && st.IsDir() {
			return true
		}
	}
	// 允许"只有 exe + models"的精简部署
	if st, err := os.Stat(filepath.Join(dir, "models")); err == nil && st.IsDir() {
		return true
	}
	return false
}

// P —— 拼出资源绝对路径：P("models", "x.onnx")
func P(parts ...string) string {
	all := make([]string, 0, len(parts)+1)
	all = append(all, baseDir)
	all = append(all, parts...)
	return filepath.Join(all...)
}

// RootDir —— 供 UI / 诊断展示
func RootDir() string { return baseDir }
