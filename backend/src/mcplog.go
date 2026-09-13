// mcplog.go —— MCP 调用日志：记录「谁、什么时候、调了什么」
//
// ⚠️ 这是【临时诊断措施】，不是长期功能（2026-09-14 加，用户明确"后续找到问题了再去掉"）
//
// 目的：排查「camera-use MCP 老被调用」，需要区分两种情况：
//   ① MCP 加载时就开相机 —— **已确认并修复**（main.go 里 e.Start() 位置错误）
//   ② agent 真的在频繁调用工具 —— **需日志才能确认**
//
// 【何时可以删】观察一段时间，若日志显示调用频率正常
//   （只有 initialize / tools/list 这类握手，没有密集的 tools/call），
//   说明 ② 不成立 → 本文件与相关接入点即可整体移除。
//
// 【撤销清单】删除时一并处理：
//   1. 删本文件 src/mcplog.go
//   2. 删 src/mcp.go 里 4 处接入：newMCPLogger() / lg.close() / lg.LogPath() / lg.add(...)
//   3. 删 .gitignore 中的 logs/ 规则
//   4. 删 logs/ 目录
//
// 为什么需要它：MCP 走 stdio，调用方就是启动它的 agent 进程。
// 没有日志时只能看到副作用（摄像头被打开、进程被拉起），**查不到来源** ——
// "这个 MCP 老被调用"就只能靠猜。
//
// 三个"谁"的来源：
//   ① MCP 协议 initialize 请求里的 clientInfo（客户端自报 name/version）
//   ② 父进程 pid + 进程名（os.Getppid() → 查进程名）
//   ③ 自己的 pid
//
// 输出：logs/mcp_calls_YYYYMMDD.jsonl（JSON Lines，每行一条，便于 grep / jq）
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type mcpLogger struct {
	mu       sync.Mutex
	f        *os.File
	path     string
	client   string // initialize 时客户端自报的 name/version
	ppid     int
	ppidName string
}

func newMCPLogger() *mcpLogger {
	l := &mcpLogger{ppid: os.Getppid()}
	dir := P("logs")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "[mcp] 日志目录创建失败: %v\n", err)
		return l
	}
	l.path = filepath.Join(dir, "mcp_calls_"+time.Now().Format("20060102")+".jsonl")
	f, err := os.OpenFile(l.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		fmt.Fprintf(os.Stderr, "[mcp] 日志文件打开失败: %v\n", err)
		return l
	}
	l.f = f
	l.ppidName = procNameOf(l.ppid)
	l.add("start", nil) // 记一条启动记录（含调用者）
	return l
}

// procNameOf —— 取进程名（回答"被谁调用"）。只在初始化时调一次，开销可接受。
func procNameOf(pid int) string {
	if pid <= 0 {
		return ""
	}
	out, err := exec.Command("tasklist", "/FI", fmt.Sprintf("PID eq %d", pid), "/FO", "CSV", "/NH").Output()
	if err != nil {
		return ""
	}
	s := strings.TrimSpace(string(out))
	if i := strings.Index(s, ","); i > 0 {
		return strings.Trim(s[:i], `"`)
	}
	return ""
}

func (l *mcpLogger) add(ev string, extra map[string]any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.f == nil {
		return
	}
	rec := map[string]any{
		"ts":   time.Now().Format("2006-01-02T15:04:05"),
		"ev":   ev,
		"pid":  os.Getpid(),
		"ppid": l.ppid,
	}
	if l.ppidName != "" {
		rec["caller"] = l.ppidName
	}
	if l.client != "" {
		rec["client"] = l.client
	}
	for k, v := range extra {
		rec[k] = v
	}
	b, _ := json.Marshal(rec)
	_, _ = l.f.Write(append(b, '\n'))
}

func (l *mcpLogger) close() {
	l.add("stop", nil) // add 自带锁
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.f != nil {
		_ = l.f.Close()
		l.f = nil
	}
}

// LogPath —— 供诊断输出
func (l *mcpLogger) LogPath() string { return l.path }
