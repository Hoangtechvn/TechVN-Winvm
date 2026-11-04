#!/usr/bin/env python3
"""
winvm.py - Chạy Windows 11 (Ghost Spectre Superlite 24H2) bằng QEMU trên GitHub Codespaces.
Tailscale (RDP) + noVNC xem màn hình cài Windows, chỉ qua cổng riêng tư (không công khai).

Cách dùng (trong terminal Codespaces):
    python3 winvm.py                           # lần đầu: tải ISO (link có sẵn) + cài tự động + mở máy
    python3 winvm.py --iso-url "LINK_KHAC"     # hoặc dùng link/đường dẫn ISO khác
    python3 winvm.py                           # các lần sau: chỉ mở máy
    python3 winvm.py stop                      # tắt máy ảo
    python3 winvm.py reinstall --iso-url "..." # xoá ổ ảo, cài lại từ đầu

Tài khoản Windows: Administrator / admin
"""
import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

# ----------------------------------------------------------------- cấu hình
USER = "Administrator"      # tên "Administrators" trùng tên nhóm nên Windows không cho tạo
PASSWORD = "admin"

# Mục [2] "Chọn ISO có sẵn": (tên hiện trong menu, link Google Drive chia sẻ 'Anyone with the link')
# Mỗi mục: (tên, link) hoặc (tên, link, "windows"|"linux"|"any"); thiếu loại => "windows".
CATALOG = [
    ("Ghost-Spectre-Windows-11-Superlite-25h2",
     "https://drive.google.com/file/d/1GTMvRB3yLD9hYSUC89E_awUDdu70-iZp/view?usp=drivesdk", "windows"),
]

WIN_DIR = os.environ.get("WINVM_DIR") or (
    "/workspaces/winvm" if os.path.isdir("/workspaces") else os.path.expanduser("~/winvm"))
LINUX_DIR = os.environ.get("LINUXVM_DIR") or (
    "/workspaces/linuxvm" if os.path.isdir("/workspaces") else os.path.expanduser("~/linuxvm"))
_HERE = os.path.dirname(os.path.realpath(__file__))
if os.path.realpath(WIN_DIR) == _HERE:
    WIN_DIR += "-data"     # repo trùng tên thư mục dữ liệu => tách ra để ổ ảo/ISO không nằm trong repo
if os.path.realpath(LINUX_DIR) == _HERE:
    LINUX_DIR += "-data"
HOSTNAMES = {"windows": "github-winvm", "linux": "github-linuxvm"}   # tên máy hiện trên Tailscale

CUR_OS = "windows"
DATA = ISO = DISK = UNATT_DIR = UNATT_ISO = MARK = ""


def configure(os_type):
    """Mỗi hệ điều hành một thư mục riêng: Windows -> .../winvm (win11.*), Linux -> .../linuxvm (linux.*)."""
    global CUR_OS, DATA, ISO, DISK, UNATT_DIR, UNATT_ISO, MARK
    CUR_OS = os_type
    DATA = LINUX_DIR if os_type == "linux" else WIN_DIR
    name = "linux" if os_type == "linux" else "win11"
    ISO, DISK = f"{DATA}/{name}.iso", f"{DATA}/{name}.qcow2"
    UNATT_DIR, UNATT_ISO = f"{DATA}/unattend", f"{DATA}/unattend.iso"
    MARK = f"{DATA}/.installed"      # đánh dấu: hệ điều hành đã cài xong và dịch vụ từ xa đã lên


configure("windows")
STATE = os.path.expanduser("~/.config/winvm/tailscaled.state")   # khoá nút Tailscale: NẰM NGOÀI repo
OLD_STATE = f"{WIN_DIR}/.tailscale/tailscaled.state"

TS_SOCK = "/tmp/tailscaled.sock"
LOG_TSD, LOG_TSUP = "/tmp/tailscaled.log", "/tmp/tailscale_up.log"
LOG_QEMU, LOG_WS = "/tmp/qemu.log", "/tmp/websockify.log"

MIN_ISO = 1_000_000_000          # ISO nhỏ hơn 1GB => tải hỏng
INSTALLED_MIN = 5_000_000_000    # ổ ảo lớn hơn 5GB => có thể đã cài Windows

SUDO = "" if os.geteuid() == 0 else "sudo "
CS_NAME = os.environ.get("CODESPACE_NAME", "")   # có giá trị khi chạy trong Codespaces
CS_DOMAIN = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")


# ----------------------------------------------------------------- tiện ích
def run(cmd, check=True, **kw):
    if isinstance(cmd, str):
        kw["shell"] = True
    return subprocess.run(cmd, check=check, **kw)


def out(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def running(pattern):
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def port_open(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


try:
    _env_catalog = json.loads(os.environ.get("WINVM_CATALOG", "") or "null")
    if _env_catalog:
        CATALOG = [(str(e[0]), str(e[1]), str(e[2]) if len(e) > 2 else "windows") for e in _env_catalog]
except (ValueError, TypeError):
    pass


def log(msg):
    print(msg, flush=True)


def disk_used(path):
    """Dung lượng THẬT đang chiếm trên ổ (file qcow2 thưa có kích thước ảo 256GB nhưng dữ liệu thật nhỏ hơn nhiều)."""
    return os.stat(path).st_blocks * 512


# ----------------------------------------------------------------- cài gói
def install_deps(need_web=True):
    cmd2pkg = {
        "qemu-system-x86_64": "qemu-system-x86",
        "qemu-img": "qemu-utils",
        "aria2c": "aria2",
        "genisoimage": "genisoimage",
        "curl": "curl",
    }
    if need_web:
        cmd2pkg["websockify"] = "websockify"
    pkgs = [p for c, p in cmd2pkg.items() if not shutil.which(c)]
    if need_web and not os.path.isdir("/usr/share/novnc"):
        pkgs.append("novnc")
    if pkgs:
        log(f"[+] Cài gói: {' '.join(pkgs)}")
        run(f"{SUDO}env DEBIAN_FRONTEND=noninteractive apt-get update -qq")
        run(f"{SUDO}env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
            + " ".join(pkgs))
    if not shutil.which("tailscale"):
        log("[+] Cài Tailscale...")
        run("curl -fsSL https://tailscale.com/install.sh | sh", check=False)


def kvm_ok():
    if not os.path.exists("/dev/kvm"):
        return False
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        run(f"{SUDO}chmod 666 /dev/kvm", check=False)
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


# ----------------------------------------------------------------- ISO + ổ ảo
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.mediafire.com/",
}


def fetch_html(url):
    """Thử urllib trước, bị chặn (403...) thì thử curl. Trả về '' nếu cả hai đều thất bại."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=BROWSER_HEADERS)
        return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    except Exception as e:  # HTTPError 403, URLError, timeout...
        log(f"[!] urllib lỗi: {e}. Thử lại bằng curl...")
    cmd = ["curl", "-sSL", "--max-time", "40", "--compressed"]
    for k, v in BROWSER_HEADERS.items():
        cmd += ["-H", f"{k}: {v}"]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def resolve_mediafire(url):
    """Trang Mediafire không phải link tải trực tiếp => đọc trang, lấy link download thật."""
    import base64
    html = fetch_html(url)
    if not html:
        return ""
    m = re.search(r'data-scrambled-url="([^"]+)"', html)
    if m:  # bản trang mới: link bị mã hoá base64
        try:
            return base64.b64decode(m.group(1)).decode()
        except Exception:
            pass
    m = re.search(r'href="(https?://download[^"]+mediafire\.com[^"]+)"', html)
    if m:
        return m.group(1).replace("&amp;", "&")
    return ""


def aria_download(direct):
    run(["aria2c", "-x16", "-s16", "-k1M", "-c", "--file-allocation=none",
         f"--user-agent={UA}", "--referer=https://www.mediafire.com/",
         "--console-log-level=warn", "--summary-interval=10",
         "-d", DATA, "-o", os.path.basename(ISO), direct])


def get_iso(url):
    if os.path.exists(ISO) and os.path.getsize(ISO) >= MIN_ISO:
        log(f"[OK] Đã có ISO ({os.path.getsize(ISO) / 1e9:.2f} GB)")
        return
    if os.path.exists(ISO):
        os.remove(ISO)  # file hỏng từ lần trước
    if not url:
        sys.exit("[!] Chưa có ISO. Chạy lại với: python3 winvm.py --iso-url \"LINK_ISO\"\n"
                 "    (hoặc đặt biến môi trường ISO_URL, hoặc đưa đường dẫn file ISO có sẵn)")
    os.makedirs(DATA, exist_ok=True)
    if url.startswith("/") and os.path.exists(url):
        os.symlink(url, ISO)
    elif re.match(r"https?://download\d*\.mediafire\.com/", url):
        log("[+] Link Mediafire trực tiếp => tải bằng aria2...")
        aria_download(url)
    elif "mediafire.com" in url:
        log("[+] Lấy link tải thật từ Mediafire...")
        direct = resolve_mediafire(url)
        if not direct:
            log("\n[!] Mediafire chặn máy chủ Codespaces (403) nên script không tự lấy được link.\n"
                "    Làm trên ĐIỆN THOẠI: mở link Mediafire, bấm giữ nút Download >\n"
                "    'Sao chép địa chỉ liên kết' (link dạng https://download1234.mediafire.com/...).\n"
                "    Hoặc dán link Google Drive (chia sẻ 'Anyone with the link') cũng được.")
            if not sys.stdin.isatty():
                sys.exit("    Rồi chạy lại: python3 winvm.py --iso-url \"LINK\"")
            direct = input("\n>>> Dán link vào đây rồi Enter: ").strip().strip('"')
            if direct.lower() in ("", "exit", "quit", "q"):
                sys.exit("[!] Thoát.")
            if not (direct.startswith("http") or direct.startswith("/")):
                sys.exit("[!] Đó không phải link hay đường dẫn file. Chạy lại và dán link đầy đủ (bắt đầu bằng https://).")
            return get_iso(direct)   # xử lý link vừa dán (trực tiếp / Drive / đường dẫn file)
        aria_download(direct)
    elif "drive.google.com" in url or "docs.google.com" in url:
        log("[+] Tải từ Google Drive (gdown)...")
        run([sys.executable, "-m", "pip", "install", "-q", "-U", "gdown"])
        m = re.search(r"/d/([\w-]+)|[?&]id=([\w-]+)", url)
        fid = (m.group(1) or m.group(2)) if m else None
        target = f"https://drive.google.com/uc?id={fid}" if fid else url
        run([sys.executable, "-m", "gdown", target, "-O", ISO])
    else:
        log("[+] Tải ISO bằng aria2 (16 luồng)...")
        aria_download(url)
    size = os.path.getsize(ISO)
    log(f"Dung lượng ISO: {size / 1e9:.2f} GB")
    if size < MIN_ISO:
        os.remove(ISO)
        sys.exit("[!] ISO quá nhỏ => link hỏng/không phải link tải trực tiếp.")


AUTOUNATTEND = r"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter><Active>true</Active></ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallTo><DiskID>0</DiskID><PartitionID>1</PartitionID></InstallTo>
        </OSImage>
      </ImageInstall>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg add HKLM\SYSTEM\Setup\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>2</Order><Path>reg add HKLM\SYSTEM\Setup\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>3</Order><Path>reg add HKLM\SYSTEM\Setup\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>4</Order><Path>reg add HKLM\SYSTEM\Setup\LabConfig /v BypassCPUCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
      </RunSynchronous>
      <UserData>
        <AcceptEula>true</AcceptEula>
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>net user administrator /active:yes</Path></RunSynchronousCommand>
      </RunSynchronous>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <ComputerName>WINVM</ComputerName>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
      <UserAccounts>
        <AdministratorPassword><Value>@PASSWORD@</Value><PlainText>true</PlainText></AdministratorPassword>
      </UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled>
        <Username>@USER@</Username>
        <Password><Value>@PASSWORD@</Value><PlainText>true</PlainText></Password>
        <LogonCount>999999</LogonCount>
      </AutoLogon>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add"><Order>1</Order><CommandLine>reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" /v fDenyTSConnections /t REG_DWORD /d 0 /f</CommandLine><Description>Bat RDP</Description></SynchronousCommand>
        <SynchronousCommand wcm:action="add"><Order>2</Order><CommandLine>netsh advfirewall firewall set rule group="remote desktop" new enable=Yes</CommandLine><Description>Mo firewall RDP</Description></SynchronousCommand>
        <SynchronousCommand wcm:action="add"><Order>3</Order><CommandLine>powercfg /h off</CommandLine><Description>Tat hibernate</Description></SynchronousCommand>
        <SynchronousCommand wcm:action="add"><Order>4</Order><CommandLine>powercfg /change standby-timeout-ac 0</CommandLine><Description>Khong sleep</Description></SynchronousCommand>
        <SynchronousCommand wcm:action="add"><Order>5</Order><CommandLine>powercfg /change monitor-timeout-ac 0</CommandLine><Description>Khong tat man hinh</Description></SynchronousCommand>
        <SynchronousCommand wcm:action="add"><Order>6</Order><CommandLine>reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects" /v VisualFXSetting /t REG_DWORD /d 2 /f</CommandLine><Description>Hieu ung toi uu hieu nang</Description></SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
"""


def build_unattend_iso():
    """Windows Setup tự tìm autounattend.xml ở gốc mọi ổ (kể cả CD) => cài hoàn toàn tự động."""
    os.makedirs(UNATT_DIR, exist_ok=True)
    xml = AUTOUNATTEND.replace("@USER@", USER).replace("@PASSWORD@", PASSWORD)
    with open(f"{UNATT_DIR}/autounattend.xml", "w", encoding="utf-8") as f:
        f.write(xml)
    run(["genisoimage", "-quiet", "-o", UNATT_ISO, "-J", "-r", "-V", "UNATTEND", UNATT_DIR])


def make_disk(size):
    if not os.path.exists(DISK):
        os.makedirs(DATA, exist_ok=True)
        # preallocation=metadata: ghi nhanh hơn, vẫn là file thưa (không ăn hết dung lượng)
        run(["qemu-img", "create", "-f", "qcow2", "-o", "preallocation=metadata", DISK, size])


# ----------------------------------------------------------------- Tailscale
def ts_cli(*args):
    return ["tailscale", f"--socket={TS_SOCK}", *args]


def ts_ip():
    lines = out(ts_cli("ip", "-4")).splitlines()
    return lines[0] if lines else ""


def tailscale(authkey=""):
    if not shutil.which("tailscale"):
        log("[!] Không cài được Tailscale, bỏ qua.")
        return ""
    if not running("tailscaled.*--tun=userspace"):
        log("[+] Khởi động tailscaled...")
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        if os.path.exists(OLD_STATE) and not os.path.exists(STATE):
            shutil.move(OLD_STATE, STATE)   # chuyển trạng thái Tailscale cũ ra khỏi thư mục dữ liệu
        subprocess.Popen(
            ["tailscaled", "--tun=userspace-networking", f"--state={STATE}", f"--socket={TS_SOCK}"],
            stdout=open(LOG_TSD, "w"), stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(4)
    ip = ts_ip()
    if ip:
        return ip
    log("[+] Đăng nhập Tailscale...")
    cmd = ts_cli("up", f"--hostname={HOSTNAMES[CUR_OS]}", "--accept-dns=false")
    if authkey:
        cmd.append(f"--auth-key={authkey}")
    open(LOG_TSUP, "w").close()
    subprocess.Popen(cmd, stdout=open(LOG_TSUP, "w"), stderr=subprocess.STDOUT)
    shown = False
    for _ in range(100):  # chờ tối đa ~5 phút
        time.sleep(3)
        if not shown:
            m = re.search(r"https://login\.tailscale\.com/a/\w+", open(LOG_TSUP).read())
            if m:
                log(f"\n[!] Mở link này để đăng nhập Tailscale:\n    {m.group(0)}\n")
                shown = True
        ip = ts_ip()
        if ip:
            return ip
    log(f"[!] Hết thời gian chờ Tailscale. Xem log: {LOG_TSD} / {LOG_TSUP}")
    return ""


# ----------------------------------------------------------------- QEMU
def mem_gb(want):
    info = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        info[k] = int(v.split()[0]) // 1024  # MB
    total, avail = info["MemTotal"], info.get("MemAvailable", info["MemTotal"])
    # Windows dùng hết RAM được cấp. Hệ thống tắt tiến trình lớn nhất (signal 15) khi RAM trống < ~10%,
    # nên phải chừa: 10% tổng RAM + ~1GB dự phòng.
    cap = min(total // 2, avail - total // 10 - 1024)
    if want * 1024 <= cap:
        return want
    gb = max(2, cap // 1024)
    log(f"[!] Máy chủ có {total // 1024}GB RAM, còn trống ~{avail // 1024}GB (VS Code chiếm phần lớn). "
        f"Để không bị hệ thống tắt (signal 15) => hạ RAM Windows từ {want}GB xuống {gb}GB.")
    if gb <= 2:
        log("    RAM quá ít, Windows có thể bị hệ thống giết (signal 15). "
            "Nên đổi Codespace lên 4-core (16GB RAM), hoặc đóng bớt tab/Copilot chat.")
    return gb


def read_os():
    return CUR_OS


def switch_os(t, move_iso=False):
    """Đổi sang hệ điều hành t. move_iso: chuyển ISO vừa tải sang thư mục của hệ điều hành đó."""
    if t == CUR_OS:
        return
    old_iso = ISO
    configure(t)
    os.makedirs(DATA, exist_ok=True)
    if move_iso and os.path.exists(old_iso):
        shutil.move(old_iso, ISO)


def os_present(os_type):
    d, name = (LINUX_DIR, "linux") if os_type == "linux" else (WIN_DIR, "win11")
    return os.path.exists(f"{d}/.installed") or os.path.exists(f"{d}/{name}.qcow2")


def choose_os(a):
    """Chọn hệ điều hành để chạy: --os, máy ảo đang chạy, hoặc ổ nào đã có (có cả hai thì hỏi)."""
    if a.os in ("windows", "linux"):
        return configure(a.os)
    if running(f"qemu-system-x86_64.*{re.escape(LINUX_DIR)}"):
        return configure("linux")
    if running(f"qemu-system-x86_64.*{re.escape(WIN_DIR)}"):
        return configure("windows")
    w, l = os_present("windows"), os_present("linux")
    if w and l and sys.stdin.isatty():
        configure("linux" if ask("[?] Có cả Windows và Linux. Chạy cái nào? (1 = Windows, 2 = Linux): ") == "2"
                  else "windows")
    elif l and not w:
        configure("linux")
    else:
        configure("windows")


WIN_MARKS = ("BOOTMGR", "SETUP.EXE")
LINUX_MARKS = ("ISOLINUX", "SYSLINUX", "CASPER", "LIVEOS", "GRUB", "DISTS", "POOL",
               "VMLINUZ", "INITRD", "INSTALL.AMD", ".DISK")


def detect_iso_type(path):
    """Đoán ISO là Windows hay Linux bằng danh sách file trong ISO (không giải nén). Không chắc => None."""
    if not shutil.which("isoinfo"):
        return None
    listing = ""
    for extra in ([], ["-J"]):
        r = subprocess.run(["isoinfo", "-i", path, "-l", *extra], capture_output=True, text=True)
        listing += r.stdout.upper()
    if any(k in listing for k in WIN_MARKS) or re.search(r"\bSOURCES\b", listing):
        return "windows"
    if any(k in listing for k in LINUX_MARKS):
        return "linux"
    # Windows 10/11 dùng cầu nối UDF: phần ISO9660 chỉ còn README.TXT
    if "README.TXT" in listing:
        return "windows"
    return None


def resolve_os(flag="auto"):
    """Quyết định ISO là Windows hay Linux (tự nhận diện, không chắc thì hỏi) rồi lưu lại."""
    if flag in ("windows", "linux"):
        t = flag
        d = detect_iso_type(ISO)
        if d and d != t:
            log(f"[!] Cấu hình đang ép {t.upper()} nhưng ISO có vẻ là {d.upper()}. "
                f"Nếu sai, chạy lại với: python3 winvm.py --os {d}")
    else:
        t = detect_iso_type(ISO)
        if t:
            log(f"[+] Nhận diện ISO: {t.upper()}")
        elif sys.stdin.isatty():
            log("[?] Không đoán được ISO là Windows hay Linux.")
            t = "linux" if ask("    Nhập 1 = Windows, 2 = Linux: ") == "2" else "windows"
        else:
            log("[!] Không đoán được loại ISO, coi là Windows (đổi bằng --os linux).")
            t = "windows"
    switch_os(t, move_iso=True)
    return t


def ssh_alive():
    """Linux đã cài + chạy sshd? (cổng 2222 trên máy chủ chuyển vào cổng 22 của máy ảo)"""
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", 2222))
        return s.recv(4).startswith(b"SSH-")
    except OSError:
        return False
    finally:
        s.close()


def rdp_alive():
    """Gửi gói X.224 Connection Request tới 127.0.0.1:3389; Windows trả lời => RDP đã sẵn sàng."""
    req = bytes([0x03, 0, 0, 0x13, 0x0E, 0xE0, 0, 0, 0, 0, 0, 0x01, 0, 0x08, 0, 0x03, 0, 0, 0])
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", 3389))
        s.sendall(req)
        data = s.recv(16)
        return len(data) > 0 and data[0] == 3
    except OSError:
        return False
    finally:
        s.close()


def tail(path, n=25):
    try:
        return "".join(open(path, errors="replace").readlines()[-n:]).strip()
    except OSError:
        return ""


def mem_sample():
    """Trả về (RAM còn trống MB, RAM QEMU đang dùng MB)."""
    avail = 0
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable"):
            avail = int(line.split()[1]) // 1024
    rss = 0
    pids = out(["pgrep", "-f", f"qemu-system-x86_64.*{re.escape(DISK)}"]).split()
    for pid in pids[:1]:
        try:
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS"):
                    rss = int(line.split()[1]) // 1024
        except OSError:
            pass
    return avail, rss


def qemu_running():
    return running(f"qemu-system-x86_64.*{re.escape(DISK)}")


def start_qemu(mode, kvm, ram, cpus_opt, force=False, os_type="windows"):
    cpus = cpus_opt or os.cpu_count() or 2   # 0 = dùng TẤT CẢ nhân CPU
    mem = ram if force else mem_gb(ram)
    name = "Linux" if os_type == "linux" else "Windows"
    log(f"[+] Mở {name} | {'CÀI TỪ ISO' if mode == 'iso' else 'BOOT Ổ ẢO'} | "
        f"KVM: {'CÓ' if kvm else 'KHÔNG (rất chậm!)'} | {cpus} CPU, {mem}GB RAM")

    if kvm:
        cpu = "host" if os_type == "linux" else "host,hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,hv_time"  # Hyper-V chỉ cho Windows
        accel = ["-accel", "kvm", "-cpu", cpu]
    else:
        accel = ["-accel", "tcg,thread=multi", "-cpu", "max"]

    # Lúc cài dùng cache=unsafe cho nhanh; chạy bình thường dùng writeback cho an toàn hơn
    cache = "unsafe" if mode == "iso" else "writeback"
    cmd = ["qemu-system-x86_64", "-machine", "q35", *accel,
           "-smp", f"{cpus},sockets=1,cores={cpus}", "-m", f"{mem}G",
           "-drive", f"file={DISK},if=none,id=hd0,format=qcow2,cache={cache},aio=threads,discard=unmap",
           "-device", "ide-hd,drive=hd0,bus=ide.0,bootindex=0,rotation_rate=1"]  # rotation_rate=1 => Windows nhận là SSD
    if mode == "iso":
        cmd += ["-drive", f"file={ISO},if=none,id=cd0,media=cdrom,readonly=on",
                "-device", "ide-cd,drive=cd0,bus=ide.1,bootindex=1"]
        if os_type == "windows":   # đĩa trả lời tự động chỉ dành cho Windows Setup
            cmd += ["-drive", f"file={UNATT_ISO},if=none,id=cd1,media=cdrom,readonly=on",
                    "-device", "ide-cd,drive=cd1,bus=ide.2"]
    cmd += ["-device", "e1000e,netdev=n0",
            "-netdev", "user,id=n0,hostfwd=tcp::3389-:3389,hostfwd=tcp::2222-:22",
            "-rtc", "base=utc" if os_type == "linux" else "base=localtime",
            "-vga", "std", "-device", "qemu-xhci", "-device", "usb-tablet",
            "-display", "none", "-vnc", "127.0.0.1:0"]

    proc = subprocess.Popen(cmd, stdout=open(LOG_QEMU, "w"), stderr=subprocess.STDOUT,
                            start_new_session=True)
    time.sleep(5)
    if proc.poll() is not None:
        log("[!] QEMU chết ngay, log:")
        log(open(LOG_QEMU).read())
        sys.exit(1)
    log(f"[OK] {name} đang khởi động, PID {proc.pid}")


def wait_vnc(timeout=60):
    """Chờ cổng màn hình (5900) của máy ảo mở. QEMU chết thì in log lỗi ra luôn."""
    for _ in range(timeout):
        if port_open(5900):
            return True
        if not qemu_running():
            break
        time.sleep(1)
    log("[!] Máy ảo không mở cổng màn hình (5900) => web sẽ báo 'Failed to connect'. Log QEMU:")
    log(tail(LOG_QEMU, 30) or "(log trống: QEMU có thể bị hệ thống giết vì hết RAM)")
    sys.exit(1)


def status():
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":")[0], line.split(":")[1].split()[0]
        mem[k] = int(v) // 1024
    log(f"QEMU đang chạy : {qemu_running()}")
    log(f"Cổng VNC 5900  : {'MỞ' if port_open(5900) else 'ĐÓNG (web sẽ báo Failed to connect)'}")
    log(f"Web noVNC 6080 : {'MỞ' if port_open(6080) else 'ĐÓNG'}")
    log(f"RDP trả lời    : {rdp_alive()}")
    log(f"/dev/kvm       : {'có' if os.path.exists('/dev/kvm') else 'KHÔNG CÓ'}")
    log(f"RAM máy chủ    : tổng {mem.get('MemTotal', 0)} MB, còn trống {mem.get('MemAvailable', 0)} MB")
    log(f"Đã đánh dấu cài xong: {os.path.exists(MARK)}")
    for f in ("memory.max", "memory.current", "memory.events"):
        try:
            log(f"cgroup {f}: " + open(f"/sys/fs/cgroup/{f}").read().replace("\n", " | ").strip())
        except OSError:
            pass
    log(f"earlyoom/oom killer: {out(['pgrep', '-a', 'earlyoom']) or 'không thấy tiến trình earlyoom'}")
    log("--- tiến trình QEMU ---")
    log(out(["pgrep", "-a", "qemu"]) or "(không có tiến trình qemu nào)")
    log("--- 25 dòng cuối log QEMU ---")
    log(tail(LOG_QEMU, 25) or "(trống)")
    log("--- 10 dòng cuối log websockify ---")
    log(tail(LOG_WS, 10) or "(trống)")


# ----------------------------------------------------------------- Web (noVNC, chỉ riêng tư)
def prepare_webroot(src="/usr/share/novnc", root="/tmp/winvm-web"):
    """noVNC + index.html tự chuyển tới màn hình máy ảo (khỏi thấy trang 'Directory listing')."""
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(src, root)
    with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8">'
                '<meta http-equiv="refresh" content="0;url=vnc.html?autoconnect=true&resize=scale">'
                '<title>Màn hình máy ảo</title>')
    return root


def web():
    """noVNC chỉ nghe ở 127.0.0.1. Trong Codespaces xem qua cổng 6080 chế độ RIÊNG TƯ
    (phải đăng nhập GitHub). Script này không bao giờ mở công khai, không dùng tunnel."""
    if not port_open(6080):
        log("[+] Khởi động noVNC (127.0.0.1:6080 -> 5900)...")
        subprocess.Popen(["websockify", f"--web={prepare_webroot()}", "127.0.0.1:6080", "127.0.0.1:5900"],
                         stdout=open(LOG_WS, "w"), stderr=subprocess.STDOUT,
                         start_new_session=True)
        time.sleep(2)
    if CS_NAME:
        return f"https://{CS_NAME}-6080.{CS_DOMAIN}/vnc.html?autoconnect=true&resize=scale"
    return "http://localhost:6080/vnc.html?autoconnect=true&resize=scale"


# ----------------------------------------------------------------- main
def stop_all():
    for pat in ("qemu-system-x86_64", "websockify.*6080"):   # tắt máy ảo của CẢ hai hệ điều hành
        subprocess.run(["pkill", "-f", pat])
    log("[OK] Đã tắt máy ảo + web. (Tailscale vẫn chạy)")


# ----------------------------------------------------------------- menu chọn nguồn cài Windows
def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        sys.exit("[!] Không nhập được từ bàn phím. Chạy kèm link: python3 winvm.py --iso-url \"LINK\"")


def iso_ready():
    return os.path.exists(ISO) and os.path.getsize(ISO) >= MIN_ISO


def load_module(name, missing_msg=None):
    """Nạp file .py cạnh winvm.py. Không có thì trả None (kèm thông báo nếu có missing_msg)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        return __import__(name)
    except ImportError:
        if missing_msg:
            log(missing_msg)
        return None


def mask_email(email):
    user, _, dom = email.partition("@")
    return (user[:1] + "***@" + dom) if dom else "(không rõ)"


def menu_own_iso(gd):
    """[1] ISO của bạn: đăng nhập Google, liệt kê file .iso trong Drive CỦA BẠN, chọn một để tải về."""
    try:
        gd.ensure_login()
    except SystemExit as e:
        log(str(e))
        return False
    email = gd.google_email()
    log("[+] Đang tìm file .iso trong Google Drive của bạn...")
    items = gd.list_user_isos()
    while True:
        log(f"\n[+] File ISO trong Google Drive của bạn ({mask_email(email)})")
        log("[0] Quay lại")
        for i, it in enumerate(items, 1):
            log(f"[{i}] {it['Path']} ({gd.human(it['Size'])})")
        if not items:
            log("    Không thấy file .iso (từ 1GB) nào trong 'Drive của tôi'. "
                "Hãy up ISO lên Drive của bạn rồi chọn lại.")
        c = ask("Nhập lựa chọn theo số: ")
        if c == "0":
            return False
        if c.isdigit() and 1 <= int(c) <= len(items):
            it = items[int(c) - 1]
            need = it["Size"] + 512 * 1024 * 1024
            if gd.free_bytes(DATA) < need:
                log(f"[!] Không đủ chỗ: cần ~{gd.human(need)}, còn {gd.human(gd.free_bytes(DATA))}.")
                continue
            if os.path.exists(ISO):
                os.remove(ISO)
            log(f"[+] Tải {it['Path']} từ Drive của bạn...")
            if gd.download_from_drive(it["Path"], ISO, it["Size"]):
                return True
            log("[!] Tải lỗi, thử lại.")
            continue
        log("    Nhập số có trong danh sách.")


def catalog_for(os_flag):
    """Chỉ hiện ISO hợp với cấu hình đang chọn (Windows/Linux); chưa ép loại nào thì hiện tất cả."""
    if os_flag in ("windows", "linux"):
        return [e for e in CATALOG if e[2] in (os_flag, "any")]
    return list(CATALOG)


def menu_catalog(os_flag="auto"):
    """[2] ISO có sẵn: trả về link được chọn (hoặc link bạn nhập), hoặc None nếu quay lại."""
    items = catalog_for(os_flag)
    while True:
        log("\n[+] Chọn file ISO có sẵn để cài đặt")
        log("[0] Quay lại")
        for i, e in enumerate(items, 1):
            log(f"[{i}] {e[0]}")
        other = len(items) + 1
        log(f"[{other}] Nhập link tải ISO khác (Linux hoặc Windows)")
        c = ask("Nhập lựa chọn theo số: ")
        if c == "0":
            return None
        if c.isdigit() and 1 <= int(c) <= len(items):
            return items[int(c) - 1][1]
        if c == str(other):
            url = ask("Dán link tải ISO (bắt đầu bằng https://), Enter để quay lại: ")
            if url.startswith("http"):
                return url
            if url:
                log("    Link phải bắt đầu bằng http hoặc https.")
            continue
        log("    Nhập số có trong danh sách.")


def menu_admin_disk(sl):
    """[3] Ổ ảo admin: CHỈ tài khoản Google admin mới tải được. Tài khoản lạ bị chặn."""
    try:
        sl.ensure_login()
    except SystemExit as e:
        log(str(e))
        return False
    if not sl.ADMIN_EMAIL_SHA256.strip():
        log("[!] Chưa cài tài khoản admin nên mục [3] đang KHÓA với mọi người. Admin: đăng nhập bằng "
            "tài khoản admin rồi chạy `python3 saoluu.py admin-hash` và làm theo hướng dẫn.")
        return False
    email = sl.google_email()
    if not sl.is_admin(email):
        log(f"[!] Tài khoản {mask_email(email)} KHÔNG phải tài khoản admin => bị chặn. "
            "Mục [3] chỉ dành cho admin.")
        return False
    log("[OK] Đúng tài khoản admin.")
    try:
        sl.restore(stop=True, yes=True)
    except SystemExit as e:
        log(str(e))
        return False
    switch_os("windows")
    return True


def pick_source(a):
    """Hỏi nguồn cài. Trả về 'iso' (ISO đã sẵn sàng hoặc a.iso_url đã đặt) hoặc 'disk' (đã đặt ổ ảo admin)."""
    gd = load_module("gdrive", "[!] Thiếu file gdrive.py cạnh winvm.py nên mục [1] chưa dùng được.")
    sl = load_module("saoluu")   # file riêng tư của admin: người dùng thường sẽ KHÔNG có file này
    while True:
        log("\n[+] 1 là ISO bắt buộc phải up lên Google Drive của bạn để chạy Windows")
        log("[+] 3 là ổ ảo của admin, người dùng thường không được dùng")
        log("\n[+] Chọn file có sẵn hay ISO riêng của bạn")
        log("[0] Đóng")
        log("[1] Chọn ISO của bạn")
        log("[2] Chọn ISO có sẵn")
        log("[3] Ổ ảo của admin")
        c = ask("Nhập lựa chọn theo số: ")
        if c == "0":
            sys.exit("Đã đóng.")
        if c == "1":
            if gd and menu_own_iso(gd):
                return "iso"
        elif c == "2":
            url = menu_catalog(a.os)
            if url:
                if os.path.exists(ISO):
                    os.remove(ISO)
                a.iso_url = url
                return "iso"
        elif c == "3":
            if not sl:
                log("[!] Mục [3] chỉ dành cho admin (cần file riêng của admin). Người dùng hãy chọn [1] hoặc [2].")
            elif menu_admin_disk(sl):
                return "disk"
        else:
            log("    Chỉ nhập 0, 1, 2 hoặc 3.")


def main():
    ap = argparse.ArgumentParser(description="Windows 11 Ghost Spectre trên QEMU + Tailscale + noVNC")
    ap.add_argument("cmd", nargs="?", default="all", choices=["all", "stop", "reinstall", "status", "done"])
    ap.add_argument("--iso-url", default=os.environ.get("ISO_URL", ""),
                    help="link tải trực tiếp ISO (Google Drive cũng được) hoặc đường dẫn file ISO")
    ap.add_argument("--disk-size", default="256G", help="dung lượng ổ ảo (file thưa), mặc định 256G")
    ap.add_argument("--ram", type=int, default=13, help="RAM cho Windows (GB), mặc định 13")
    ap.add_argument("--force-ram", action="store_true",
                    help="dùng đúng số --ram, KHÔNG tự hạ (có thể bị hệ thống tắt nếu RAM trống < 10%%)")
    ap.add_argument("--cpus", type=int, default=0, help="số nhân CPU, mặc định 0 = tất cả")
    ap.add_argument("--mode", choices=["auto", "iso", "disk"], default="auto")
    ap.add_argument("--os", choices=["auto", "windows", "linux"],
                    default=os.environ.get("WINVM_OS", "auto").strip().lower(),
                    help="loại ISO (mặc định tự nhận diện; Linux cài thủ công qua màn hình web)")
    ap.add_argument("--authkey", default=os.environ.get("TS_AUTHKEY", ""),
                    help="Tailscale auth key (khỏi bấm link đăng nhập)")
    ap.add_argument("--no-web", action="store_true",
                    help="tắt noVNC (mặc định BẬT, xem màn hình qua cổng riêng tư)")
    a = ap.parse_args()

    if a.cmd == "stop":
        return stop_all()
    choose_os(a)
    if a.cmd == "status":
        return status()
    if a.cmd == "done":
        open(MARK, "w").close()
        return log("[OK] Đã đánh dấu hệ điều hành cài xong. Từ giờ 'python3 winvm.py' vào thẳng ổ, không gắn ISO.")
    if a.cmd == "reinstall":
        stop_all()
        for p in (DISK, MARK):   # xoá luôn dấu 'đã cài xong' để lần sau không boot nhầm ổ trống
            if os.path.exists(p):
                os.remove(p)
        a.mode = "iso"

    os.makedirs(DATA, exist_ok=True)
    install_deps(need_web=not a.no_web)
    kvm = kvm_ok()
    if not kvm:
        log("[!] Không có /dev/kvm => Windows sẽ CHẬM khủng khiếp. "
            "Dùng Codespaces máy 4-core trở lên (KVM có sẵn).")

    mode = a.mode
    if mode == "auto":
        big = os.path.exists(DISK) and disk_used(DISK) > INSTALLED_MIN
        # Đã cài xong (có file đánh dấu) => chỉ boot ổ, không gắn/tải ISO.
        # Ổ lớn mà không còn ISO => cũng boot ổ. Còn lại: gắn ISO nhưng ổ vẫn boot trước.
        mode = "disk" if (os.path.exists(MARK) or (big and not os.path.exists(ISO))) else "iso"

    if mode == "iso" and not a.iso_url and not qemu_running():
        interactive = sys.stdin.isatty()
        use_existing = False
        if iso_ready():
            if CUR_OS == "linux" and os.path.exists(DISK):
                use_existing = True   # đang cài Linux dở: giữ nguyên ISO, không hỏi lại
            elif interactive:
                use_existing = ask(f"[?] Đã có ISO trên Codespace ({os.path.getsize(ISO) / 1e9:.2f} GB). "
                                   "Dùng luôn? (y/n): ").lower() in ("", "y", "yes")
            else:
                use_existing = True
        if not use_existing:
            if not interactive:
                sys.exit("[!] Chưa có ISO. Chạy kèm link: python3 winvm.py --iso-url \"LINK\"")
            if pick_source(a) == "disk":
                mode = "disk"

    if mode == "iso":
        # Lần cài trước bị ngắt giữa chừng => ổ có bảng phân vùng nhưng KHÔNG có Windows,
        # boot sẽ báo "An operating system wasn't found". Ổ nhỏ + chưa đánh dấu => tạo lại ổ trống.
        if (os.path.exists(DISK) and not os.path.exists(MARK) and not qemu_running()
                and read_os() != "linux" and disk_used(DISK) < INSTALLED_MIN):
            log("[+] Ổ ảo chưa có Windows hoàn chỉnh (lần cài trước bị ngắt) => tạo lại ổ trống để cài từ đầu.")
            os.remove(DISK)
        if not qemu_running():
            get_iso(a.iso_url)
            if resolve_os(a.os) == "windows":
                build_unattend_iso()
    os_type = read_os()
    make_disk(a.disk_size)

    ip = tailscale(a.authkey)

    if qemu_running():
        log(f"[OK] Máy ảo {'Linux' if os_type == 'linux' else 'Windows'} đang chạy sẵn.")
    else:
        if running("qemu-system-x86_64"):
            sys.exit("[!] Đang có máy ảo khác chạy (hệ điều hành kia). Chạy 'python3 winvm.py stop' trước.")
        start_qemu(mode, kvm, a.ram, a.cpus, a.force_ram, os_type)
    wait_vnc()

    url = "" if a.no_web else web()

    log("\n" + "=" * 60)
    if url:
        log(f"WEB (màn hình máy ảo):\n    {url}")
        log("    (cổng 6080 phải để RIÊNG TƯ - không mở công khai)")
    if os_type == "linux":
        if ip:
            log(f"SSH qua Tailscale: {ip}:2222 (khi Linux đã cài openssh-server)")
        log("Linux: gợi ý đặt tên máy (hostname) = linuxvm, tài khoản root (mật khẩu bạn tự đặt lúc cài)")
        if mode == "iso":
            log("Cài Linux theo trình cài đặt hiện trên màn hình web. Cài xong, khởi động lại vào Linux rồi chạy:")
            log("    python3 winvm.py done      (để lần sau vào thẳng ổ, không gắn ISO)")
    else:
        if ip:
            log(f"RDP qua Tailscale: {ip}:3389")
        log(f"Windows: tên máy WINVM | tài khoản {USER} / {PASSWORD}")
        if mode == "iso":
            log("Đang cài tự động (~10-20 phút, tự khởi động lại vài lần). "
                "Xong thì lần sau chỉ cần chạy: python3 winvm.py")
            log("Nếu màn hình báo 'An operating system wasn't found' => chạy: python3 winvm.py reinstall")
    log("=" * 60)
    log("Giữ terminal này mở để Codespace không tự tắt (Ctrl+C = thoát, máy ảo vẫn chạy).")
    samples, n = [], 0
    try:
        while qemu_running():
            time.sleep(5)
            n += 1
            av, r = mem_sample()
            samples = (samples + [f"{time.strftime('%H:%M:%S')}  RAM còn trống {av} MB | QEMU dùng {r} MB"])[-10:]
            if n % 6 == 0:
                if not os.path.exists(MARK) and (rdp_alive() if os_type == "windows" else ssh_alive()):
                    open(MARK, "w").close()
                    log("\n[OK] Cài xong + dịch vụ từ xa đã lên. Từ giờ 'python3 winvm.py' sẽ tự vào ổ, không dùng ISO.")
                print(".", end="", flush=True)
    except KeyboardInterrupt:
        return
    log("\n[!] Máy ảo đã DỪNG. RAM trong 10 mẫu cuối (mỗi 5 giây):")
    log("\n".join(samples) or "(chưa có mẫu)")
    log("25 dòng cuối log QEMU:")
    t = tail(LOG_QEMU, 25)
    log(t or "(log trống)")
    if "signal 15" in t or not t:
        log("=> QEMU bị HỆ THỐNG tắt (signal 15), gần như chắc chắn do Codespace thiếu RAM. "
            "Đổi Codespace lên 4-core/16GB RAM rồi chạy lại.")


if __name__ == "__main__":
    main()
