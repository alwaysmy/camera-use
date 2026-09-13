// align.go — 原生自动配准（粗到细搜索）
//
// 跨模态配准不能靠 ORB/SIFT（实测会被背景纹理骗走，解是错的），
// 也不能靠朴素归一化互相关全搜索（640×480 上要 1e9 次运算）。
// 这里用：**梯度图 + 1/4 分辨率粗搜 + 全分辨率局部精搜**。
//
//   粗搜：1/4 图（160×120）上按 2 像素步长滑窗，模板取中心 60% 且隔点采样
//         ≈ 每尺度几万次运算，31 个尺度总共不到 1000 万次 —— 几十毫秒。
//   精搜：全分辨率上在粗解附近 ±8 像素、±0.03 尺度内细找。
package main

import (
	"math"
)

type AlignResult struct {
	Scale float64 `json:"scale"`
	Tx    float64 `json:"tx"`
	Ty    float64 `json:"ty"`
	Score float64 `json:"score"`
	OK    bool    `json:"ok"`
}

// gradMag —— 梯度幅值（跨模态下比灰度本身稳得多）
func gradMag(src []byte, w, h int) []float32 {
	g := make([]float32, w*h)
	for y := 1; y < h-1; y++ {
		for x := 1; x < w-1; x++ {
			i := y*w + x
			gx := float32(int(src[i+1]) - int(src[i-1]))
			gy := float32(int(src[i+w]) - int(src[i-w]))
			g[i] = float32(math.Sqrt(float64(gx*gx + gy*gy)))
		}
	}
	return g
}

// downscalePlane —— 简单 2x2 平均降采样
func downscalePlane(src []byte, w, h, factor int) ([]byte, int, int) {
	dw, dh := w/factor, h/factor
	out := make([]byte, dw*dh)
	for y := 0; y < dh; y++ {
		for x := 0; x < dw; x++ {
			var s, n int
			for dy := 0; dy < factor; dy++ {
				row := (y*factor + dy) * w
				for dx := 0; dx < factor; dx++ {
					s += int(src[row+x*factor+dx])
					n++
				}
			}
			out[y*dw+x] = uint8(s / n)
		}
	}
	return out, dw, dh
}

// resizeFloat —— 最近邻缩放（配准搜索里够用，快）
func resizeFloat(src []float32, sw, sh, dw, dh int) []float32 {
	out := make([]float32, dw*dh)
	for y := 0; y < dh; y++ {
		sy := y * sh / dh
		for x := 0; x < dw; x++ {
			sx := x * sw / dw
			out[y*dw+x] = src[sy*sw+sx]
		}
	}
	return out
}

// nccScore —— 零均值归一化相关的近似：只对模板做零均值，图像侧用能量归一化。
// 位置限制在画布内；模板按 stride 隔点采样以省时间。
func nccScore(img []float32, iw, ih int, tmpl []float32, tw, th, tx, ty, stride int) float64 {
	if tx < 0 || ty < 0 || tx+tw > iw || ty+th > ih {
		return -1e9
	}
	var dot, eImg float64
	for y := 0; y < th; y += stride {
		row := (ty + y) * iw
		trow := y * tw
		for x := 0; x < tw; x += stride {
			g := float64(img[row+tx+x])
			t := float64(tmpl[trow+x])
			dot += t * g
			eImg += g * g
		}
	}
	if eImg <= 1e-6 {
		return -1e9
	}
	// 模板已是零均值，其能量是常数（在调用前归一化过）
	return dot / math.Sqrt(eImg)
}

// zeroMean —— 原地零均值并返回模板能量
func zeroMean(t []float32) float64 {
	if len(t) == 0 {
		return 0
	}
	var s float64
	for _, v := range t {
		s += float64(v)
	}
	m := s / float64(len(t))
	var e float64
	for i := range t {
		t[i] = float32(float64(t[i]) - m)
		e += float64(t[i]) * float64(t[i])
	}
	return math.Sqrt(e)
}

// AutoAlign —— 输入 RGB 的 Y 平面与 IR 灰度，输出 IR→RGB 的相似变换
func AutoAlign(rgbY []byte, rw, rh int, ir []byte, iw, ih int,
	seedScale, seedTx, seedTy float64) AlignResult {

	// ── 粗搜（1/4 分辨率）──
	const F = 4
	rgbQ, qw, qh := downscalePlane(rgbY, rw, rh, F)
	irQ := make([]byte, (iw/F)*(ih/F))
	for y := 0; y < ih/F; y++ {
		for x := 0; x < iw/F; x++ {
			irQ[y*(iw/F)+x] = ir[(y*F)*(iw)+x*F]
		}
	}
	gRgb := gradMag(rgbQ, qw, qh)
	gIrBase := gradMag(irQ, iw/F, ih/F)
	baseW, baseH := iw/F, ih/F

	best := AlignResult{Score: -1e9}
	scaleLo := math.Max(0.4, seedScale-0.35)
	scaleHi := math.Min(2.6, seedScale+0.35)
	for s := scaleLo; s <= scaleHi; s += 0.025 {
		tw, th := int(float64(baseW)*s), int(float64(baseH)*s)
		if tw < 24 || th < 24 || tw >= qw || th >= qh {
			continue
		}
		gIr := resizeFloat(gIrBase, baseW, baseH, tw, th)
		// 模板取中心 60%
		cw, ch := int(float64(tw)*0.6), int(float64(th)*0.6)
		if cw < 16 || ch < 16 {
			continue
		}
		cx, cy := (tw-cw)/2, (th-ch)/2
		tmpl := make([]float32, cw*ch)
		for y := 0; y < ch; y++ {
			copy(tmpl[y*cw:(y+1)*cw], gIr[(cy+y)*tw+cx:(cy+y)*tw+cx+cw])
		}
		if zeroMean(tmpl) < 1e-3 {
			continue
		}
		step := 1 // 1/4 分辨率下步长 1 = 全分辨率 4px；再粗就会让精搜窗口兜不住
		for ty := 0; ty+th <= qh; ty += step {
			for tx := 0; tx+tw <= qw; tx += step {
				sc := nccScore(gRgb, qw, qh, tmpl, cw, ch, tx+cx, ty+cy, 2)
				if sc > best.Score {
					best.Score = sc
					best.Scale = s
					// 模板左上在 (tx+cx, ty+cy)，对应 IR_s 的左上 = (tx, ty)
					best.Tx = float64(tx * F)
					best.Ty = float64(ty * F)
				}
			}
		}
	}
	if best.Score < -1e8 {
		return AlignResult{OK: false}
	}

	// ── 精搜（全分辨率，粗解附近 ±8 像素 / ±0.03 尺度）──
	gFull := gradMag(rgbY, rw, rh)
	gIrFull := gradMag(ir, iw, ih)
	refined := best
	for s := best.Scale - 0.03; s <= best.Scale+0.03; s += 0.005 {
		tw, th := int(float64(iw)*s), int(float64(ih)*s)
		if tw < 40 || th < 40 || tw >= rw || th >= rh {
			continue
		}
		cw, ch := int(float64(tw)*0.6), int(float64(th)*0.6)
		cx, cy := (tw-cw)/2, (th-ch)/2
		tmpl := make([]float32, cw*ch)
		for y := 0; y < ch; y++ {
			sy := (cy + y) * ih / th
			for x := 0; x < cw; x++ {
				sx := (cx + x) * iw / tw
				tmpl[y*cw+x] = gIrFull[sy*iw+sx]
			}
		}
		if zeroMean(tmpl) < 1e-3 {
			continue
		}
		baseTx, baseTy := int(best.Tx), int(best.Ty)
		for dy := -18; dy <= 18; dy += 1 {
			for dx := -18; dx <= 18; dx += 1 {
				tx, ty := baseTx+dx, baseTy+dy
				if tx < 0 || ty < 0 || tx+tw > rw || ty+th > rh {
					continue
				}
				sc := nccScore(gFull, rw, rh, tmpl, cw, ch, tx+cx, ty+cy, 3)
				if sc > refined.Score {
					refined = AlignResult{Scale: s, Tx: float64(tx), Ty: float64(ty), Score: sc, OK: true}
				}
			}
		}
	}
	refined.OK = true
	return refined
}
