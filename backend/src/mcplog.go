// mcplog.go —— MCP 调用日志：记录「谁、什么时候、调了什么」
//
// 为什么需要：MCP 是 stdio 传输，调用方是启动它的 agent 进程。
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
