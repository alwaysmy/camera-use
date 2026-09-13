"""
camera_app.py — 摄像头交互式控制面板
===================================
功能: 实时预览 / 参数调节 / 拍照 / 多摄像头切换
集成: duvc-ctl 实现正确的UVC曝光控制（Auto/Manual模式）
"""
import cv2
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import time
import os
import subprocess

try:
    import duvc_ctl as duvc
    HAS_DUVC = True
except ImportError:
    HAS_DUVC = False
    print("Warning: duvc-ctl not installed. Auto-exposure mode unavailable.")

class CameraApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Camera Control Panel")
        self.root.geometry("900x700")
        self.root.configure(bg="#1e1e1e")
        
        # Camera state
        self.cap = None
        self.duvc_cam = None
        self.current_camera = 0
        self.is_previewing = False
        self.save_dir = os.getcwd()
        self.exposure_mode = "auto"  # "auto" or "manual"
        
        # Default parameters
        self.defaults = {
            "BRIGHTNESS": 1.0,
            "CONTRAST": 51.0,
            "SATURATION": 65.0,
            "HUE": 1.0,
            "GAIN": 1.0,
            "EXPOSURE": -4.0,
            "SHARPNESS": 50.0,
        }
        self.params = self.defaults.copy()
        
        self.setup_ui()
        self.detect_cameras()
        
    def setup_ui(self):
        """Build UI"""
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#1e1e1e", foreground="white")
        style.configure("TButton", background="#3d3d3d", foreground="white")
        style.configure("TFrame", background="#1e1e1e")
        style.configure("TScale", background="#1e1e1e")
        
        # Top: Camera selection
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(top_frame, text="Camera:").pack(side="left")
        self.camera_combo = ttk.Combobox(top_frame, width=30, state="readonly")
        self.camera_combo.pack(side="left", padx=5)
        self.camera_combo.bind("<<ComboboxSelected>>", self.on_camera_change)
        
        ttk.Button(top_frame, text="Refresh", command=self.detect_cameras).pack(side="left", padx=5)
        
        # Center: Preview
        self.preview_label = tk.Label(self.root, bg="#000000", text="Preview Area", fg="white")
        self.preview_label.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Bottom: Control panel
        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill="x", padx=10, pady=5)
        
        # Left: Parameter sliders
        param_frame = ttk.LabelFrame(control_frame, text="Parameters")
        param_frame.pack(side="left", fill="both", expand=True, padx=5)
        
        self.sliders = {}
        slider_names = ["BRIGHTNESS", "CONTRAST", "SATURATION", "HUE", 
                       "GAIN", "EXPOSURE", "SHARPNESS"]
        
        for i, name in enumerate(slider_names):
            row = ttk.Frame(param_frame)
            row.pack(fill="x", padx=5, pady=2)
            
            ttk.Label(row, text=f"{name}:", width=12).pack(side="left")
            
            if name == "EXPOSURE":
                from_val, to_val, init_val = -10, 0, -4
            elif name in ["BRIGHTNESS", "HUE", "GAIN"]:
                from_val, to_val, init_val = 0, 100, 1
            else:
                from_val, to_val, init_val = 0, 100, 50
            
            slider = ttk.Scale(row, from_=from_val, to=to_val, 
                              orient="horizontal", length=200,
                              command=lambda v, n=name: self.on_slider_change(n, v))
            slider.set(init_val)
            slider.pack(side="left", padx=5)
            
            value_label = ttk.Label(row, text=str(init_val), width=8)
            value_label.pack(side="left")
            
            self.sliders[name] = (slider, value_label)
        
        # Right: Buttons
        btn_frame = ttk.LabelFrame(control_frame, text="Actions")
        btn_frame.pack(side="right", fill="y", padx=5)
        
        ttk.Button(btn_frame, text="Start Preview", command=self.start_preview).pack(fill="x", padx=5, pady=3)
        ttk.Button(btn_frame, text="Stop Preview", command=self.stop_preview).pack(fill="x", padx=5, pady=3)
        ttk.Button(btn_frame, text="Capture", command=self.capture).pack(fill="x", padx=5, pady=3)
        ttk.Button(btn_frame, text="Reset", command=self.reset_params).pack(fill="x", padx=5, pady=3)
        
        # Exposure mode toggle
        self.exposure_mode_var = tk.StringVar(value="auto")
        mode_frame = ttk.Frame(btn_frame)
        mode_frame.pack(fill="x", padx=5, pady=5)
        ttk.Radiobutton(mode_frame, text="Auto", variable=self.exposure_mode_var, 
                        value="auto", command=self.on_exposure_mode_change).pack(side="left")
        ttk.Radiobutton(mode_frame, text="Manual", variable=self.exposure_mode_var,
                        value="manual", command=self.on_exposure_mode_change).pack(side="left")
        
        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief="sunken")
        status_bar.pack(fill="x", side="bottom")
    
    def detect_cameras(self):
        """Detect available cameras（走 camera_core：名字 + 类型，不再靠裸索引）"""
        self.status_var.set("Detecting cameras...")
        self.root.update()

        cameras = []
        try:
            from camera_core import list_devices, probe_index
            for d in list_devices():
                if d.index is None:
                    # IR 相机没有 DShow 索引 —— 列出来但标明要用 Media Foundation
                    cameras.append((None, f"[{d.kind}] {d.name} — 需 Media Foundation"))
                    continue
                info = probe_index(d.index)
                res = (f"{info['width']}x{info['height']} mean={info['mean']}"
                       if info else "打不开")
                cameras.append((d.index, f"[{d.kind}] {d.name} — {res}"))
        except Exception as e:
            self.status_var.set(f"Camera detection failed: {e}")
            return

        self.camera_combo["values"] = [c[1] for c in cameras]
        self.camera_map = {c[1]: c[0] for c in cameras}

        # 默认落到 RGB 主相机（而不是"第一个索引"）
        pick = next((i for i, c in enumerate(cameras)
                     if c[1].startswith("[rgb]") and c[0] is not None), None)
        if cameras:
            idx = pick if pick is not None else 0
            self.camera_combo.current(idx)
            self.current_camera = cameras[idx][0]
            self.status_var.set(f"Found {len(cameras)} cameras")
        else:
            self.status_var.set("No cameras found")

    def on_camera_change(self, event=None):
        """Switch camera"""
        if self.is_previewing:
            self.stop_preview()

        selection = self.camera_combo.get()
        if selection in self.camera_map:
            self.current_camera = self.camera_map[selection]
            if self.current_camera is None:
                self.status_var.set("这台相机没有 DirectShow 索引，请用 camera_ir.py / CLI 的 ir 子命令")
            else:
                self.status_var.set(f"Switched to Camera {self.current_camera}")

    def start_preview(self):
        """Start preview"""
        if self.is_previewing:
            return
        if self.current_camera is None:
            messagebox.showerror("Error", "请先选择一台有 DirectShow 索引的相机")
            return

        # Close Windows Camera if open
        subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
        time.sleep(0.5)

        self.cap = cv2.VideoCapture(self.current_camera, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            messagebox.showerror("Error", f"Cannot open Camera {self.current_camera}")
            return
        
        time.sleep(0.5)
        
        # Open duvc-ctl camera for auto-exposure control
        if HAS_DUVC:
            try:
                self._open_duvc_camera()
                self._set_auto_exposure()
            except Exception as e:
                print(f"duvc-ctl warning: {e}")
        
        self.is_previewing = True
        self.status_var.set(f"Previewing - Camera {self.current_camera}")
        self.update_preview()
    
    def _open_duvc_camera(self):
        """Open camera via duvc-ctl（按当前索引对应的名字绑定，不再硬编码 'HP'）"""
        if self.duvc_cam:
            try:
                self.duvc_cam.close()
            except Exception:
                pass
            self.duvc_cam = None

        # duvc-ctl 的名字顺序 = DirectShow 枚举顺序 = OpenCV 索引，据此对上号
        target = None
        try:
            from camera_core import dshow_names
            names = dshow_names()
            if names and isinstance(self.current_camera, int) and 0 <= self.current_camera < len(names):
                target = names[self.current_camera]
        except Exception as e:
            print(f"camera_core warning: {e}")

        if target is None:
            cams = duvc.list_cameras()
            target = cams[0] if cams else None
        if target is None:
            return

        try:
            self.duvc_cam = duvc.find_camera(target)
            print(f"duvc-ctl bound to: {target}")
        except Exception as e:
            print(f"duvc-ctl bind failed for {target!r}: {e}")
    
    def _set_auto_exposure(self):
        """Set auto-exposure mode via duvc-ctl"""
        if not self.duvc_cam:
            return
        
        try:
            self.duvc_cam.set_exposure(0, 'auto')
            self.exposure_mode = "auto"
            self.exposure_mode_var.set("auto")
            print("Auto-exposure enabled")
        except Exception as e:
            print(f"Failed to set auto-exposure: {e}")
    
    def _set_manual_exposure(self, value):
        """Set manual exposure value via duvc-ctl"""
        if not self.duvc_cam:
            return
        
        try:
            self.duvc_cam.set_exposure(int(value), 'manual')
            self.exposure_mode = "manual"
            print(f"Manual exposure set to {value}")
        except Exception as e:
            print(f"Failed to set manual exposure: {e}")
    
    def on_exposure_mode_change(self):
        """Handle exposure mode radio button change"""
        mode = self.exposure_mode_var.get()
        if mode == "auto":
            self._set_auto_exposure()
        else:
            # Get current slider value
            if "EXPOSURE" in self.sliders:
                val = self.sliders["EXPOSURE"][0].get()
                self._set_manual_exposure(val)
    
    def _init_camera(self):
        """Initialize camera parameters"""
        if not self.cap:
            return
        
        init_params = {
            "BRIGHTNESS": 10,
            "CONTRAST": 50,
            "SATURATION": 64,
            "HUE": 0,
            "GAIN": 0,
            "EXPOSURE": -2,
            "SHARPNESS": 50,
        }
        
        for name, val in init_params.items():
            prop_id = getattr(cv2, f"CAP_PROP_{name}", None)
            if prop_id is not None:
                self.cap.set(prop_id, val)
        
        for name, (slider, label) in self.sliders.items():
            if name in init_params:
                slider.set(init_params[name])
                label.config(text=f"{init_params[name]:.1f}")
                self.params[name] = init_params[name]
    
    def stop_preview(self):
        """Stop preview"""
        self.is_previewing = False
        if self.cap:
            self.cap.release()
            self.cap = None
        if self.duvc_cam:
            try:
                self.duvc_cam.close()
            except Exception:
                pass
            self.duvc_cam = None
        self.preview_label.config(image="", text="Preview stopped")
        self.status_var.set("Preview stopped")
    
    def update_preview(self):
        """Update preview frame"""
        if not self.is_previewing or not self.cap:
            return
        
        ret, frame = self.cap.read()
        if ret:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            label_w = self.preview_label.winfo_width()
            label_h = self.preview_label.winfo_height()
            
            if label_w > 1 and label_h > 1:
                h, w = frame_rgb.shape[:2]
                scale = min(label_w / w, label_h / h)
                new_w, new_h = int(w * scale), int(h * scale)
                frame_resized = cv2.resize(frame_rgb, (new_w, new_h))
                
                img = Image.fromarray(frame_resized)
                imgtk = ImageTk.PhotoImage(image=img)
                
                self.preview_label.config(image=imgtk, text="")
                self.preview_label.image = imgtk
        
        self.root.after(30, self.update_preview)
    
    def on_slider_change(self, name, value):
        """Slider value change"""
        val = float(value)
        self.params[name] = val
        if name in self.sliders:
            self.sliders[name][1].config(text=f"{val:.1f}")
        
        # Apply in real-time if previewing
        if self.cap and self.is_previewing:
            if name == "EXPOSURE":
                # Use duvc-ctl for proper exposure control
                if self.exposure_mode == "manual":
                    self._set_manual_exposure(val)
                # In auto mode, ignore slider (auto controls it)
            else:
                prop_id = getattr(cv2, f"CAP_PROP_{name}", None)
                if prop_id is not None:
                    self.cap.set(prop_id, val)
    
    def reset_params(self):
        """Reset to default parameters"""
        self.params = self.defaults.copy()
        for name, (slider, label) in self.sliders.items():
            slider.set(self.params[name])
            label.config(text=f"{self.params[name]:.1f}")
        
        if self.cap and self.is_previewing:
            for name, val in self.params.items():
                if name == "EXPOSURE":
                    self._set_auto_exposure()
                else:
                    prop_id = getattr(cv2, f"CAP_PROP_{name}", None)
                    if prop_id is not None:
                        self.cap.set(prop_id, val)
        
        self.status_var.set("Parameters reset")
    
    def capture(self):
        """Capture and save photo"""
        if not self.cap or not self.is_previewing:
            messagebox.showwarning("Warning", "Please start preview first")
            return
        
        ret, frame = self.cap.read()
        if ret:
            ts = time.strftime("%Y%m%d_%H%M%S")
            filename = f"capture_{self.current_camera}_{ts}.jpg"
            filepath = os.path.join(self.save_dir, filename)
            cv2.imwrite(filepath, frame)
            self.status_var.set(f"Saved: {filename}")
            messagebox.showinfo("Success", f"Photo saved:\n{filename}")
    
    def run(self):
        """Run application"""
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()
    
    def on_close(self):
        """Close application"""
        self.stop_preview()
        self.root.destroy()

if __name__ == "__main__":
    app = CameraApp()
    app.run()
