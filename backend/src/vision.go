// vision.go — 旁路视觉推理结果的接收与叠加绘制
//
// 定位：YOLO 人脸 / 姿态这类需要 DNN 的重活交给**旁路进程**（Python + onnxruntime），
// Go 主程序只负责：拿干净帧给它 → 收回结构化结果 → 画到画面上。
// 主程序零依赖不变；旁路没跑就自动回退（人脸仍可用原生 Viola-Jones）。
package main

import (
	"image"
	"sync"
	"time"
)

// VisionBox —— 归一化到原图的框（像素坐标）
type VisionBox struct {
	X     float64 `json:"x"`
	Y     float64 `json:"y"`
	W     float64 `json:"w"`
	H     float64 `json:"h"`
	Score float64 `json:"score"`
}

// VisionPose —— 一个人 + 17 个 COCO 关键点（x, y, conf）
type VisionPose struct {
	Box   [4]float64 `json:"box"` // x1,y1,x2,y2
	Score float64    `json:"score"`
	Kpts  []float64  `json:"kpts"` // 17*3 展平
}

// VisionHand —— 一只手：21 个 MediaPipe 关键点 + 五指伸展 + 手势名
type VisionHand struct {
	Hand    string    `json:"hand"`    // Left / Right
	Gesture string    `json:"gesture"` // 五指张开 / 握拳 / 剪刀手 ...
	Fingers []int     `json:"fingers"` // 5 个 0/1
	Kpts    []float64 `json:"kpts"`    // 21*3 展平（x,y,conf，归一化坐标）
}

// VisionResult —— 旁路一次推理的完整结果
type VisionResult struct {
	Faces  []VisionBox  `json:"faces"`
	Poses  []VisionPose `json:"poses"`
	Hands  []VisionHand `json:"hands"`
	Model  string       `json:"model"`
	MsFace float64      `json:"ms_face"`
	MsPose float64      `json:"ms_pose"`
	MsHand float64      `json:"ms_hand"`
	FrameW int          `json:"frame_w"`
	FrameH int          `json:"frame_h"`
	At     time.Time    `json:"-"`
}

type visionStore struct {
	mu     sync.Mutex
	res    VisionResult
	outW   int
	outH   int // 旁路推理时用的画面尺寸（用于把坐标缩放到当前帧）
	live   bool
}

func (v *visionStore) set(r VisionResult, w, h int) {
	v.mu.Lock()
	r.At = time.Now()
	v.res = r
	v.outW, v.outH = w, h
	v.live = true
	v.mu.Unlock()
}

func (v *visionStore) get() (VisionResult, int, int, bool) {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.res, v.outW, v.outH, v.live
}

// fresh —— 结果是否够新（旁路挂了就别再画旧框）
func (v *visionStore) fresh(maxAge time.Duration) bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.live && time.Since(v.res.At) < maxAge
}

// ── 彩色绘制工具（YCbCr 里也能画彩色：Y 逐像素，Cb/Cr 每 2×2 一个）──

var (
	colFace  = [3]byte{150, 43, 21}   // 绿
	colBody  = [3]byte{178, 171, 1}   // 青
	colLimb  = [3]byte{226, 1, 149}   // 黄
	colKpt   = [3]byte{105, 212, 235} // 品红
)

func putCol(img *image.YCbCr, x, y int, c [3]byte) {
	w, h := img.Rect.Dx(), img.Rect.Dy()
	if x < 0 || y < 0 || x >= w || y >= h {
		return
	}
	img.Y[y*img.YStride+x] = c[0]
	ci := (y/2)*img.CStride + x/2
	if ci >= 0 && ci < len(img.Cb) {
		img.Cb[ci] = c[1]
		img.Cr[ci] = c[2]
	}
}

func drawLineC(img *image.YCbCr, x0, y0, x1, y1 int, c [3]byte, thick int) {
	dx, dy := x1-x0, y1-y0
	n := dx
	if n < 0 {
		n = -n
	}
	if m := dy; m < 0 {
		if -m > n {
			n = -m
		}
	} else if m > n {
		n = m
	}
	if n == 0 {
		putCol(img, x0, y0, c)
		return
	}
	for i := 0; i <= n; i++ {
		x := x0 + dx*i/n
		y := y0 + dy*i/n
		for tx := 0; tx < thick; tx++ {
			for ty := 0; ty < thick; ty++ {
				putCol(img, x+tx, y+ty, c)
			}
		}
	}
}

func drawRectC(img *image.YCbCr, x0, y0, x1, y1 int, c [3]byte, thick int) {
	drawLineC(img, x0, y0, x1, y0, c, thick)
	drawLineC(img, x0, y1, x1, y1, c, thick)
	drawLineC(img, x0, y0, x0, y1, c, thick)
	drawLineC(img, x1, y0, x1, y1, c, thick)
}

// COCO 17 点骨架连接
var cocoPairs = [][2]int{{0, 1}, {0, 2}, {1, 3}, {2, 4}, {5, 7}, {7, 9}, {6, 8}, {8, 10},
	{5, 6}, {5, 11}, {6, 12}, {11, 12}, {11, 13}, {13, 15}, {12, 14}, {14, 16}}

// MediaPipe 手部 21 点骨架
var handBones = [][2]int{{0, 1}, {1, 2}, {2, 3}, {3, 4}, {0, 5}, {5, 6}, {6, 7}, {7, 8},
	{5, 9}, {9, 10}, {10, 11}, {11, 12}, {9, 13}, {13, 14}, {14, 15}, {15, 16},
	{13, 17}, {17, 18}, {18, 19}, {19, 20}, {0, 17}}

var colHand = [3]byte{180, 100, 200} // 偏紫，跟人脸/人形区分

// drawHands —— 手部骨架（关键点是**归一化坐标** 0..1，乘画面尺寸即可）
func drawHands(img *image.YCbCr, hands []VisionHand) {
	w, h := img.Rect.Dx(), img.Rect.Dy()
	for _, hd := range hands {
		if len(hd.Kpts) < 63 {
			continue
		}
		pt := func(i int) (int, int) {
			return int(hd.Kpts[i*3] * float64(w)), int(hd.Kpts[i*3+1] * float64(h))
		}
		for _, b := range handBones {
			x0, y0 := pt(b[0])
			x1, y1 := pt(b[1])
			drawLineC(img, x0, y0, x1, y1, colHand, 2)
		}
		for _, i := range []int{4, 8, 12, 16, 20} { // 指尖画粗点
			x, y := pt(i)
			for tx := -3; tx <= 3; tx++ {
				for ty := -3; ty <= 3; ty++ {
					putCol(img, x+tx, y+ty, colKpt)
				}
			}
		}
	}
}

// drawVision —— 把旁路结果画到当前画面上（会自动按尺寸比例换算）
func drawVision(img *image.YCbCr, r VisionResult, srcW, srcH int, drawFace, drawPose, drawHand bool) {
	w, h := img.Rect.Dx(), img.Rect.Dy()
	if srcW <= 0 || srcH <= 0 {
		srcW, srcH = w, h
	}
	sx := float64(w) / float64(srcW)
	sy := float64(h) / float64(srcH)

	if drawFace {
		for _, f := range r.Faces {
			x0 := int(f.X * sx)
			y0 := int(f.Y * sy)
			x1 := int((f.X + f.W) * sx)
			y1 := int((f.Y + f.H) * sy)
			drawRectC(img, x0, y0, x1, y1, colFace, 2)
		}
	}
	if drawHand {
		drawHands(img, r.Hands)
	}
	if drawPose {
		for _, p := range r.Poses {
			drawRectC(img, int(p.Box[0]*sx), int(p.Box[1]*sy), int(p.Box[2]*sx), int(p.Box[3]*sy), colBody, 1)
			if len(p.Kpts) < 51 {
				continue
			}
			for _, pr := range cocoPairs {
				a, b := pr[0], pr[1]
				if p.Kpts[a*3+2] < 0.5 || p.Kpts[b*3+2] < 0.5 {
					continue
				}
				drawLineC(img,
					int(p.Kpts[a*3]*sx), int(p.Kpts[a*3+1]*sy),
					int(p.Kpts[b*3]*sx), int(p.Kpts[b*3+1]*sy), colLimb, 2)
			}
			for k := 0; k < 17; k++ {
				if p.Kpts[k*3+2] < 0.5 {
					continue
				}
				x := int(p.Kpts[k*3] * sx)
				y := int(p.Kpts[k*3+1] * sy)
				for tx := -2; tx <= 2; tx++ {
					for ty := -2; ty <= 2; ty++ {
						putCol(img, x+tx, y+ty, colKpt)
					}
				}
			}
		}
	}
}
