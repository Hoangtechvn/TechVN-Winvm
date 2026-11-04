# winvm - Windows 11 và Linux trên GitHub Codespaces

Tạo Codespace từ repo này, terminal tự chạy `winvm.py`: tải ISO, cài Windows tự động bằng QEMU,
rồi cho bạn vào bằng RDP (Tailscale) hoặc xem màn hình qua cổng web **riêng tư** của Codespaces.

## Cách dùng
1. Bấm **Code → Codespaces → Create codespace** (nên chọn máy **4-core / 16GB RAM** trở lên).
2. Terminal tự mở và hiện menu. Bấm link cổng **6080** (hoặc link terminal in ra) là vào thẳng màn hình máy ảo. Lần đầu VS Code có thể hỏi "Allow automatic tasks" → bấm **Allow**.
   Không tự chạy thì: **Terminal → Run Task → Chạy Windows**, hoặc gõ `python3 winvm.py`.
3. Chọn nguồn cài: `1` ISO của bạn (up lên Google Drive của bạn), `2` ISO có sẵn (nếu người cài đặt cấu hình), `3` ổ ảo admin (chỉ admin, người khác bị từ chối).
4. Windows cài tự động ~10-20 phút. Tài khoản: `Administrator` / `admin` (đổi mật khẩu sau khi vào).

Lệnh: `python3 winvm.py stop` (tắt) · `python3 winvm.py reinstall` (cài lại, XOÁ ổ ảo) ·
(Admin có thêm `saoluu.py` riêng tư để sao lưu ổ ảo lên Google Drive; file đó KHÔNG nằm trong repo này.)

## Cài Linux
- Chọn ISO Linux ở mục `1` (ISO trong Drive của bạn) hoặc `2` → *Nhập link tải ISO khác* (dùng link chính thức của bản phân phối). Script **tự nhận diện** ISO là Windows hay Linux (ép bằng `--os linux`).
- Mỗi hệ điều hành một thư mục riêng: Windows ở `/workspaces/winvm`, Linux ở `/workspaces/linuxvm`. Chạy một máy ảo tại một thời điểm.
- Linux cài **thủ công** qua màn hình web (trình cài đặt của ISO). Gợi ý: tên máy `linuxvm`, tài khoản `root`. Cài xong, khởi động lại vào Linux rồi chạy `python3 winvm.py done` để lần sau vào thẳng ổ.
- SSH vào Linux qua Tailscale: cổng `2222` (cần cài `openssh-server`). Chỉ hỗ trợ ISO khởi động kiểu BIOS (chưa có UEFI).

## Lưu ý an toàn
- **Không commit** file ổ ảo, ISO, token. `.gitignore` đã chặn; token Google và khoá Tailscale lưu ở `~/.config/winvm/` (ngoài repo).
- Repo **không kèm ISO Windows**. Bạn tự chịu trách nhiệm bản quyền/giấy phép của ISO mình dùng. Không đưa link tải ISO vào mã nguồn công khai; đặt link bằng Codespaces secret `WINVM_CATALOG` (JSON: `[["Tên","link"]]`).
- Mục `3` cần `saoluu.py` (riêng tư, chỉ admin giữ) nên người dùng thường không dùng được. Bảo vệ thật là **không chia sẻ file ổ ảo** trên Drive.
- Giờ miễn phí Codespaces có hạn; dùng xong nhớ **Stop codespace**.
