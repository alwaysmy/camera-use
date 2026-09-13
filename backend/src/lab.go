// lab.go — 原生实现 OpenCV 的 BGR↔LAB（sRGB/D65，8bit 约定）
//
// 为什么非得这么做：
//   YCbCr 的 Cb/Cr 是**伽马编码之后**的差值，而 LAB 的 a/b 作用在**线性光**上。
//   同样"乘以 1.6"放大色度，在伽马空间放大 1.6 倍，折算到线性空间会放大得**远超** 1.6 ——
//   实测同一帧输入：YCbCr 融合把原始色偏 +2.92 放大到 +8.43（2.9x，偏黄发脏），
//   而 LAB 融合只放大到 +4.22（1.45x）。这就是"颜色不对"的根因。
//
// 8bit 约定（与 OpenCV 一致）：
//   L8 = L* * 255/100      a8 = a* + 128      b8 = b* + 128
package main

import (
	"image"
	"math"
)

const (
	labXn = 0.950456
	labYn = 1.0
	labZn = 1.088754
)

var srgbToLinear [256]float64

func init() {
	for i := 0; i < 256; i++ {
		v := float64(i) / 255.0
		if v <= 0.04045 {
			v /= 12.92
		} else {
			v = math.Pow((v+0.055)/1.055, 2.4)
		}
		srgbToLinear[i] = v
	}
}

func fLab(t float64) float64 {
	if t > 0.008856 {
		return math.Cbrt(t)
	}
	return 7.787*t + 16.0/116.0
}

func fLabInv(t float64) float64 {
	if t > 0.2068966 {
		return t * t * t
	}
	return (t - 16.0/116.0) / 7.787
}

func gammaEncode(v float64) float64 { // linear -> sRGB [0,1]
	if v <= 0.0031308 {
		return v * 12.92
	}
	return 1.055*math.Pow(v, 1.0/2.4) - 0.055
}

// cbcrAt —— 色度采样（4:2:0 用双线性，对齐 libjpeg 的 fancy upsampling）。
//
// OpenCV/libjpeg 默认做双线性上采样，而"最近邻"会让色度块状化（实测 PSNR 差 3~5dB）。
// 其它子采样比例按最近邻处理（正常路径都会先过 to420）。
func cbcrAt(img *image.YCbCr, x, y int) (float64, float64) {
	w, h := img.Rect.Dx(), img.Rect.Dy()
	if img.SubsampleRatio != image.YCbCrSubsampleRatio420 {
		cx, cy := x, y
		switch img.SubsampleRatio {
		case image.YCbCrSubsampleRatio422:
			cx = x / 2
		case image.YCbCrSubsampleRatio440:
			cy = y / 2
		case image.YCbCrSubsampleRatio444:
		default:
			cx, cy = x/2, y/2
		}
		if cx >= w {
			cx = w - 1
		}
		if cy >= h {
			cy = h - 1
		}
		i := cy*img.CStride + cx
		if i < 0 || i >= len(img.Cb) {
			return 128, 128
		}
		return float64(img.Cb[i]), float64(img.Cr[i])
	}
	cw, ch := (w+1)/2, (h+1)/2
	if cw < 2 || ch < 2 {
		i := (y/2)*img.CStride + x/2
		if i < 0 || i >= len(img.Cb) {
			return 128, 128
		}
		return float64(img.Cb[i]), float64(img.Cr[i])
	}
	// 色度像素 (cx,cy) 的中心对应 luma (2cx+0.5, 2cy+0.5)
	fx := float64(x)*0.5 - 0.25
	fy := float64(y)*0.5 - 0.25
	x0, y0 := int(math.Floor(fx)), int(math.Floor(fy))
	ax, ay := fx-float64(x0), fy-float64(y0)
	cl := func(v, hi int) int {
		if v < 0 {
			return 0
		}
		if v > hi {
			return hi
		}
		return v
	}
	x0c, x1c := cl(x0, cw-1), cl(x0+1, cw-1)
	y0c, y1c := cl(y0, ch-1), cl(y0+1, ch-1)
	i00 := y0c*img.CStride + x0c
	i10 := y0c*img.CStride + x1c
	i01 := y1c*img.CStride + x0c
	i11 := y1c*img.CStride + x1c
	if i11 >= len(img.Cb) {
		return 128, 128
	}
	bil := func(p []byte) float64 {
		return float64(p[i00])*(1-ax)*(1-ay) + float64(p[i10])*ax*(1-ay) +
			float64(p[i01])*(1-ax)*ay + float64(p[i11])*ax*ay
	}
	return bil(img.Cb), bil(img.Cr)
}

// ycbcrToBGR —— 标准 JFIF 全范围 YCbCr → BGR（色度双线性上采样）
func ycbcrToBGR(img *image.YCbCr) []byte {
	w, h := img.Rect.Dx(), img.Rect.Dy()
	out := make([]byte, w*h*3)
	parallelRows(h, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			yo := y * img.YStride
			oo := y * w * 3
			for x := 0; x < w; x++ {
				Y := float64(img.Y[yo+x])
				cbv, crv := cbcrAt(img, x, y)
				cb := cbv - 128
				cr := crv - 128
				r := Y + 1.402*cr
				g := Y - 0.344136*cb - 0.714136*cr
				b := Y + 1.772*cb
				out[oo+x*3] = clamp8(b)
				out[oo+x*3+1] = clamp8(g)
				out[oo+x*3+2] = clamp8(r)
			}
		}
	})
	return out
}

// bgrToLAB —— BGR 交错 → LAB 交错（8bit 约定）
func bgrToLAB(bgr []byte, w, h int) []byte {
	out := make([]byte, w*h*3)
	parallelRows(h, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			row := y * w * 3
			for x := 0; x < w; x++ {
				i := row + x*3
				bl := srgbToLinear[bgr[i]]
				gl := srgbToLinear[bgr[i+1]]
				rl := srgbToLinear[bgr[i+2]]
				X := 0.412453*rl + 0.357580*gl + 0.180423*bl
				Y := 0.212671*rl + 0.715160*gl + 0.072169*bl
				Z := 0.019334*rl + 0.119193*gl + 0.950227*bl
				fx := fLab(X / labXn)
				fy := fLab(Y / labYn)
				fz := fLab(Z / labZn)
				L := 116.0*fy - 16.0
				a := 500.0 * (fx - fy)
				bb := 200.0 * (fy - fz)
				out[i] = clamp8(L * 255.0 / 100.0)
				out[i+1] = clamp8(a + 128.0)
				out[i+2] = clamp8(bb + 128.0)
			}
		}
	})
	return out
}

// labToBGR —— LAB 交错 → BGR 交错
func labToBGR(lab []byte, w, h int) []byte {
	out := make([]byte, w*h*3)
	parallelRows(h, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			row := y * w * 3
			for x := 0; x < w; x++ {
				i := row + x*3
				L := float64(lab[i]) * 100.0 / 255.0
				a := float64(lab[i+1]) - 128.0
				b := float64(lab[i+2]) - 128.0
				fy := (L + 16.0) / 116.0
				fx := fy + a/500.0
				fz := fy - b/200.0
				X := labXn * fLabInv(fx)
				Y := labYn * fLabInv(fy)
				Z := labZn * fLabInv(fz)
				r := 3.240479*X - 1.537150*Y - 0.498535*Z
				g := -0.969256*X + 1.875992*Y + 0.041556*Z
				bb := 0.055648*X - 0.204043*Y + 1.057311*Z
				out[i] = clamp8(gammaEncode(bb) * 255.0)
				out[i+1] = clamp8(gammaEncode(g) * 255.0)
				out[i+2] = clamp8(gammaEncode(r) * 255.0)
			}
		}
	})
	return out
}

// gaussianKernel —— OpenCV 的 σ 推导：σ = 0.3*((ksize-1)*0.5 - 1) + 0.8
func gaussianKernel(ksize int) []float64 {
	if ksize < 3 {
		ksize = 3
	}
	if ksize%2 == 0 {
		ksize++
	}
	sigma := 0.3*(float64(ksize-1)*0.5-1.0) + 0.8
	k := make([]float64, ksize)
	var sum float64
	c := ksize / 2
	for i := 0; i < ksize; i++ {
		d := float64(i - c)
		k[i] = math.Exp(-(d * d) / (2 * sigma * sigma))
		sum += k[i]
	}
	for i := range k {
		k[i] /= sum
	}
	return k
}

// blurPlaneGaussian —— 可分离高斯（与 OpenCV GaussianBlur 同款核）
func blurPlaneGaussian(p []byte, stride, w, h, ksize int) {
	if ksize < 3 || w < 3 || h < 3 {
		return
	}
	k := gaussianKernel(ksize)
	r := len(k) / 2
	tmp := make([]float64, w*h)
	parallelRows(h, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			row := y * stride
			for x := 0; x < w; x++ {
				var acc float64
				for i, kv := range k {
					sx := x + i - r
					if sx < 0 {
						sx = 0
					} else if sx >= w {
						sx = w - 1
					}
					acc += kv * float64(p[row+sx])
				}
				tmp[y*w+x] = acc
			}
		}
	})
	parallelCols(w, func(x0, x1 int) {
		for x := x0; x < x1; x++ {
			for y := 0; y < h; y++ {
				var acc float64
				for i, kv := range k {
					sy := y + i - r
					if sy < 0 {
						sy = 0
					} else if sy >= h {
						sy = h - 1
					}
					acc += kv * tmp[sy*w+x]
				}
				p[y*stride+x] = clamp8(acc)
			}
		}
	})
}

// measureCast —— 测场景整体色偏（LAB a/b）。
//
// 用**截尾均值**（去掉两端各 10% 再平均）：纯中位数对偏斜分布不够（实测中位数 b=+2.0
// 而均值 b=+5.65，白平衡开到 1.0 仍残留明显色偏）；纯均值又容易被一块高饱和物体拽跑。
func measureCast(lab []byte, w, h int) (float64, float64) {
var ha, hb [256]int
n := w * h
for i := 0; i < n; i++ {
ha[lab[i*3+1]]++
hb[lab[i*3+2]]++
}
tmean := func(hist *[256]int) float64 {
lo, hi := int(0.10*float64(n)), int(0.90*float64(n))
acc, sum, cnt := 0, 0.0, 0
for v := 0; v < 256; v++ {
c := hist[v]
if c == 0 {
continue
}
// 落在 [lo,hi) 之外的部分裁掉
start, end := acc, acc+c
if start < lo {
start = lo
}
if end > hi {
end = hi
}
if end > start {
sum += float64(v) * float64(end-start)
cnt += end - start
}
acc += c
}
if cnt == 0 {
return 0
}
return sum/float64(cnt) - 128.0
}
return tmean(&ha), tmean(&hb)
}

// fuseLAB —— 与 OpenCV 参考实现等价的融合，另加白平衡校准：
//
//	blur(a,b) → 围绕**本帧平均色度**放大 → 按 wb 把整体色偏拉回中性
//
// 公式： a_out = (a - refA) * chroma + refA * (1 - wb)
//
//	refA 为色偏参考（引擎给的平滑值）
//	wb=0 → 保留原色偏，但**不再把色偏放大**（旧实现的问题就在这）
//	wb=1 → 完全中性化（灰世界白平衡）；中间值保留一部分现场气氛
//
// 返回处理后的 BGR，以及本帧实测色偏（供上层做时间平滑与显示）。
func fuseLAB(rgbBGR []byte, w, h int, irW, mask []byte, weight, chroma float64,
	chromaKsize int, wb, refA, refB, gamma, chromaDark float64) ([]byte, float64, float64) {

	lab := bgrToLAB(rgbBGR, w, h)
	castA, castB := measureCast(lab, w, h)
	if chroma != 1.0 {
		if chromaKsize > 1 {
			// a、b 是交错存储，先抽出来分别模糊
			a := make([]byte, w*h)
			bb := make([]byte, w*h)
			for i := 0; i < w*h; i++ {
				a[i] = lab[i*3+1]
				bb[i] = lab[i*3+2]
			}
			blurPlaneGaussian(a, w, w, h, chromaKsize)
			blurPlaneGaussian(bb, w, w, h, chromaKsize)
			for i := 0; i < w*h; i++ {
				lab[i*3+1] = a[i]
				lab[i*3+2] = bb[i]
			}
		}
	}
	ra := refA * (1 - wb)
	rb := refB * (1 - wb)
	parallelRows(h, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			row := y * w
			for x := 0; x < w; x++ {
				i := row + x
				m := 1.0
				if mask != nil {
					m = float64(mask[i]) / 255.0
				}
				ww := weight * m
				if ww > 0 && irW != nil {
					L := float64(lab[i*3])
					lab[i*3] = clamp8(L*(1-ww) + float64(irW[i])*ww)
				}
				if chroma != 1.0 {
					a := float64(lab[i*3+1]) - 128.0
					b := float64(lab[i*3+2]) - 128.0
					lab[i*3+1] = clamp8((a-refA)*chroma + ra + 128.0)
					lab[i*3+2] = clamp8((b-refB)*chroma + rb + 128.0)
				}
				if chromaDark > 0 {
// 暗部色度不可靠（信噪比极差，放大后就是一块块紫/绿）。
// 按亮度往中性收缩：越暗越去饱和，亮部保留颜色。
// 这是"IR 亮的区域在 RGB 里是暗的"那种画面的必备处理 ——
// 否则融合后会出现"发光但发紫"的物体（实测椅子就是这样）。
Lc := float64(lab[i*3])
conf := (Lc - 12.0) / 55.0
if conf < 0 {
conf = 0
} else if conf > 1 {
conf = 1
}
keep := 1.0 - chromaDark*(1.0-conf)
lab[i*3+1] = clamp8(128.0 + (float64(lab[i*3+1])-128.0)*keep)
lab[i*3+2] = clamp8(128.0 + (float64(lab[i*3+2])-128.0)*keep)
}
if gamma != 1.0 {
					// 作用在亮度通道：>1 提亮暗部（暗光下看清楚），<1 压暗
					L := float64(lab[i*3]) / 255.0
					lab[i*3] = clamp8(255.0 * math.Pow(L, 1.0/gamma))
				}
			}
		}
	})
	return labToBGR(lab, w, h), castA, castB
}

// detailLAB —— 只把 IR 的高频细节注入 L 通道（不改整体亮度）
func detailLAB(rgbBGR []byte, w, h int, irW, mask []byte, amount float64) []byte {
	lab := bgrToLAB(rgbBGR, w, h)
	blur := make([]byte, len(irW))
	copy(blur, irW)
	blurPlane(blur, w, w, h, 4)
	parallelRows(h, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			row := y * w
			for x := 0; x < w; x++ {
				i := row + x
				m := 1.0
				if mask != nil {
					m = float64(mask[i]) / 255.0
				}
				d := (float64(irW[i]) - float64(blur[i])) * amount * m
				lab[i*3] = clamp8(float64(lab[i*3]) + d)
			}
		}
	})
	return labToBGR(lab, w, h)
}

