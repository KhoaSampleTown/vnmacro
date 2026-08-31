-- Ví dụ truy vấn kho parquet bằng duckdb.
--   duckdb -c ".read examples/queries.sql"
-- hoặc mở duckdb rồi copy từng câu.

-- Toàn bộ kho, đọc trực tiếp từ parquet (hive partition tự nhận).
CREATE OR REPLACE VIEW obs AS
SELECT * FROM read_parquet('data/curated/*/*/*.parquet', hive_partitioning = true);

-- 1) Có những dataset nào, bao nhiêu series, khoảng thời gian nào.
SELECT dataset, freq,
       count(*)                      AS rows,
       count(DISTINCT series_id)     AS series,
       min(date) AS from_date, max(date) AS to_date
FROM obs GROUP BY 1, 2 ORDER BY 1, 2;

-- 2) Thu chi ngân sách theo tháng (từ lời văn báo cáo).
SELECT date, series_id, value, unit
FROM obs
WHERE series_id LIKE 'NSO.FISCAL.%'
  AND series_id LIKE '%.MONTH'
ORDER BY date DESC, series_id
LIMIT 40;

-- 3) CPI: mức công bố theo kỳ gốc vs chuỗi đã chain.
--    Cột base_year cho thấy đúng chỗ NSO đổi rổ.
SELECT date,
       max(CASE WHEN series_id = 'NSO.CPI.BASE.HEADLINE'        THEN value END) AS published_level,
       max(CASE WHEN series_id = 'NSO.CPI.BASE.HEADLINE'
                THEN json_extract_string(dims, '$.base_year') END)             AS base_year,
       max(CASE WHEN series_id = 'NSO.CPI.MOM.HEADLINE'         THEN value END) AS mom_index,
       max(CASE WHEN series_id = 'NSO.CPI.CHAINED.HEADLINE'     THEN value END) AS chained,
       max(CASE WHEN series_id = 'NSO.CPI.CHAINED_YOY.HEADLINE' THEN value END) AS chained_yoy,
       max(CASE WHEN series_id = 'NSO.CPI.YOY.HEADLINE'         THEN value END) AS published_yoy
FROM obs
WHERE series_id LIKE 'NSO.CPI.%HEADLINE'
GROUP BY date ORDER BY date;

-- 4) Các tháng bị đứt đoạn do đổi kỳ gốc, và sai lệch YoY tại đó.
SELECT date,
       json_extract_string(dims, '$.base_year')    AS base_year,
       json_extract(dims, '$.yoy_residual')        AS yoy_residual_pp
FROM obs
WHERE series_id = 'NSO.CPI.CHAINED.HEADLINE'
  AND json_extract_string(dims, '$.rebasing_break') = 'true'
ORDER BY date;

-- 5) Thị phần xuất khẩu theo đối tác, trượt 12 tháng, kỳ mới nhất.
SELECT partner, share_12m
FROM read_parquet('data/panel/trade_shares.parquet')
WHERE flow = 'exports'
  AND date = (SELECT max(date) FROM read_parquet('data/panel/trade_shares.parquet'))
ORDER BY share_12m DESC NULLS LAST
LIMIT 15;

-- 6) FDI: vốn đăng ký (NSO) vs direct investment trong BOP (IMF).
SELECT date, series_id, partner, value, unit
FROM obs
WHERE series_id IN ('NSO.FDI.DISBURSED.YTD')
   OR (dataset = 'imf_bop' AND measure = 'D_F')
ORDER BY date DESC LIMIT 30;

-- 7) Kiểm tra revision: cùng một (series, kỳ) được công bố lại mấy lần.
SELECT series_id, date, count(DISTINCT vintage) AS n_vintages,
       min(value) AS lo, max(value) AS hi
FROM obs
WHERE dataset = 'nso_narrative'
GROUP BY 1, 2 HAVING count(DISTINCT vintage) > 1
ORDER BY n_vintages DESC, date DESC
LIMIT 25;
