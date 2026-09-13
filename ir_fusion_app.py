#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ir_fusion_app.py — RGB + 红外(IR) 实时融合演示台（tkinter 桌面板）

本文件只是**界面**：采集、校准、配准、融合全部委托给 ``camera_pipeline``
（WebUI / CLI 用的是同一份引擎，避免三套逻辑各自漂移）。

    python ir_fusion_app.py --auto-align
    python ir_fusion_app.py --help          # 见参数

快捷键：空格=暂停  S=存图  A=自动标定  D=暗场校准  R=重置对齐  Q/Esc=退出
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import tkinter as tk
from tkinter import messagebox, ttk
from PIL import Image, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camera_pipeline import (CameraSession, DarkNotCovered, MODES as _MODES,   # noqa: E402
                             affine, stretch, warp_ir)

MODES = [("融合", "fuse"), ("细节注入", "detail"), ("RGB", "rgb"), ("IR", "ir"),
         ("IR 伪彩", "ircolor"), ("补光差分", "diff"), ("并排", "side")]


class App:
    VIEW_W, VIEW_H = 640, 480

    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.args = args
        self.paused = False
        self.view = None
        self._photo = None
        self._thumb_ph = {}

        self.session = CameraSession(
            rgb_spec=args.rgb_device, width=args.width, height=args.height,
            codec=args.codec, use_ir=not args.no_ir, out_dir=args.save_dir)
        p = self.session.params
        self.session.set_params(weight=args.weight, gain=args.gain, chroma=args.chroma)

        self.mode = tk.StringVar(value=args.mode)
        self.weight = tk.DoubleVar(value=p["weight"])
        self.gain = tk.DoubleVar(value=p["gain"])
        self.chroma = tk.DoubleVar(value=p["chroma"])
        self.scale = tk.DoubleVar(value=p["scale"])
        self.tx = tk.DoubleVar(value=p["tx"])
        self.ty = tk.DoubleVar(value=p["ty"])
        self.rot = tk.DoubleVar(value=p["rot"])
        self.status = tk.StringVar(value="启动中…")

        self._build_ui()
        for var, key in ((self.weight, "weight"), (self.gain, "gain"), (self.chroma, "chroma"),
                         (self.scale, "scale"), (self.tx, "tx"), (self.ty, "ty"), (self.rot, "rot"),
                         (self.mode, "mode")):
            var.trace_add("write", lambda *_, v=var, k=key: self._push_params())

        self.root.bind("<space>", lambda e: self._toggle_pause())
        self.root.bind("<Key-s>", lambda e: self._save())
        self.root.bind("<Key-a>", lambda e: self._auto_align())
        self.root.bind("<Key-d>", lambda e: self._dark_cal())
        self.root.bind("<Key-r>", lambda e: self._reset_align())
        self.root.bind("<Key-q>", lambda e: self._quit())
        self.root.bind("<Escape>", lambda e: self._quit())
        self.root.after(600, self._maybe_auto_align)
        self.root.after(0, self._tick)

    # ---- UI ----
    def _build_ui(self):
        self.root.title("RGB + 红外(IR) 实时融合演示台")
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(top, width=self.VIEW_W, height=self.VIEW_H,
                                bg="#111", highlightthickness=0)
        self.canvas.pack()

        thumbs = ttk.Frame(top)
        thumbs.pack(fill="x", pady=4)
        self.thumb_labels = {}
        # Tk 的 Label 没有图片时 width/height 按字符算 —— 先塞占位图，尺寸才按像素走
        placeholder = ImageTk.PhotoImage(Image.new("RGB", (160, 120), (34, 34, 34)))
        self._placeholder = placeholder
        for key, text in (("rgb", "RGB"), ("ir", "IR"), ("diff", "补光差分")):
            box = ttk.LabelFrame(thumbs, text=text, padding=2)
            box.pack(side="left", padx=3)
            lb = tk.Label(box, image=placeholder, borderwidth=0)
            lb.pack()
            self.thumb_labels[key] = lb

        row = ttk.Frame(top)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="模式").pack(side="left")
        for text, val in MODES:
            ttk.Radiobutton(row, text=text, value=val, variable=self.mode).pack(side="left", padx=2)

        row2 = ttk.Frame(top)
        row2.pack(fill="x", pady=2)
        for label, var, lo, hi in (("融合权重", self.weight, 0, 1),
                                   ("IR 增益", self.gain, 0.5, 4),
                                   ("彩度", self.chroma, 0.5, 4)):
            ttk.Label(row2, text=label).pack(side="left", padx=(10, 0))
            ttk.Scale(row2, from_=lo, to=hi, variable=var, length=110).pack(side="left", padx=4)
            ttk.Label(row2, textvariable=var, width=5).pack(side="left")

        row3 = ttk.Frame(top)
        row3.pack(fill="x", pady=2)
        for label, var in (("scale", self.scale), ("tx", self.tx), ("ty", self.ty), ("rot°", self.rot)):
            ttk.Label(row3, text=label).pack(side="left", padx=(8, 0))
            ttk.Spinbox(row3, from_=-300, to=300, increment=0.01 if label == "scale" else 1,
                        width=7, textvariable=var).pack(side="left", padx=2)
        ttk.Button(row3, text="自动标定 (A)", command=self._auto_align).pack(side="left", padx=6)
        ttk.Button(row3, text="重置 (R)", command=self._reset_align).pack(side="left")
        ttk.Button(row3, text="存图 (S)", command=self._save).pack(side="left", padx=6)
        ttk.Button(row3, text="暗场校准 (D)", command=self._dark_cal).pack(side="left")
        ttk.Button(row3, text="清暗场", command=self._dark_clear).pack(side="left", padx=4)
        ttk.Button(row3, text="暂停 (空格)", command=self._toggle_pause).pack(side="left")

        ttk.Label(self.root, textvariable=self.status, relief="sunken",
                  anchor="w", justify="left").pack(fill="x", side="bottom")

    # ---- 行为 ----
    def _push_params(self):
        try:
            self.session.set_params(mode=self.mode.get(), weight=float(self.weight.get()),
                                    gain=float(self.gain.get()), chroma=float(self.chroma.get()),
                                    scale=float(self.scale.get()), tx=float(self.tx.get()),
                                    ty=float(self.ty.get()), rot=float(self.rot.get()))
        except Exception:
            pass

    def _toggle_pause(self):
        self.paused = not self.paused

    def _reset_align(self):
        self.scale.set(1.300); self.tx.set(98.0); self.ty.set(18.0); self.rot.set(0.0)

    def _maybe_auto_align(self):
        if self.args.auto_align:
            self.root.after(800, self._auto_align)

    def _auto_align(self):
        def work():
            try:
                info = self.session.do_align()
                self.scale.set(info["scale"]); self.tx.set(info["tx"]); self.ty.set(info["ty"])
                self.status.set(f"标定完成 scale={info['scale']} tx={info['tx']} "
                                f"ty={info['ty']} 相关度={info['score']}")
            except Exception as e:
                self.status.set(f"标定失败: {e}")
        self.status.set("标定中…")
        threading.Thread(target=work, daemon=True).start()

    def _dark_cal(self):
        if not messagebox.askokcancel(
                "暗场校准",
                "请先用不透光物体把镜头和 IR 窗口完全遮住。\n遮好后点“确定”开始采集暗场。"):
            return
        self.status.set("采集暗场中…")

        def work():
            try:
                out = self.session.calibrate_dark(target="both", frames=30)
                msg = "；".join(f"{k}: 均值{v['meta']['mean']} 热噪点{v['meta']['hot_pixels']}"
                                for k, v in out.items())
                self.status.set("暗场校准完成 " + msg)
            except DarkNotCovered as e:
                self.status.set(f"暗场校准失败: {e}")
            except Exception as e:
                self.status.set(f"暗场校准出错: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _dark_clear(self):
        self.session.clear_dark("both")
        self.status.set("已清除暗场")

    def _save(self):
        try:
            saved = self.session.save()
            names = ", ".join(Path(v).name for v in saved.values())
            self.status.set(f"已保存: {names}  → {self.session.out_dir}")
        except Exception as e:
            self.status.set(f"存图失败: {e}")

    def _quit(self):
        self.session.stop()
        self.root.after(150, self.root.destroy)

    # ---- 主循环 ----
    def _tick(self):
        if not self.paused:
            try:
                res = self.session.compose()
                self.view = res["view"]
                if self.view is not None:
                    self._draw(self.view)
                self._thumb("rgb", res["rgb"], stretch_if_gray=False)
                self._thumb("ir", res["ir"], stretch_if_gray=True)
                self._thumb("diff", res["diff"], stretch_if_gray=False)
                st = res["stats"]
                led = (f" | LED {st.get('led_on_mean')}/{st.get('led_off_mean')}"
                       if st.get("led_on_mean") is not None else "")
                err = "".join(" | " + e for e in (st["rgb_error"], st["ir_error"]) if e)
                self.status.set(
                    f"RGB {st['rgb_fps']:4.1f}fps({st['codec']}) mean={st['rgb_mean']} | "
                    f"IR {st['ir_fps']:4.1f}fps mean={st['ir_mean']}{led} | "
                    f"暗场 RGB={'有' if st['dark_rgb'] else '无'}/IR={'有' if st['dark_ir'] else '无'} | "
                    f"模式={st['mode']} 权重={float(self.weight.get()):.2f}"
                    + ("  [暂停]" if self.paused else "") + err)
            except Exception as e:
                self.status.set(f"渲染错误: {e}")
        self.root.after(33, self._tick)

    def _draw(self, view: np.ndarray):
        self.canvas.delete("all")
        s = min(self.VIEW_W / view.shape[1], self.VIEW_H / view.shape[0])
        shown = cv2.resize(view, (int(view.shape[1] * s), int(view.shape[0] * s))) if s != 1 else view
        ox, oy = (self.VIEW_W - shown.shape[1]) // 2, (self.VIEW_H - shown.shape[0]) // 2
        self._photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(shown, cv2.COLOR_BGR2RGB)))
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)

    def _thumb(self, key, frame, stretch_if_gray: bool):
        if frame is None:
            return
        img = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        if stretch_if_gray and frame.ndim == 2:
            img = frame
        small = cv2.resize(img, (160, 120))
        ph = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
        lb = self.thumb_labels[key]
        lb.configure(image=ph)
        lb.image = ph


def main(argv=None):
    ap = argparse.ArgumentParser(description="RGB + 红外(IR) 实时融合演示台")
    ap.add_argument("--rgb-device", default="rgb")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--codec", default="auto", choices=["auto", "yuy2", "mjpg"],
                    help="auto=有 IR 时用 MJPG，只开 RGB 时用画质更好的 YUY2")
    ap.add_argument("--no-ir", action="store_true")
    ap.add_argument("--mode", default="fuse", choices=[m[1] for m in MODES])
    ap.add_argument("--weight", type=float, default=0.62)
    ap.add_argument("--gain", type=float, default=1.6)
    ap.add_argument("--chroma", type=float, default=2.0)
    ap.add_argument("--auto-align", action="store_true")
    ap.add_argument("--save-dir", default="captures")
    args = ap.parse_args(argv)

    root = tk.Tk()
    app = App(root, args)
    try:
        root.mainloop()
    finally:
        app.session.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
