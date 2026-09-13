// probe.go — 原生链路自检（-probe）：设备枚举 → 打开 → 格式 → 抓帧 → 曝光控制
package main

import (
	"bytes"
	"fmt"
	"image"
	"image/jpeg"
	"strings"
	"time"
)

func MFProbe() {
	if err := lockCOMThread(); err != nil {
		fmt.Println("COM/MF 初始化失败:", err)
		return
	}
	defer mfShutdown()

	fmt.Println("=== 1) SetupAPI 设备枚举（原生，不依赖 PowerShell） ===")
	for _, d := range ListCameras() {
		fmt.Printf("  [%-7s] %s\n             %s\n", d.Kind, d.Name, d.SymbolicLink)
	}

	fmt.Println("\n=== 2) MF 设备枚举（对照：看不到 IR 属正常，被 Hello 隐私策略过滤） ===")
	if devs, err := EnumVidcapDevices(); err != nil {
		fmt.Println("  MFEnumDeviceSources 失败:", err)
	} else {
		for _, d := range devs {
			fmt.Printf("  [%s] %s\n", d.Kind, d.Name)
		}
	}

	for _, kind := range []string{"rgb", "ir"} {
		dev, found := PickCamera(kind)
		if !found {
			fmt.Printf("\n=== 跳过 %s：没找到设备 ===\n", kind)
			continue
		}
		fmt.Printf("\n=== 3) 打开 %s：%s ===\n", kind, dev.Name)
		cam, err := OpenCameraByLink(dev.SymbolicLink)
		if err != nil {
			fmt.Println("  打开失败:", err)
			continue
		}
		formats := cam.NativeFormats()
		byType := map[string][]MediaFormat{}
		var order []string
		for _, f := range formats {
			if _, ok := byType[f.Subtype]; !ok {
				order = append(order, f.Subtype)
			}
			byType[f.Subtype] = append(byType[f.Subtype], f)
		}
		fmt.Printf("  原生格式 %d 种 / %d 个媒体类型:\n", len(order), len(formats))
		for _, st := range order {
			fs := byType[st]
			fmt.Printf("    %-5s  %d 种分辨率，最大 %dx%d\n", st, len(fs), maxW(fs), maxH(fs))
		}

		// 选格式并抓帧
		w, h := 640, 480
		if kind == "ir" {
			w, h = 340, 340
		}
		if _, err := cam.SetBestFormat([]string{"MJPG", "YUY2", "NV12"}, w, h); err != nil {
			fmt.Println("  设置格式失败:", err)
			cam.Close()
			continue
		}
		fmt.Printf("  已选格式: %s %dx%d @%.0ffps\n", cam.Subtype, cam.W, cam.H, cam.FPS)

		n, t0, bytesTotal := 0, time.Now(), 0
		var lastMean float64
		for i := 0; i < 40; i++ {
			data, err := cam.GrabFrame()
			if err != nil {
				fmt.Println("  抓帧失败:", err)
				break
			}
			if data == nil {
				continue
			}
			n++
			bytesTotal += len(data)
			if m, ok := frameMean(data, cam.Subtype, cam.W, cam.H); ok {
				lastMean = m
			}
		}
		dt := time.Since(t0).Seconds()
		fmt.Printf("  抓 %d 帧 / %.2fs → %.1f fps，平均帧大小 %.0f KB，末帧亮度 %.1f\n",
			n, dt, float64(n)/dt, float64(bytesTotal)/float64(max(1, n))/1024, lastMean)

		// 曝光控制
		if cc := cam.CameraControl(); cc != 0 {
			defer comRelease(cc)
			rng, ok := ctrlGetRange(cc, CtrlExposure)
			if ok {
				fmt.Printf("  IAMCameraControl.Exposure: min=%d max=%d step=%d default=%d caps=0x%X\n",
					rng.Min, rng.Max, rng.Step, rng.Default, rng.Caps)
				v, f, ok2 := ctrlGet(cc, CtrlExposure)
				if ok2 {
					fmt.Printf("    当前 value=%d flags=%d（1=Auto 2=Manual）\n", v, f)
				}
				if err := ctrlSet(cc, CtrlExposure, 0, CtrlFlagAuto); err == nil {
					fmt.Println("    切 Auto: OK")
				} else {
					fmt.Println("    切 Auto:", err)
				}
				if err := ctrlSet(cc, CtrlExposure, -3, CtrlFlagManual); err == nil {
					fmt.Println("    切 Manual(-3): OK")
				} else {
					fmt.Println("    切 Manual(-3):", err)
				}
				_ = ctrlSet(cc, CtrlExposure, 0, CtrlFlagAuto) // 还原
			} else {
				fmt.Println("  IAMCameraControl 无 Exposure 范围（相机不支持）")
			}
		} else {
			fmt.Println("  IAMCameraControl 不可用")
		}
		cam.Close()
	}
	fmt.Println("\nprobe 完成")
}

func maxW(fs []MediaFormat) int {
	m := 0
	for _, f := range fs {
		if f.W > m {
			m = f.W
		}
	}
	return m
}

func maxH(fs []MediaFormat) int {
	m := 0
	for _, f := range fs {
		if f.H > m {
			m = f.H
		}
	}
	return m
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// frameMean —— 取一帧的平均亮度（顺便验证 Go 的 JPEG 解码能不能吃下相机出的 MJPG）
func frameMean(data []byte, subtype string, w, h int) (float64, bool) {
	switch subtype {
	case "MJPG":
		img, err := jpeg.Decode(bytes.NewReader(data))
		if err != nil {
			return 0, false
		}
		if yc, ok := img.(*image.YCbCr); ok {
			return planeMean(yc.Y), true
		}
		b := img.Bounds()
		var sum float64
		for y := b.Min.Y; y < b.Max.Y; y++ {
			for x := b.Min.X; x < b.Max.X; x++ {
				r, g, bb, _ := img.At(x, y).RGBA()
				sum += float64(r>>8)*0.299 + float64(g>>8)*0.587 + float64(bb>>8)*0.114
			}
		}
		return sum / float64(b.Dx()*b.Dy()), true
	case "YUY2":
		if len(data) < h*w*2 {
			return 0, false
		}
		var sum float64
		for y := 0; y < h; y++ {
			row := y * w * 2
			for x := 0; x < w; x++ {
				sum += float64(data[row+x*2])
			}
		}
		return sum / float64(w*h), true
	case "NV12":
		if len(data) < h*w {
			return 0, false
		}
		return planeMean(data[:h*w]), true
	}
	return 0, false
}

func planeMean(p []byte) float64 {
	if len(p) == 0 {
		return 0
	}
	var s int64
	for _, v := range p {
		s += int64(v)
	}
	return float64(s) / float64(len(p))
}

// ── IAMVideoProcAmp 探测 ──
//
// IAMCameraControl 只管曝光/对焦/变焦/光圈；**白平衡、增益、饱和度、背光补偿**
// 这些在 IAMVideoProcAmp 上（两者 vtable 布局相同：3=GetRange 4=Set 5=Get）。
var procAmpNames = []string{
	"亮度 Brightness", "对比度 Contrast", "色相 Hue", "饱和度 Saturation",
	"锐度 Sharpness", "Gamma", "彩色开关 ColorEnable", "白平衡 WhiteBalance",
	"背光补偿 BacklightComp", "增益 Gain",
}

func probeProcAmp(cam *MFCamera) {
	iface := cam.VideoProcAmp()
	fmt.Println("\n=== IAMVideoProcAmp（画质/白平衡控制）===")
	if iface == 0 {
		fmt.Println("  接口不可用（相机不支持）")
		return
	}
	defer comRelease(iface)
	any := false
	for i, name := range procAmpNames {
		cr, ok := ctrlGetRange(iface, int32(i))
		if !ok {
			continue
		}
		any = true
		v, f, ok2 := ctrlGet(iface, int32(i))
		mode := "?"
		switch {
		case f == 1:
			mode = "自动"
		case f == 2:
			mode = "手动"
		case f == 0:
			mode = "—"
		}
		caps := ""
		if cr.Caps&1 != 0 {
			caps += "Auto "
		}
		if cr.Caps&2 != 0 {
			caps += "Manual"
		}
		cur := ""
		if ok2 {
			cur = fmt.Sprintf("当前=%d(%s)", v, mode)
		}
		fmt.Printf("  [%d] %-22s 范围 %d..%d 步长 %d 默认 %d  caps=0x%x(%s)  %s\n",
			i, name, cr.Min, cr.Max, cr.Step, cr.Default, cr.Caps, strings.TrimSpace(caps), cur)
	}
	if !any {
		fmt.Println("  相机没暴露任何画质控制")
	}
}

// SetWhiteBalance —— 直接命令相机白平衡。value<0 表示只切换模式不设值。
func SetWhiteBalance(cam *MFCamera, value int32, auto bool) (int32, int32, error) {
	iface := cam.VideoProcAmp()
	if iface == 0 {
		return 0, 0, fmt.Errorf("相机不支持 IAMVideoProcAmp")
	}
	defer comRelease(iface)
	const propWB = 7
	flags := int32(2) // Manual
	if auto {
		flags = 1
	}
	if value >= 0 {
		if err := ctrlSet(iface, propWB, value, flags); err != nil {
			return 0, 0, fmt.Errorf("设置白平衡 %d 失败: %w", value, err)
		}
	} else if auto {
		v, _, ok := ctrlGet(iface, propWB)
		if !ok {
			return 0, 0, fmt.Errorf("读白平衡失败")
		}
		if err := ctrlSet(iface, propWB, v, 1); err != nil {
			return 0, 0, fmt.Errorf("切自动白平衡失败: %w", err)
		}
	}
	v, f, ok := ctrlGet(iface, propWB)
	if !ok {
		return 0, 0, fmt.Errorf("回读白平衡失败")
	}
	return v, f, nil
}
