"""
vnmacro — dữ liệu kinh tế vĩ mô Việt Nam, lấy thẳng từ nguồn công khai.
========================================================================
GDP, CPI, thu chi ngân sách, thương mại song phương, cán cân thanh toán, tiền tệ
— từ Tổng cục Thống kê, IMF và Ngân hàng Nhà nước. Không cần máy chủ: thư viện
chạy trên máy bạn và gọi trực tiếp các endpoint công khai.

    python -m vnmacro.cli all          # chạy hết
    python -m vnmacro.cli nso --from-year 2015
    python -m vnmacro.cli status

Dữ liệu ra dạng parquet trong `data/curated/`. Đổi chỗ lưu bằng biến môi trường
`VNMACRO_DATA_DIR`.

MIỄN TRỪ. Gói lấy dữ liệu, không cấp quyền truy cập, và không bảo đảm tính chính
xác. Bạn chịu trách nhiệm tuân thủ điều khoản của từng nguồn. In
``vnmacro.DISCLAIMER`` hoặc xem README để đọc đầy đủ.
"""

__version__ = "0.1.0"

DISCLAIMER = """TUYÊN BỐ MIỄN TRỪ — vnmacro

1. LẤY DỮ LIỆU, KHÔNG CẤP QUYỀN TRUY CẬP
   Gói chỉ tự động hoá những lời gọi mà trình duyệt của bạn vẫn thực hiện: API
   REST công khai của NSO, SDMX mở của IMF, trang HTML của SBV. Không vượt lớp
   kiểm soát nào và KHÔNG vượt CAPTCHA — đó là lý do phần Hải quan yêu cầu người
   dùng tự lấy catalog bằng trình duyệt.

2. BẠN CHỊU TRÁCH NHIỆM TUÂN THỦ ĐIỀU KHOẢN TỪNG NGUỒN
   NSO, IMF, SBV và Hải quan mỗi bên có quy định riêng về sử dụng, lưu trữ và
   phân phối lại, và các quy định đó đổi được mà không báo trước.

3. DỮ LIỆU KHÔNG THUỘC GIẤY PHÉP MIT CỦA GÓI
   MIT áp cho mã nguồn. Dữ liệu tải về thuộc về đơn vị công bố.

4. NHỊP GỌI LÀ MỨC TỐI THIỂU, KHÔNG PHẢI MỨC KHUYẾN NGHỊ
   SBV đứng sau WAF trả trang chặn kèm mã 200 khi bị gọi dồn; gói giãn 6 giây
   giữa các trang. Đừng hạ nhịp, đừng chạy song song.

5. KHÔNG BẢO ĐẢM TÍNH CHÍNH XÁC
   Số được bóc từ .docx, .xlsx, .doc cũ, PDF và lời văn; cách hành văn của NSO
   đổi theo tháng nên pattern không khớp là thiếu số. Trường `vintage` được giữ
   vì NSO sửa số sơ bộ trong nhiều tháng.

6. KHÔNG PHẢI TƯ VẤN ĐẦU TƯ
   Gói cung cấp dữ liệu thô. Mọi quyết định đầu tư là của bạn.

7. CUNG CẤP NGUYÊN TRẠNG
   Không bảo hành dưới bất kỳ hình thức nào. Tác giả không chịu trách nhiệm cho
   thiệt hại phát sinh từ việc sử dụng gói.
"""

__all__ = ["DISCLAIMER", "__version__"]
