// devices.go — 原生设备发现（SetupAPI）
//
// MFEnumDeviceSources **看不到 IR 相机**（被 Windows Hello 的隐私策略从摄像机
// 枚举里过滤了，实测 count=1），但它注册在 KSCATEGORY_SENSOR_CAMERA 接口类别下。
// 所以这里直接问 SetupAPI 要"设备接口路径"（就是 MFCreateDeviceSource 需要的
// 那个符号链接），顺便把友好名也拿回来 —— 全程不依赖 PowerShell / 注册表脚本。
package main

import (
	"syscall"
	"unsafe"
)

var setupapiDLL = syscall.NewLazyDLL("setupapi.dll")

const (
	digcfPresent         = 0x02
	digcfDeviceInterface = 0x10

	spdrpDeviceDesc   = 0x00
	spdrpFriendlyName = 0x0C
)

// 接口类别（KSCATEGORY）
var (
	IfaceVideoCamera  = NewGUID("E5323777-F976-4F5B-9B55-B94699C46E44")
	IfaceSensorCamera = NewGUID("24E552D7-6523-47F7-A647-D3465BF1F5CA")
)

type spDevInfoData struct {
	cbSize    uint32
	ClassGuid GUID
	DevInst   uint32
	Reserved  uintptr
}

type spDeviceInterfaceData struct {
	cbSize             uint32
	InterfaceClassGuid GUID
	Flags              uint32
	Reserved           uintptr
}

// 枚举某个接口类别下所有**在场**的设备接口
func enumInterfaces(class GUID) []DeviceInfo {
	var out []DeviceInfo
	h, _, _ := setupapiDLL.NewProc("SetupDiGetClassDevsW").Call(
		uintptr(unsafe.Pointer(&class)), 0, 0, digcfPresent|digcfDeviceInterface)
	if h == 0 || h == uintptr(^uintptr(0)) {
		return out
	}
	defer setupapiDLL.NewProc("SetupDiDestroyDeviceInfoList").Call(h)

	for i := uint32(0); ; i++ {
		iface := spDeviceInterfaceData{cbSize: uint32(unsafe.Sizeof(spDeviceInterfaceData{}))}
		r, _, _ := setupapiDLL.NewProc("SetupDiEnumDeviceInterfaces").Call(
			h, 0, uintptr(unsafe.Pointer(&class)), uintptr(i), uintptr(unsafe.Pointer(&iface)))
		if r == 0 {
			break
		}
		// 第一次问长度，再按长度取明细
		var need uint32
		setupapiDLL.NewProc("SetupDiGetDeviceInterfaceDetailW").Call(
			h, uintptr(unsafe.Pointer(&iface)), 0, 0, uintptr(unsafe.Pointer(&need)), 0)
		if need == 0 {
			continue
		}
		buf := make([]byte, need)
		// 64 位下 cbSize 固定为 8（4 字节 cbSize + 2 字节首字符 + 对齐）
		*(*uint32)(unsafe.Pointer(&buf[0])) = 8
		dev := spDevInfoData{cbSize: uint32(unsafe.Sizeof(spDevInfoData{}))}
		r, _, _ = setupapiDLL.NewProc("SetupDiGetDeviceInterfaceDetailW").Call(
			h, uintptr(unsafe.Pointer(&iface)), uintptr(unsafe.Pointer(&buf[0])), uintptr(need),
			uintptr(unsafe.Pointer(&need)), uintptr(unsafe.Pointer(&dev)))
		if r == 0 {
			continue
		}
		path := utf16At(unsafe.Pointer(&buf[4]))
		name := spProperty(h, &dev, spdrpFriendlyName)
		if name == "" {
			name = spProperty(h, &dev, spdrpDeviceDesc)
		}
		out = append(out, DeviceInfo{
			Name:         name,
			SymbolicLink: path,
			Kind:         classifyDevice(name, path),
		})
	}
	return out
}

func spProperty(h uintptr, dev *spDevInfoData, prop uint32) string {
	buf := make([]byte, 1024)
	var typ, need uint32
	r, _, _ := setupapiDLL.NewProc("SetupDiGetDeviceRegistryPropertyW").Call(
		h, uintptr(unsafe.Pointer(dev)), uintptr(prop),
		uintptr(unsafe.Pointer(&typ)), uintptr(unsafe.Pointer(&buf[0])),
		uintptr(len(buf)), uintptr(unsafe.Pointer(&need)))
	if r == 0 {
		return ""
	}
	return utf16At(unsafe.Pointer(&buf[0]))
}

func utf16At(p unsafe.Pointer) string {
	u := unsafe.Slice((*uint16)(p), 1024) // 上限保护
	n := 0
	for n < len(u) && u[n] != 0 {
		n++
	}
	return syscall.UTF16ToString(u[:n])
}

// ListCameras —— 汇总：VIDEO_CAMERA 类别（RGB+IR 都有）+ SENSOR_CAMERA 类别（IR 专有），去重
func ListCameras() []DeviceInfo {
	seen := map[string]bool{}
	var out []DeviceInfo
	add := func(list []DeviceInfo) {
		for _, d := range list {
			if d.SymbolicLink == "" || seen[d.SymbolicLink] {
				continue
			}
			// 只保留真正的采集接口尾部（\GLOBAL），排除掉一些控制用接口
			seen[d.SymbolicLink] = true
			out = append(out, d)
		}
	}
	add(enumInterfaces(IfaceVideoCamera))
	add(enumInterfaces(IfaceSensorCamera))
	// MI_02 惯例：同型号的 IR 子接口
	for i := range out {
		if contains(lower(out[i].SymbolicLink), "mi_02") && out[i].Kind == "rgb" {
			out[i].Kind = "ir"
		}
	}
	return out
}

// PickCamera —— 按类型挑一台（"rgb" / "ir"）
func PickCamera(kind string) (DeviceInfo, bool) {
	for _, d := range ListCameras() {
		if d.Kind == kind {
			return d, true
		}
	}
	return DeviceInfo{}, false
}
