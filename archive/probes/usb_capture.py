"""
usb_capture.py — USB抓包分析摄像头通信
步骤：
1. 找到摄像头的USB接口
2. 开始USB抓包
3. 启动Windows相机（触发正常曝光）
4. 停止抓包
5. 分析pcap文件
"""
import subprocess
import time
import os
import glob

def find_camera_usb_interface():
    """找到摄像头的USB接口编号"""
    print("查找摄像头USB接口...")
    
    # 获取所有USB设备
    result = subprocess.run(
        ["powershell", "-Command", 
         "Get-PnpDevice -Class Camera | Select-Object -ExpandProperty InstanceId"],
        capture_output=True, text=True, encoding="utf-8"
    )
    
    cameras = result.stdout.strip().split('\n')
    print(f"找到摄像头: {cameras}")
    
    # 获取USB控制器
    result = subprocess.run(
        ["powershell", "-Command",
         "Get-PnpDevice -Class 'USB' -Status OK | Select-Object FriendlyName, InstanceId | ConvertTo-Json"],
        capture_output=True, text=True, encoding="utf-8"
    )
    
    return cameras

def start_usb_capture(output_file, duration=10):
    """开始USB抓包"""
    print(f"开始USB抓包 -> {output_file}")
    
    # 列出可用的USBPcap接口
    result = subprocess.run(
        ["C:\\Program Files\\USBPcap\\USBPcapCMD.exe", "--list-interfaces"],
        capture_output=True, text=True
    )
    print(f"可用接口:\n{result.stdout}")
    
    # 使用接口1（通常是第一个USB控制器）
    # 开始抓包，持续duration秒
    cmd = [
        "C:\\Program Files\\USBPcap\\USBPcapCMD.exe",
        "-d", "USBPcap1",
        "-w", output_file,
        "-a"  # 自动停止
    ]
    
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(1)
    
    return proc

def analyze_pcap(pcap_file):
    """分析pcap文件"""
    print(f"\n分析 {pcap_file}...")
    
    # 使用tshark（Wireshark命令行）分析
    tshark_path = "C:\\Program Files\\Wireshark\\tshark.exe"
    
    if not os.path.exists(tshark_path):
        print("tshark未找到，请手动用Wireshark打开pcap文件")
        return
    
    # 过滤USB控制传输
    cmd = [
        tshark_path,
        "-r", pcap_file,
        "-Y", "usb.transfer_type == 0x02",  # Control transfer
        "-T", "fields",
        "-e", "frame.time",
        "-e", "usb.transfer_type",
        "-e", "usb.request_type",
        "-e", "usb.request",
        "-e", "usb.value",
        "-e", "usb.index",
        "-e", "usb.data_len",
        "-e", "usb.capdata",
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    print("\nUSB控制传输:")
    print("-" * 100)
    lines = result.stdout.strip().split('\n')
    for i, line in enumerate(lines[:50]):  # 只显示前50行
        print(f"{i+1:4d}: {line}")
    
    if len(lines) > 50:
        print(f"... 共 {len(lines)} 条记录")
    
    # 查找UVC特定请求
    print("\n\n查找UVC视频控制请求...")
    cmd_uvc = [
        tshark_path,
        "-r", pcap_file,
        "-Y", "usb.transfer_type == 0x02 && usb.request_type == 0x21",  # Class-specific
        "-T", "fields",
        "-e", "frame.time_relative",
        "-e", "usb.request",
        "-e", "usb.value", 
        "-e", "usb.index",
        "-e", "usb.capdata",
    ]
    
    result_uvc = subprocess.run(cmd_uvc, capture_output=True, text=True)
    
    if result_uvc.stdout.strip():
        print("\nUVC类特定请求 (可能包含曝光设置):")
        print("-" * 100)
        for line in result_uvc.stdout.strip().split('\n')[:30]:
            print(line)
    else:
        print("未找到UVC类特定请求")

def main():
    output_dir = os.getcwd()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    pcap_file = os.path.join(output_dir, f"usb_capture_{timestamp}.pcapng")
    
    print("=" * 70)
    print("  USB摄像头通信抓包工具")
    print("=" * 70)
    
    # 查找摄像头
    cameras = find_camera_usb_interface()
    
    print("\n步骤1: 开始USB抓包...")
    capture_proc = start_usb_capture(pcap_file)
    
    print("\n步骤2: 等待抓包稳定...")
    time.sleep(2)
    
    print("\n步骤3: 启动Windows相机...")
    subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
    time.sleep(5)  # 等待相机打开并初始化摄像头
    
    print("\n步骤4: 关闭Windows相机...")
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(2)
    
    print("\n步骤5: 停止抓包...")
    capture_proc.terminate()
    capture_proc.wait()
    
    print(f"\n抓包文件已保存: {pcap_file}")
    
    # 分析抓包文件
    print("\n步骤6: 分析抓包数据...")
    analyze_pcap(pcap_file)
    
    print("\n" + "=" * 70)
    print("  分析完成")
    print("=" * 70)

if __name__ == "__main__":
    main()
