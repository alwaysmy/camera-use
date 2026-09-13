// events.go — 事件流（环形缓冲）：给底部时间线与 agent 用
//
// "此刻"能看状态，"刚才发生了什么"只能靠事件流。对 agent 尤其重要：
// camera_observe{what:"events"} 就能回答"刚才有谁经过/什么时候人走了"。
package main

import (
	"sync"
	"time"
)

type Event struct {
	ID   uint64 `json:"id"`
	At   string `json:"at"`   // HH:MM:SS
	Kind string `json:"kind"` // person/gesture/face/config/camera/calib
	Text string `json:"text"`
}

type eventLog struct {
	mu   sync.Mutex
	buf  []Event
	next uint64
	cap  int
}

func newEventLog(capacity int) *eventLog {
	return &eventLog{buf: make([]Event, 0, capacity), cap: capacity, next: 1}
}

func (l *eventLog) add(kind, text string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	e := Event{ID: l.next, At: time.Now().Format("15:04:05"), Kind: kind, Text: text}
	l.next++
	l.buf = append(l.buf, e)
	if len(l.buf) > l.cap {
		l.buf = l.buf[len(l.buf)-l.cap:]
	}
}

// since —— 取 id > since 的事件（前端增量拉取用）
func (l *eventLog) since(id uint64, max int) []Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]Event, 0, 16)
	for _, e := range l.buf {
		if e.ID > id {
			out = append(out, e)
		}
	}
	if max > 0 && len(out) > max {
		out = out[len(out)-max:]
	}
	return out
}

func (l *eventLog) recent(max int) []Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	n := len(l.buf)
	if max > 0 && n > max {
		n = max
	}
	out := make([]Event, n)
	copy(out, l.buf[len(l.buf)-n:])
	return out
}
