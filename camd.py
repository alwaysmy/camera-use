#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""camd.py — 采集守护进程（给 Go 后端喂帧）

分工：**Python 只碰硬件**（DirectShow / Media Foundation 那套），
所有像素运算（配准 warp、融合、差分、JPEG 编码）和 HTTP 都在 Go 后端做。

协议
----
stdout（二进制帧，小端）::

    [type:1][w:uint32][h:uint32][ch:uint32][len:uint32][payload]

    type 'R' = RGB BGR 帧      'B' = IR 补光灯亮帧(灰度)
         'D' = IR 补光灯灭帧   'J' = JSON 文本（w/h/ch=0）

stdin（每行一个 JSON 命令）::

    {"cmd":"codec","value":"mjpg"}          {"cmd":"resolution","value":[1280,720]}
    {"cmd":"ir","value":true}               {"cmd":"align"}
    {"cmd":"dark","target":"both","frames":30}
    {"cmd":"dark_clear","target":"both"}    {"cmd":"save_raw","dir":"..."}
    {"cmd":"quit"}

单独调试::

    python camd.py --frames 3 > dump.bin     # 抓几帧就退出
"""
from __future__ import annotations

import argparse
import json
import queue
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HEADER = struct.Struct("<BIIII")


class Sender(threading.Thread):
    """把帧写到 stdout。1 槽"只留最新"队列 —— 后端处理不过来时**丢旧帧**，
    保证永远是低延迟的最新画面（而不是越积越长的延迟）。"""

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out
        self.q: queue.Queue = queue.Queue(maxsize=1)
        self.halt = False
        self.sent = 0
        self.dropped = 0
        self.bytes = 0

    def push(self, typ: str, arr) -> None:
        """零拷贝登记：arr 是 numpy 数组，写入时才转 memoryview。"""
        item = (typ, arr)
        try:
            self.q.put_nowait(item)
        except queue.Full:
            try:
                self.q.get_nowait()
                self.q.put_nowait(item)
                self.dropped += 1
            except queue.Empty:
                pass

    def push_json(self, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.out.write(HEADER.pack(ord("J"), 0, 0, 0, len(data)))
            self.out.write(data)
            self.out.flush()
        except (BrokenPipeError, OSError):
            self.halt = True

    def run(self):
        while not self.halt:
            try:
                typ, arr = self.q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                import numpy as np
                h, w = arr.shape[:2]
                ch = 1 if arr.ndim == 2 else arr.shape[2]
                mv = memoryview(np.ascontiguousarray(arr)).cast("B")
                self.out.write(HEADER.pack(ord(typ), w, h, ch, len(mv)))
                self.out.write(mv)
                self.out.flush()
                self.sent += 1
                self.bytes += len(mv)
            except (BrokenPipeError, OSError):
                self.halt = True
            except Exception as e:
                self.push_json({"event": "error", "where": "sender", "error": str(e)})


class Daemon:
    def __init__(self, args):
        from camera_pipeline import CameraSession, DarkField, DarkNotCovered
        self.args = args
        self.dark_exc = DarkNotCovered
        self.session = CameraSession(
            rgb_spec=args.device, width=args.width, height=args.height,
            codec=args.codec, use_ir=not args.no_ir, out_dir=args.out_dir)
        self.sender = Sender(sys.stdout.buffer)
        self.seq_rgb = self.seq_ir = -1
        self.halt = False

    # ---- 主循环 ----
    def run(self) -> int:
        self.sender.start()
        threading.Thread(target=self._stdin_loop, daemon=True).start()
        self.sender.push_json({"event": "hello", "pid": None,
                               "codec": self.session.codec,
                               "size": [self.session.width, self.session.height],
                               "ir": self.session.ir is not None})
        n = 0
        t0 = time.time()
        while not self.halt and not self.sender.halt:
            s = self.session
            if s.rgb and s.rgb.seq != self.seq_rgb and s.rgb.frame is not None:
                self.seq_rgb = s.rgb.seq
                self.sender.push("R", s.rgb.frame)
            if s.ir and s.ir.seq != self.seq_ir:
                self.seq_ir = s.ir.seq
                if s.ir.bright is not None:
                    self.sender.push("B", s.ir.bright)
                if s.ir.darkframe is not None:
                    self.sender.push("D", s.ir.darkframe)
            n += 1
            if self.args.frames and n > self.args.frames * 4:
                time.sleep(0.4)
                break
            if time.time() - t0 > 2.0:
                self.sender.push_json({"event": "stats", "sent": self.sender.sent,
                                       "dropped": self.sender.dropped,
                                       "MB": round(self.sender.bytes / 1e6, 1),
                                       "rgb_fps": round(s.rgb.fps, 1) if s.rgb else 0,
                                       "ir_fps": round(s.ir.fps, 1) if s.ir else 0})
                t0 = time.time()
            time.sleep(0.004)
        self.session.stop()
        return 0

    # ---- 命令 ----
    def _stdin_loop(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self._dispatch(msg)
            except Exception as e:
                self.sender.push_json({"event": "reply", "id": msg.get("id"), "ok": False,
                                       "error": f"{type(e).__name__}: {e}"})
    
    def _dispatch(self, m: dict):
        cmd = m.get("cmd")
        rid = m.get("id")
        s = self.session

        if cmd == "quit":
            self.halt = True
            self.sender.push_json({"event": "reply", "id": rid, "ok": True})
            return

        if cmd == "codec":
            r = s.restart_rgb(codec=m["value"])
            self._reply(rid, r)
        elif cmd == "resolution":
            w, h = m["value"]
            r = s.restart_rgb(width=int(w), height=int(h))
            self._reply(rid, r)
        elif cmd == "ir":
            r = s.set_ir_enabled(bool(m["value"]))
            self._reply(rid, r)
        elif cmd == "align":
            if s.rgb.frame is None or s.ir is None or s.ir.bright is None:
                raise RuntimeError("需要 RGB 和 IR 都有画面")
            r = s.do_align()
            self._reply(rid, r)
        elif cmd == "dark":
            r = s.calibrate_dark(target=m.get("target", "both"), frames=int(m.get("frames", 30)))
            self._reply(rid, r)
        elif cmd == "dark_clear":
            s.clear_dark(m.get("target", "both"))
            self._reply(rid, {"cleared": True})
        elif cmd == "dark_status":
            from camera_pipeline import DarkField
            out = {}
            for tag in ("rgb", "ir"):
                d = DarkField.load(tag)
                out[tag] = ({"ready": True, **d.meta} if d and d.ready else {"ready": False})
            self._reply(rid, out)
        elif cmd == "save_raw":
            out_dir = Path(m.get("dir", "captures"))
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            import cv2
            files = {}
            if s.rgb and s.rgb.frame is not None:
                p = out_dir / f"rgb_{ts}.jpg"
                cv2.imwrite(str(p), s.rgb.frame)
                files["rgb"] = str(p)
            if s.ir and s.ir.bright is not None:
                p = out_dir / f"ir_{ts}.jpg"
                cv2.imwrite(str(p), s.ir.bright)
                files["ir"] = str(p)
            self._reply(rid, files)
        elif cmd == "status":
            self._reply(rid, {
                "codec": s.codec, "size": [s.width, s.height],
                "ir": s.ir is not None,
                "rgb_fps": round(s.rgb.fps, 1) if s.rgb else 0,
                "ir_fps": round(s.ir.fps, 1) if s.ir else 0,
                "rgb_fourcc": s.rgb.fourcc if s.rgb else "?",
                "rgb_error": s.rgb.error if s.rgb else None,
                "ir_error": s.ir.error if s.ir else None,
                "sent": self.sender.sent, "dropped": self.sender.dropped,
            })
        else:
            self._reply(rid, {"error": f"unknown cmd {cmd!r}"}, ok=False)

    def _reply(self, rid, data, ok=True):
        self.sender.push_json({"event": "reply", "id": rid, "ok": ok, "data": data})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="采集守护进程（给 Go 后端喂帧）")
    ap.add_argument("--device", default="rgb")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--codec", default="auto", choices=["auto", "yuy2", "mjpg"])
    ap.add_argument("--no-ir", action="store_true")
    ap.add_argument("--out-dir", default="captures")
    ap.add_argument("--frames", type=int, default=0, help="抓 N 帧后退出（调试用）")
    a = ap.parse_args(argv)
    return Daemon(a).run()


if __name__ == "__main__":
    import os
    try:
        rc = main()
        sys.stdout.flush()
    finally:
        os._exit(rc if 'rc' in dir() else 0)
