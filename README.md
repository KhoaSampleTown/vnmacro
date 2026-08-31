# vnmacro

Thu thập dữ liệu kinh tế vĩ mô Việt Nam cho phân tích GDP / chính sách tài khoá /
chính sách tiền tệ / thương mại, lưu dưới dạng **Parquet**.

Ba nguồn:

| Nguồn | Lấy gì | Cách lấy |
|---|---|---|
| **NSO** (nso.gov.vn) | Báo cáo KTXH hàng tháng: 20 sheet biểu thống kê + phần lời văn; CPI chi tiết theo vùng/tỉnh | WordPress REST API (`/wp-json`) — không cần scrape HTML |
| **IMF** (api.imf.org) | Thương mại song phương theo đối tác, cán cân thanh toán (FDI vào/ra), tỷ giá, tài khoản quốc gia, CPI đối chiếu, giá hàng hoá | SDMX 2.1, API mở, không cần key |
| **SBV** (sbv.gov.vn) | M2, tiền gửi, dư nợ tín dụng theo ngành, nợ xấu, LDR | HTML server-rendered — **chỉ có tháng hiện tại** |
| **Hải quan** (customs.gov.vn) | Số liệu định kỳ XNK theo nước × mặt hàng | PDF — **có giới hạn, xem bên dưới** |

---

## Cài đặt

```bash
pip install vnmacro
```

Cài kèm phần đọc PDF (số liệu Hải quan và vài tháng CPI chỉ có PDF):

```bash
pip install "vnmacro[pdf]"
```

Hoặc cài từ mã nguồn để sửa parser:

```bash
git clone https://github.com/KhoaSampleTown/vnmacro
cd vnmacro && pip install -e ".[dev]"
```

> **Windows ARM64**: `pyarrow` không có wheel cho `win_arm64`, nên gói dùng
> **polars** để đọc/ghi parquet và **duckdb** để query SQL — không phụ thuộc
> pyarrow.

---

## Chạy

```bash
python -m vnmacro.cli all
```

Từng bước:

```bash
python -m vnmacro.cli nso --from-year 2015
```

```bash
python -m vnmacro.cli imf --start 2000
```

```bash
python -m vnmacro.cli sbv
```

```bash
python -m vnmacro.cli cpi
```

```bash
python -m vnmacro.cli panel
```

```bash
python -m vnmacro.cli status
```

Chạy lại là **incremental** — release nào đã xử lý thì bỏ qua (state ở
`data/_state/nso_done.json`); thêm `--force` để tải lại.

Khi sửa parser (thêm pattern, sửa regex), **không cần tải lại** — chạy lại
parser trên archive đã có, hoàn toàn offline:

```bash
python -m vnmacro.cli nso --reparse
```

212 release trong ~30 giây thay vì ~35 phút.

---

## Dữ liệu ra

```
data/
  raw/        file gốc .docx .xlsx .pdf (archive, append-only)
  curated/    dataset=<tên>/freq=<M|Q|A>/part-*.parquet    <- long format
  panel/      trade_shares.parquet, vn_macro_monthly.parquet
  _state/     con trỏ incremental
```

Đổi chỗ lưu bằng biến môi trường (nên làm nếu archive lớn, để tránh OneDrive
sync liên tục):

```bash
set VNMACRO_DATA_DIR=D:\data\vnmacro
```

### Schema `curated/`

Mỗi dòng là một quan sát:

`series_id`, `dataset`, `source`, `freq`, `date`, `ref_period`, `value`, `unit`,
`scale`, `status`, `vintage`, `partner`, `breakdown`, `label_vi`, `measure`,
`dims` (JSON), `raw_file`, `ingested_at`.

**`vintage` = ngày công bố của bản báo cáo chứa con số đó.** NSO sửa số "sơ bộ"
trong nhiều tháng sau, nên ước lượng DSGE trên số đã revised và trên số real-time
là hai bài toán khác nhau. Giữ vintage cho phép làm cả hai; các bước derived
luôn lấy vintage mới nhất cho mỗi (series, date).

Đọc bằng duckdb:

```bash
duckdb -c "SELECT * FROM 'data/curated/*/*/*.parquet' WHERE series_id LIKE 'NSO.FISCAL%' LIMIT 20"
```

hoặc polars:

```python
import polars as pl
df = pl.read_parquet("data/curated/dataset=nso_cpi/freq=M/*.parquet")
```

---

## CPI và chuyện đổi rổ hàng hoá

Đây là vấn đề bạn nêu, và là lý do có riêng module `transform/cpi_chain.py`.

NSO đổi quyền số và **kỳ gốc** khoảng 5 năm một lần (2009 → 2014 → 2019 → 2024).
Mỗi lần đổi, chỉ số **mức** quay về 100, nên nối thẳng các mức sẽ tạo ra một cú
nhảy giả.

Cách xử lý ở đây là cách chuẩn: **tỷ lệ tháng/tháng trước luôn được tính trong
cùng một rổ**, nên nối được qua mối nối kể cả khi mức thì không:

```
I(t) = I(t-1) × MoM(t) / 100 ,  I(mốc đầu) = 100
```

Pipeline phân loại từng cột theo *so với cái gì* — đọc từ chữ trong header
(`"Tháng 7 năm 2026 so với: Tháng 6 năm 2026"`), không theo vị trí cột, vì thứ
tự cột có thay đổi giữa các kỳ:

| series | nghĩa |
|---|---|
| `NSO.CPI.MOM.*` | so tháng trước — đầu vào để chain |
| `NSO.CPI.YOY.*` | so cùng kỳ năm trước |
| `NSO.CPI.YTD.*` | so tháng 12 năm trước |
| `NSO.CPI.AVG_YOY.*` | bình quân luỹ kế so cùng kỳ |
| `NSO.CPI.BASE.*` | mức công bố theo kỳ gốc — **đứt đoạn khi đổi rổ** |
| `NSO.CPI.CHAINED.*` | **chuỗi liên tục** sau khi chain |
| `NSO.CPI.CHAINED_YOY.*` | YoY suy ra từ chuỗi liên tục |

Hai chỉ báo kiểm tra đi kèm trong `dims`:

- `rebasing_break` — đánh dấu đúng tháng mà kỳ gốc đổi, tức chỗ mà nối thẳng
  sẽ nhảy;
- `yoy_residual` — YoY chain trừ YoY công bố. Bình thường ≈ 0. Nếu lệch đáng kể
  ngay tại mối nối thì đó là **hiệu ứng đổi quyền số**, nên nhìn kỹ trước khi
  đưa vào ước lượng.

`python -m vnmacro.cli cpi` in ra tóm tắt các mối nối tìm được.

Ngoài ra `imf_cpi` (CPI do IMF hài hoà) được thu thập song song để đối chiếu độc
lập với chuỗi chain từ NSO.

---

## Thương mại và FDI

**Thị phần theo quốc gia** lấy từ **IMF IMTS** (`International Trade in Goods by
partner country`, kế thừa DOTS) — API mở, tần suất tháng, đầy đủ lịch sử:

- `XG_FOB_USD` xuất khẩu FOB, `MG_CIF_USD` nhập khẩu CIF, `TBG_USD` cán cân,
  theo từng `COUNTERPART_COUNTRY`.
- `transform/panel.py` tính `share` (theo tháng) và `share_12m` (trượt 12
  tháng). **Dùng `share_12m` để hiệu chỉnh** — thị phần từng tháng nhiễu do
  thời điểm giao hàng và Tết.

**FDI** có ở hai chỗ, khác định nghĩa:

| series | nghĩa |
|---|---|
| `NSO.fdi.*` (biểu 12) | vốn **đăng ký** — cấp mới, điều chỉnh, theo tỉnh/đối tác |
| `NSO.FDI.DISBURSED.YTD` | vốn **thực hiện**, luỹ kế (từ lời văn) |
| `IMF.BOP.D_F` + `L_NIL_T` | direct investment **vào** Việt Nam (BOP) |
| `IMF.BOP.D_F` + `A_NFA_T` | đầu tư trực tiếp **ra nước ngoài** |
| `IMF.BOP.D_F` + `NNAFANIL_T` | ròng |

---

## Giới hạn đã biết

**1. Hải quan có CAPTCHA.** Catalog "Số liệu định kỳ" đi qua
`/bridge?url=/customs/api/GetTKHQInfo`, và mọi lời gọi ngoài phiên trình duyệt
đều trả `{"message": "Invalid Captcha"}`. Pipeline **không** tìm cách vượt qua
chuyện đó. Nếu cần số liệu hải quan chi tiết:

1. mở `https://www.customs.gov.vn/index.jsp?pageId=5002`
2. DevTools → Network → reload, copy JSON của request `GetTKHQInfo`
3. lưu vào `data/raw/customs/catalog_YYYY-MM-DD.json`
4. `python -m vnmacro.cli customs --catalog <file đó> --parse`

File PDF trên `files.customs.gov.vn` thì mở, tải và parse tự động được. Parser
PDF để mặc định **tắt** (`parse_pdfs: false`) vì bảng gộp nhiều dòng logic vào
một ô — dùng được nhưng cần kiểm tra kỹ. Lịch sử song phương tự động thì đã có
IMF IMTS thay thế.

**2. IMF không có dữ liệu tiền tệ cho Việt Nam.** Đã probe tháng 8/2026:
`MFS_MA` (M1/M2), `MFS_IR` (lãi suất), `EER` (NEER/REER), `IRFCL` (dự trữ),
`QNEA` (GDP quý), `QGFS` (tài khoá quý) — **đều rỗng cho VNM**. Vì vậy:

- **GDP quý**: từ lời văn báo cáo quý (`GDP.GROWTH.QUARTER_YOY`);
- **Tài khoá**: từ lời văn báo cáo tháng (`FISCAL.*`) — nguồn gốc là Bộ Tài chính;
- **Tiền tệ**: từ **SBV** (`SBV.*`), xem mục 3.

Các flow rỗng vẫn để trong `vnmacro/config/sources.yaml` với `enabled: false` kèm lý do,
để không phải dò lại.

**3. SBV chỉ công bố tháng hiện tại, không có kho lưu trữ.** Mỗi trang thống kê
của Ngân hàng Nhà nước chỉ hiện số của tháng mới nhất — **không lùi lại được**.
Lịch sử vì thế chỉ tích luỹ được bằng cách **chạy pipeline hàng tháng**. Mỗi
quan sát được gán tháng tham chiếu đọc từ chính caption của bảng, nên chạy lại
là idempotent, và một tháng bị bỏ lỡ sẽ nằm im chứ không bị điền sai kỳ.

SBV còn đứng sau một WAF (F5) trả về trang "Request Rejected" 246 byte với mã
**200** khi bị gọi dồn. Pipeline nhận diện trang đó, **bỏ qua và ghi log** chứ
không parse nhầm thành dữ liệu; mỗi trang cách nhau 6 giây. Nếu thấy log báo
skip thì chạy lại sau ít phút — trước khi SBV lật sang tháng mới.

Series lấy được: `SBV.M2.*` (tổng phương tiện thanh toán, tiền gửi TCKT và dân
cư), `SBV.CREDIT.*` (dư nợ tín dụng theo ngành, có `TONG-CONG`), `SBV.LDR.*` —
mỗi cái có `.LEVEL` (tỷ đồng) và `.GROWTH_YTD` (% so với cuối năm trước).

**Chưa lấy được** (đã probe 31/8/2026, đều render bằng JS nên HTML server trả
về không có bảng): tỷ lệ nợ xấu, tiền mặt/M2, lãi suất điều hành, lãi suất liên
ngân hàng. Muốn có thì phải chạy headless browser — đổi selector không giải
quyết được. Các trang này để `enabled: false` trong `vnmacro/config/sources.yaml` kèm
lý do.

**4. Kỳ tham chiếu lấy từ tiêu đề, không lấy từ ngày đăng.** NSO migrate site
năm 2019 nên báo cáo tháng 1/2005 mang `date = 2019-04-16`. `util.parse_period`
đọc kỳ từ tiêu đề (cả dạng chữ "tháng Bảy" lẫn số "tháng 01") rồi mới tới slug.

**5. Báo cáo quý chứa số liệu tháng cuối quý.** "Quý II và sáu tháng đầu năm
2026" mang CPI và biểu thương mại của **tháng 6**. Dữ liệu tháng vì thế được gán
`month_date` = tháng cuối quý; nếu gán vào tháng đầu quý thì chuỗi chain CPI sẽ
nối nhầm tháng.

**6. Báo cáo cũ dùng định dạng cũ.** Trước ~2017 NSO phát hành `.xls` (BIFF) và
`.doc` (Word 97) thay vì `.xlsx`/`.docx`:

- `.doc` → đọc được. `python-docx` không mở được, nên pipeline lấy thẳng stream
  `WordDocument` rồi decode UTF-16LE (cách Word lưu tiếng Việt) và giữ các đoạn
  dài có dấu. Cách này thô nhưng an toàn: pattern trong
  `narrative_patterns.yaml` rất dài và cụ thể, text hỏng thì **không khớp** chứ
  không khớp nhầm.
- `.xls` → đọc được phần lớn qua `xlrd`. Còn **7 file** (2015-M01/M02,
  2016-M01/M02, 2018-Q4) chứa chuỗi UTF-16 lỗi mà `xlrd` từ chối, không có
  tham số nào vượt qua được. Muốn lấy: mở bằng Excel/LibreOffice, lưu lại
  thành `.xlsx` vào đúng thư mục `data/raw/nso/...`, rồi `nso --reparse`.

Hệ quả: chuỗi CPI chain hiện thiếu **3 tháng** (2016-03, 2019-01, 2020-10) và
`cli cpi` in cảnh báo mỗi lần chạy cho tới khi được vá.

---

## Hai cái bẫy đã sập một lần — có test giữ

`python tests/test_parsing.py` (không cần pytest). Cả hai lỗi này **không ném
exception**, chỉ cho ra số sai:

1. **Số kiểu Việt Nam.** Dấu chấm là phân cách nghìn, dấu phẩy là thập phân.
   `1.225.073` tỷ đồng dư nợ mà đọc dấu chấm thành thập phân thì ra `1.2` —
   sai sáu bậc, không có cảnh báo nào.
2. **Dấu tiếng Việt trong regex.** "so với" dùng `ớ` (U+1EDB) chứ không phải
   `ơ`. Pattern viết `v[ơo]i` không khớp → không tách được vế so sánh → tháng
   *chủ thể* bị đọc thành tháng *tham chiếu*, và mọi cột CPI trông như đang so
   với chính nó. Vì vậy mọi so khớp tiếng Việt trong code đều **bỏ dấu trước**
   (`util.strip_accents`) rồi mới match.

---

## Cấu hình

| File | Nội dung |
|---|---|
| `vnmacro/config/sources.yaml` | nguồn nào bật/tắt, flow IMF nào lấy, sheet ưu tiên, năm backfill |
| `vnmacro/config/narrative_patterns.yaml` | regex bóc số từ lời văn — phần dễ vỡ nhất được tách khỏi code |

Chữ trong báo cáo đổi theo tháng ("ước đạt" / "đạt", có lúc chèn "gần", "khoảng").
Pattern không khớp thì chỉ **log**, không làm hỏng run. Xem dòng
`narrative patterns unmatched` để biết cần sửa gì.

---

## Panel cho DSGE

`data/panel/vn_macro_monthly.parquet` — mỗi dòng một tháng, mỗi cột một chỉ tiêu
(CPI chain, thu/chi NSNN, cán cân thương mại, FDI thực hiện, VND/USD...).

`data/panel/trade_shares.parquet` — `date × partner × flow` với `share` và
`share_12m`.

Hai file này là **derived, ghi đè mỗi lần chạy**; nguồn sự thật luôn là
`curated/`.

---

## Tuyên bố miễn trừ

Đọc phần này trước khi dùng.

**Thư viện lấy dữ liệu, không cấp quyền truy cập.** `vnmacro` chỉ tự động hoá
những lời gọi mà trình duyệt của bạn vẫn thực hiện khi vào các trang này: API
REST công khai của Tổng cục Thống kê, SDMX mở của IMF, trang HTML của Ngân hàng
Nhà nước. Nó không vượt qua lớp kiểm soát nào và **không tìm cách vượt CAPTCHA**
— đó là lý do phần Hải quan phải để người dùng tự lấy catalog bằng trình duyệt.

**Bạn chịu trách nhiệm tuân thủ điều khoản của từng nguồn.** NSO, IMF, SBV và
Hải quan mỗi bên có quy định riêng về sử dụng, lưu trữ và phân phối lại dữ liệu,
và các quy định đó đổi được mà không báo trước. Tự kiểm tra — nhất là khi dùng
cho mục đích thương mại hoặc phân phối lại cho bên thứ ba.

**Dữ liệu không thuộc giấy phép MIT của gói.** MIT áp cho *mã nguồn*. Dữ liệu
tải về thuộc về đơn vị công bố. Repo này không chứa và sẽ không chứa file dữ
liệu nào — `.gitignore` chặn toàn bộ `data/`, `*.parquet`, `*.docx`, `*.xlsx`.

**Nhịp gọi là mức tối thiểu, không phải mức khuyến nghị.** SBV đứng sau WAF (F5)
trả trang *"Request Rejected"* kèm **mã 200** khi bị gọi dồn; gói nhận diện trang
đó và bỏ qua thay vì parse nhầm thành dữ liệu, đồng thời giãn 6 giây giữa các
trang. Đừng hạ nhịp, đừng chạy song song. Đây là hạ tầng công dùng chung.

**Không bảo đảm tính chính xác.** Số được bóc từ `.docx`, `.xlsx`, `.doc` cũ,
PDF và lời văn báo cáo; cách hành văn của NSO đổi theo tháng nên pattern không
khớp là thiếu số. Mục *Hai cái bẫy đã sập một lần* ở trên là các lỗi **đã biết
và đã sửa** — gần như chắc chắn còn lỗi chưa biết. Trường `vintage` được giữ lại
chính vì lý do này: NSO sửa số sơ bộ trong nhiều tháng, và ước lượng trên số đã
chỉnh khác hẳn ước lượng trên số real-time.

**Không phải tư vấn đầu tư.** Gói cung cấp dữ liệu thô, không đưa ra khuyến nghị.

**Cung cấp nguyên trạng.** Không bảo hành dưới bất kỳ hình thức nào. Tác giả
không chịu trách nhiệm cho thiệt hại phát sinh từ việc sử dụng gói này, bao gồm
tổn thất tài chính, mất quyền truy cập nguồn, hay hậu quả pháp lý từ việc vi phạm
điều khoản của bên thứ ba.

---

## Dữ liệu thị trường

Trái phiếu chính phủ, trái phiếu doanh nghiệp, OMO và fixing lãi suất — từ HNX,
cbonds, SBV, VIRA và VBMA — nằm ở gói riêng
[`vnbond`](https://github.com/KhoaSampleTown/vnbond), phát hành độc lập.

---

## Giấy phép

Mã nguồn: MIT — xem `LICENSE`. Dữ liệu: xem phần Tuyên bố miễn trừ ở trên.
