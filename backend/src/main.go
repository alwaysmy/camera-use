// camera_backend — RGB + IR 摄像头控制台（100% 原生 Go，零外部依赖、不依赖外网）
//
// 构成
// ----
//	mf.go      Media Foundation 封装（枚举/打开/抓帧/曝光控制），COM 走 syscall
//	devices.go SetupAPI 设备发现（IR 相机只能这样找出来）
//	camera.go  相机会话：一台相机一个绑线程的 goroutine + 命令队列
//	yuv.go     相机原生格式 → image.YCbCr 4:2:0
//	image.go   图像算子（配准 warp / 融合 / 差分 / 拉伸）
//	align.go   自动配准（梯度图 + 粗到细搜索）
//	engine.go  会话状态 + 渲染循环
//	web.go     HTTP + 嵌入式前端
//
// 用法::
//
//	camera_backend.exe                 # 启动控制台 http://127.0.0.1:8770/
//	camera_backend.exe -list           # 只列设备
//	camera_backend.exe -probe          # 原生链路自检（设备/格式/抓帧/曝光）
//	camera_backend.exe -selftest       # 图像算子 + 自动标定自检（不用相机）
package main

import (
	"flag"
	"fmt"
	"image"
	"image/jpeg"
	"math"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"time"
)

func main() {
	port := flag.Int("port", 8770, "监听端口")
	codec := flag.String("codec", "mjpg", "码流 mjpg|yuy2|nv12|auto")
	width := flag.Int("w", 640, "RGB 宽")
	height := flag.Int("h", 480, "RGB 高")
	noIR := flag.Bool("no-ir", false, "不启用 IR")
	openBr := flag.Bool("open", false, "启动后打开浏览器")
	list := flag.Bool("list", false, "只列出设备")
	probe := flag.Bool("probe", false, "原生采集链路自检")
	wb := flag.Bool("wb", false, "探测相机的 IAMVideoProcAmp（白平衡/增益/饱和度等）")
	vision := flag.Bool("vision", false, "启动时自动拉起旁路视觉进程（YOLO 人脸+骨架，需 python+onnxruntime）")
	rootDir := flag.String("root", "", "资源根目录（默认=可执行文件所在目录，一般不用改）")
	mcp := flag.Bool("mcp", false, "以 MCP 服务器模式运行（stdio，给 agent 用）")
	visionFPS := flag.Float64("vision-fps", 0, "旁路推理频率，0=不限速（默认）")
	wbSet := flag.Int("wb-set", -1, "设置白平衡值（配合 -wb）")
	wbAuto := flag.Bool("wb-auto", false, "切回自动白平衡（配合 -wb）")
	selftest := flag.Bool("selftest", false, "图像算子 + 自动标定自检")
	facetest := flag.String("facetest", "", "对指定图片跑一次人脸检测（诊断用）")
	facewin := flag.String("facewin", "", "诊断：对指定窗口逐级打印判定 x,y,w,h")
	facemin := flag.Int("facemin", 48, "检测最小窗口边长")
	facescale := flag.Float64("facescale", 1.15, "检测尺度步长")
	facenn := flag.Int("facenn", 3, "最少邻居票数")
	flag.Parse()
	initBaseDir(*rootDir) // 路径锚定：先定资源根目录，后面所有 models/calib/captures 都相对它

	if *facetest != "" {
		runFaceTest(*facetest, *facewin, *facemin, *facescale, *facenn)
		return
	}

	if *list {
		listDevices()
		return
	}
	if *probe {
		MFProbe()
		return
	
	}
	if *vision {
		visionSidecarOn = true
		visionSidecarFPS = *visionFPS
	}
	if *wb {
		runWhiteBalance(*wbSet, *wbAuto)
		return
	}
	if *selftest {
		runSelfTest()
		return
	}

	e := NewEngine()
	// -mcp：走 MCP（stdio）给 agent 用
	//
	// ⚠ 必须在 e.Start() **之前**返回：MCP 模式**不预开相机**，相机在工具调用时
	// 由 EnsureOpen() 懒加载（空闲 60s 再释放）。
	// 否则每次 MCP 客户端启动服务器进程都会打开摄像头——占着设备、亮着指示灯，
	// 而 agent 可能整轮都不调用相机工具。
	if *mcp {
		e.SetMCPMode(true)
		if *vision {
			visionSidecarOn = true
			visionSidecarFPS = *visionFPS
		}
		RunMCP(e)
		return
	}

	// 以下为 HTTP 控制台模式：这里才需要常开相机
	if err := e.Start(*codec, *width, *height, !*noIR); err != nil {
		fmt.Fprintln(os.Stderr, "启动失败:", err)
		os.Exit(1)
	}
	addr := fmt.Sprintf("127.0.0.1:%d", *port)
	_, _, cfg := e.Stats()
	fmt.Printf("[backend] 资源目录 %s\n", RootDir())
	fmt.Printf("[backend] Go %s / %d 核 | RGB %s %dx%d | IR %v\n",
		runtime.Version(), runtime.NumCPU(), cfg.Codec, cfg.W, cfg.H, cfg.IR)
	fmt.Printf("[backend] 控制台 http://%s/   (Ctrl+C 退出)\n", addr)
	if *openBr {
		go func() {
			time.Sleep(700 * time.Millisecond)
			_ = exec.Command("cmd", "/c", "start", "", "http://"+addr+"/").Start()
		}()
	}
	srv := &http.Server{Addr: addr, Handler: e.Routes()}
	if err := srv.ListenAndServe(); err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
}

func listDevices() {
	cams := ListCameras()
	if len(cams) == 0 {
		fmt.Println("没有发现相机")
		return
	}
	fmt.Println("原生设备枚举（SetupAPI 接口类别）:")
	for _, d := range cams {
		fmt.Printf("  [%-3s] %s\n        %s\n", d.Kind, d.Name, d.SymbolicLink)
	}
}

// runSelfTest —— 不用相机也能验证算子正确性与性能
func runSelfTest() {
	const w, h = 640, 480
	base := image.NewYCbCr(image.Rect(0, 0, w, h), image.YCbCrSubsampleRatio420)
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			base.Y[y*base.YStride+x] = uint8((x*3 + y*5) % 256)
		}
	}
	for i := range base.Cb {
		base.Cb[i] = uint8((i * 7) % 256)
		base.Cr[i] = uint8((i * 11) % 256)
	}
	// 用**平滑的多尺度纹理**当测试目标 —— 纯随机/高频图案在降采样时会混叠，
	// 多尺度搜索本来就会被带偏，那样的自检没有意义。
	ir := make([]byte, 340*340)
	for y := 0; y < 340; y++ {
		for x := 0; x < 340; x++ {
			v := 128 + 55*math.Sin(float64(x)/17.0)*math.Cos(float64(y)/23.0) +
				28*math.Sin(float64(x+y)/9.0) + 18*math.Cos(float64(x-y)/31.0)
			ir[y*340+x] = clamp8(v)
		}
	}

	fmt.Println("=== 算子耗时（640×480 + 340×340）===")
	t0 := time.Now()
	irW, mask := warpGray(ir, 340, 340, 1.3, 0, 98, 18, w, h)
	t1 := time.Now()
	out := fuseYCbCr(base, irW, mask, 0.62, 1.6, 2)
	t2 := time.Now()
	b := newByteWriter(1 << 20)
	_ = jpeg.Encode(b, out, &jpeg.Options{Quality: 80})
	t3 := time.Now()
	fmt.Printf("  warpGray    %6.2f ms\n", ms(t0, t1))
	fmt.Printf("  fuseYCbCr   %6.2f ms\n", ms(t1, t2))
	fmt.Printf("  jpeg encode %6.2f ms (%d KB)\n", ms(t2, t3), len(b.Bytes())/1024)
	fmt.Printf("  合计        %6.2f ms → 单核 %.0f fps\n", ms(t0, t3), 1000/ms(t0, t3))

	// LAB 路径（与 OpenCV 参考实现同款色彩数学）
	t4 := time.Now()
	bgr := ycbcrToBGR(base)
	t5 := time.Now()
	lout, castA, castB := fuseLAB(bgr, w, h, irW, mask, 0.62, 1.25, 7, 0.6, 0, 0, 1.0, 0.6)
	_ = castA
	_ = castB
	t6 := time.Now()
	_ = bgrBytesTo420(lout, w, h)
	t7 := time.Now()
	fmt.Printf("\n=== LAB 融合路径 ===\n")
	fmt.Printf("  ycbcr→bgr   %6.2f ms\n", ms(t4, t5))
	fmt.Printf("  fuseLAB     %6.2f ms（含 bgr↔LAB 两次换算 + 7x7 高斯）\n", ms(t5, t6))
	fmt.Printf("  bgr→ycbcr   %6.2f ms\n", ms(t6, t7))
	fmt.Printf("  LAB 合计    %6.2f ms → 单核 %.0f fps\n", ms(t4, t7), 1000/ms(t4, t7))

	// 人脸检测自检：加载 cascade + 对最新一帧运行
	t0f := time.Now()
	cas, cerr := LoadFaceCascade()
	if cerr != nil {
		fmt.Println("\n人脸检测：cascade 加载失败:", cerr)
	} else {
		fmt.Printf("\n=== 人脸检测（Viola-Jones 原生实现）===\n")
		tot := 0
		for i := range cas.Stages {
			tot += len(cas.Stages[i].stumps)
		}
		fmt.Printf("  cascade: %d stages / %d features / %d 弱分类器 / %dx%d 窗口（解析 %.0f ms）\n",
			len(cas.Stages), len(cas.Features), tot, cas.WinW, cas.WinH, ms(t0f, time.Now()))
		// 造一张带"脸"的合成图：中间偏上放一个亮椭圆块
		gw, gh := 320, 240
		fg := make([]byte, gw*gh)
		for i := range fg {
			fg[i] = 90
		}
		for yy := 40; yy < 150; yy++ {
			for xx := 110; xx < 210; xx++ {
				dx := float64(xx-160) / 50.0
				dy := float64(yy-95) / 60.0
				if dx*dx+dy*dy < 1 {
					fg[yy*gw+xx] = uint8(170 + (xx+yy)%40)
				}
			}
		}
		t1f := time.Now()
		fs := cas.Detect(fg, gw, gh, 48, 200, 1.15, 3)
		fmt.Printf("  合成图检测耗时 %.0f ms，检出 %d 张\n", ms(t1f, time.Now()), len(fs))
	}

	// measureCast 自检：造一张已知暖色偏的图
	warm := make([]byte, w*h*3)
	for i := 0; i < w*h; i++ {
		warm[i*3], warm[i*3+1], warm[i*3+2] = 110, 125, 145 // B<G<R = 暖
	}
	lb := bgrToLAB(warm, w, h)
	ca, cb := measureCast(lb, w, h)
	fmt.Printf("measureCast 自检：暖色图(B110 G125 R145) → a=%+.2f b=%+.2f（应为正）\n", ca, cb)

	// 自动标定回归：用已知变换造一对图，看能不能估回来
	fmt.Println("\n=== 自动标定回归（已知变换 1.30 / 98 / 18）===")
	// 把 ir 用已知变换铺到 RGB 尺寸，当作"RGB 视角下的同一场景"
	syn, _ := warpGray(ir, 340, 340, 1.30, 0, 98, 18, w, h)
	// 加一点噪声，模拟跨模态差异
	for i := range syn {
		syn[i] = clampInt(int(syn[i]) + int((i*13)%17) - 8)
	}
	tA := time.Now()
	res := AutoAlign(syn, w, h, ir, 340, 340, 1.30, 98, 18)
	dtA := time.Since(tA).Seconds() * 1000
	fmt.Printf("  估出 scale=%.3f tx=%.1f ty=%.1f 相关度=%.3f（耗时 %.0f ms）\n",
		res.Scale, res.Tx, res.Ty, res.Score, dtA)
	errS := math.Abs(res.Scale-1.30) / 1.30
	errTx := math.Abs(res.Tx - 98)
	errTy := math.Abs(res.Ty - 18)
	if errS < 0.05 && errTx < 12 && errTy < 12 {
		fmt.Println("  ✅ 标定回归通过（尺度误差 <5%，平移误差 <12px）")
	} else {
		fmt.Printf("  ❌ 标定回归偏差过大：scale %.1f%% / tx %.0fpx / ty %.0fpx\n",
			errS*100, errTx, errTy)
	}
}

func ms(a, b time.Time) float64 { return float64(b.Sub(a).Microseconds()) / 1000 }


// runFaceTest —— 对一张图片跑人脸检测（不需要相机）
func runFaceTest(path, facewin string, facemin int, facescale float64, facenn int) {
f, err := os.Open(path)
if err != nil {
fmt.Println("打开失败:", err)
return
}
defer f.Close()
img, _, err := image.Decode(f)
if err != nil {
fmt.Println("解码失败:", err)
return
}
b := img.Bounds()
gray := make([]byte, b.Dx()*b.Dy())
for y := 0; y < b.Dy(); y++ {
for x := 0; x < b.Dx(); x++ {
r, g, bl, _ := img.At(b.Min.X+x, b.Min.Y+y).RGBA()
gray[y*b.Dx()+x] = uint8((19595*int(r>>8) + 38470*int(g>>8) + 7471*int(bl>>8)) >> 16)
}
}
cas, err := LoadFaceCascade()
if err != nil {
fmt.Println("cascade 加载失败:", err)
return
}
fmt.Printf("图片 %dx%d，cascade %d stages\n", b.Dx(), b.Dy(), len(cas.Stages))
for _, f := range []int{1, 2} { // 原图 + 二分之一图各跑一次
g, gw, gh := gray, b.Dx(), b.Dy()
if f == 2 {
g, gw, gh = downscalePlane(gray, b.Dx(), b.Dy(), 2)
}
t := time.Now()
faces := cas.Detect(g, gw, gh, facemin, 0, facescale, facenn)
fmt.Printf("  1/%d 图 (%dx%d): %d ms，检出 %d 张", f, gw, gh, int(ms(t, time.Now())), len(faces))
for _, fc := range faces {
fmt.Printf("  [%d,%d %dx%d]", fc.X*f, fc.Y*f, fc.W*f, fc.H*f)
}
fmt.Println()
}
	if facewin != "" {
		var x, y, w2, h2 int
		if _, err := fmt.Sscanf(facewin, "%d,%d,%d,%d", &x, &y, &w2, &h2); err == nil {
			fmt.Println("=== 窗口级诊断 ===")
			cas.DebugWindow(gray, b.Dx(), b.Dy(), x, y, w2, h2)
		} else {
			fmt.Println("facewin 解析失败:", err)
		}
	}
}


// runWhiteBalance —— 探测/设置相机白平衡（不需要开控制台）
func runWhiteBalance(setVal int, auto bool) {
	if err := lockCOMThread(); err != nil {
		fmt.Println("COM/MF 初始化失败:", err)
		return
	}
	defer mfShutdown()

	dev, found := PickCamera("rgb")
	if !found {
		fmt.Println("没找到 RGB 相机")
		return
	}
	fmt.Printf("RGB 相机: %s\n", dev.Name)
	cam, err := OpenCameraByLink(dev.SymbolicLink)
	if err != nil {
		fmt.Println("打开相机失败:", err)
		return
	}
	defer cam.Close()

	probeProcAmp(cam)

	if setVal >= 0 || auto {
		fmt.Println("\n=== 尝试设置白平衡 ===")
		v, f, err := SetWhiteBalance(cam, int32(setVal), auto)
		if err != nil {
			fmt.Println("  失败:", err)
			return
		}
		fmt.Printf("  设置完成 → 当前=%d flags=%d(%s)\n", v, f,
			map[int32]string{1: "自动", 2: "手动", 0: "—"}[f])
	}
}


// startVisionSidecar —— 拉起旁路视觉进程（失败不影响主程序：人脸会回退到原生 Viola-Jones）
func startVisionSidecar(fps float64) {
py := "python"
args := []string{P("tools", "vision_sidecar.py"), "--fps", fmt.Sprintf("%.1f", fps)}
if _, err := os.Stat(P("tools", "vision_sidecar.py")); err != nil {
fmt.Println("[vision] 找不到 tools/vision_sidecar.py，跳过旁路")
return
}
if _, err := os.Stat(P("models", "yolov8n-pose.onnx")); err != nil {
fmt.Println("[vision] 缺 ONNX 模型（先跑 python tools/export_models.py），跳过旁路")
return
}
cmd := exec.Command(py, args...)
cmd.Stdout = os.Stdout
cmd.Stderr = os.Stderr
if err := cmd.Start(); err != nil {
fmt.Println("[vision] 拉起失败:", err)
return
}
fmt.Printf("[vision] 旁路已启动 pid=%d（%.0ffps，Ctrl+C 退出时一并结束）\n", cmd.Process.Pid, fps)
go func() { _ = cmd.Wait() }()
}
