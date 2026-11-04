#!/usr/bin/env python3
"""
gdrive.py - phần dùng chung (CÔNG KHAI): cài rclone, đăng nhập Google Drive bằng trình duyệt,
liệt kê / tải file .iso trong Drive CỦA CHÍNH NGƯỜI DÙNG. winvm.py dùng file này cho mục [1].
Phần sao lưu ổ ảo + ổ ảo admin nằm ở saoluu.py (riêng tư, chỉ admin giữ).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile


DATA = os.environ.get("WINVM_DIR") or (
    "/workspaces/winvm" if os.path.isdir("/workspaces") else os.path.expanduser("~/winvm"))
if os.path.realpath(DATA) == os.path.dirname(os.path.realpath(__file__)):
    DATA += "-data"   # repo tên "winvm" trùng thư mục dữ liệu => tách ra để file ổ ảo/ISO không nằm trong repo
BIN = f"{DATA}/bin"
RCLONE = f"{BIN}/rclone"
CONF = os.path.expanduser("~/.config/winvm/rclone.conf")   # token Google Drive: NẰM NGOÀI repo, tuyệt đối không commit
OLD_CONF = f"{DATA}/rclone.conf"
REMOTE = "gdrive"
AUTH_PORT = 53682
LINKFILE = f"{DATA}/google_link.txt"   # link đăng nhập Google lưu thành 1 dòng để dễ copy
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def log(msg):
    print(msg, flush=True)


def rc(*args):
    return [RCLONE, "--config", CONF, *args]


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.2f} {u}"
        n /= 1024


def free_bytes(d):
    try:
        return shutil.disk_usage(d).free
    except OSError:
        return 0


def install_rclone():
    if os.path.exists(RCLONE):
        return
    os.makedirs(BIN, exist_ok=True)
    zpath = f"{BIN}/rclone.zip"
    log("[+] Tải rclone (một lần duy nhất)...")
    subprocess.run(["curl", "-fsSL", "-o", zpath,
                    "https://downloads.rclone.org/rclone-current-linux-amd64.zip"], check=True)
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            if n.endswith("/rclone"):
                with z.open(n) as src, open(RCLONE, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                break
    os.chmod(RCLONE, 0o755)
    os.remove(zpath)


def logged_in():
    if not os.path.exists(CONF):
        return False
    r = subprocess.run(rc("lsd", f"{REMOTE}:", "--max-depth", "1"),
                       capture_output=True, timeout=120)
    return r.returncode == 0


def google_url(local_link):
    """Link rclone in ra (127.0.0.1) chỉ mở được trong Codespace; lấy link Google mà nó chuyển hướng tới."""
    for _ in range(10):
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{redirect_url}", local_link],
                           capture_output=True, text=True)
        if r.stdout.strip().startswith("http"):
            return r.stdout.strip()
        time.sleep(1)
    return ""


def login():
    log("\n=== BƯỚC 1: ĐĂNG NHẬP GOOGLE ===")
    p = subprocess.Popen([RCLONE, "authorize", "drive", "--auth-no-open-browser"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    token, asked = None, False
    for line in p.stdout:
        m = re.search(rf"http://127\.0\.0\.1:{AUTH_PORT}/auth\?state=[\w-]+", line)
        if m and not asked:
            asked = True
            g = google_url(m.group(0))
            if not g:
                p.terminate()
                sys.exit("[!] Không lấy được link đăng nhập Google. Chạy lại lệnh thử xem.")
            with open(LINKFILE, "w") as f:
                f.write(g + "\n")
            log("\n(a) Mở link Google bên dưới TRÊN ĐIỆN THOẠI, đăng nhập rồi bấm Cho phép/Allow.")
            log("    Cách 1 (nên dùng): BẤM THẲNG vào link trong terminal rồi chọn Open/Mở. "
                "Đừng copy tay, link rất dài, copy thiếu một đoạn là Google báo lỗi 400.\n")
            log(g)
            log(f"\n    Cách 2: mở file {LINKFILE} trong khung Explorer, Chọn tất cả (Ctrl+A), "
                "Sao chép rồi dán vào thanh địa chỉ trình duyệt.")
            if "client_id=" not in g or "redirect_uri=" not in g or "state=" not in g:
                log("    [!] Link trông thiếu tham số, nếu Google báo lỗi 400 hãy Ctrl+C và chạy lại.")
            log("\n(b) Sau khi cho phép, trình duyệt sẽ báo LỖI 'không thể truy cập trang' "
                "(địa chỉ 127.0.0.1...). Bình thường, đừng đóng.\n"
                "    Copy TOÀN BỘ địa chỉ trên thanh địa chỉ (bắt đầu bằng http://127.0.0.1:53682/?state=...)\n")
            while True:
                pasted = input(">>> Dán địa chỉ đó vào đây rồi Enter: ").strip().strip('"')
                if f"127.0.0.1:{AUTH_PORT}" in pasted and "code=" in pasted:
                    break
                print("    Chưa đúng: địa chỉ phải chứa 127.0.0.1:53682 và code=... Thử copy lại.")
            subprocess.run(["curl", "-s", "-o", "/dev/null", pasted])
        m2 = re.search(r'(\{"access_token".*\})', line)
        if m2:
            token = m2.group(1)
            break
    p.terminate()
    if not token:
        sys.exit("[!] Không nhận được token từ Google. Chạy lại và làm lại từ (a).")
    json.loads(token)  # kiểm tra token hợp lệ
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF, "w") as f:
        f.write(f"[{REMOTE}]\ntype = drive\nscope = drive\ntoken = {token}\n")
    os.chmod(CONF, 0o600)
    log("[OK] Đăng nhập xong, đã lưu. Lần sau không phải đăng nhập lại.")


def ensure_login(force=False):
    """Cài rclone nếu thiếu và đăng nhập Google nếu chưa. Lỗi thì sys.exit(thông báo)."""
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    if os.path.exists(OLD_CONF) and not os.path.exists(CONF):
        shutil.move(OLD_CONF, CONF)        # chuyển token cũ ra khỏi thư mục dữ liệu
        os.chmod(CONF, 0o600)
    install_rclone()
    if force and os.path.exists(CONF):
        os.remove(CONF)
    if not logged_in():
        login()
    else:
        log("[OK] Đã đăng nhập Google từ trước.")


def google_email():
    """Email của tài khoản Google đang đăng nhập (chữ thường), hoặc '' nếu không đọc được."""
    r = subprocess.run(rc("config", "userinfo", f"{REMOTE}:"), capture_output=True, text=True)
    if r.returncode == 0:
        m = EMAIL_RE.search(r.stdout)
        if m:
            return m.group(0).lower()
    # dự phòng: gọi thẳng Drive API bằng token trong rclone.conf (rclone đã làm mới token khi lsd)
    try:
        subprocess.run(rc("lsd", f"{REMOTE}:", "--max-depth", "1"), capture_output=True, timeout=120)
        conf = open(CONF).read()
        token = json.loads(re.search(r"^token\s*=\s*(\{.*\})\s*$", conf, re.M).group(1))
        r = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token['access_token']}",
                            "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)"],
                           capture_output=True, text=True)
        return json.loads(r.stdout)["user"]["emailAddress"].lower()
    except (OSError, ValueError, KeyError, AttributeError, subprocess.TimeoutExpired):
        return ""


def email_hash(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def list_user_isos(min_size=1_000_000_000):
    """Các file .iso (>=1GB) trong 'Drive của tôi' của tài khoản đang đăng nhập."""
    r = subprocess.run(rc("lsjson", f"{REMOTE}:", "-R", "--fast-list", "--files-only",
                          "--include", "*.{iso,ISO,Iso}"), capture_output=True, text=True)
    try:
        items = json.loads(r.stdout)
    except ValueError:
        return []
    items = [i for i in items if i.get("Size", 0) >= min_size]
    items.sort(key=lambda i: i["Path"].lower())
    return items[:30]


def download_from_drive(remote_path, dest, size):
    """Tải về file .part rồi mới đổi tên, đủ dung lượng mới thay => đứt giữa chừng không làm hỏng file cũ."""
    part = dest + ".part"
    r = subprocess.run(rc("copyto", f"{REMOTE}:{remote_path}", part, "--progress", "--stats", "10s"))
    if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) != size:
        if os.path.exists(part):
            os.remove(part)
        return False
    os.replace(part, dest)
    return True
