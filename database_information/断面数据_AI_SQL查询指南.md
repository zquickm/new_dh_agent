# 断面数据 AI SQL 查询指南（精简版）

用途：作为 AI 生成 MySQL 查询的上下文。数据库为 `document`，仅允许使用以下 3 张表。

## 1. 表与字段

### `section_stations`：断面站位

- `id`：主键；供观测表的 `station_id` 关联。
- `code`：唯一站位代码，如 E1001、E2001、E3001。
- `longitude`、`latitude`：经纬度，类型为字符串。
- 数据库没有独立的断面字段。按站号前缀识别断面：`E1%` 为 E1，`E2%` 为 E2，`E3%` 为 E3。

### `hydromet_observations`：水文气象观测

主字段：

- 标识：`id, station_id, ship_id, observed_at`；`observed_at` 是观测日期。
- 水深与海况：`depth_m` 水深、`sea_state` 海况、`wave_type` 波型、`wave_level` 波级。
- 波浪：`max_wave_height_m` 最大波高、`effective_wave_height_m` 有效波高、`effective_wave_period_s` 有效波周期、`wind_wave` 风浪、`swell` 涌浪。
- 风：`wind_speed_ms` 风速、`wind_direction_deg` 风向。
- 气象：`air_temperature_c` 气温/干球温度、`relative_humidity` 相对湿度、`air_pressure_hpa` 气压、`visibility_km` 能见度、`total_cloud` 总云量、`low_cloud` 低云量。
- 海洋现象：`transparency_m` 透明度、`water_color` 水色、`sea_glow_type` 海发光类型、`sea_glow_intensity` 海发光强度。

粒度：一个站位、一个日期最多一条，唯一键为 `(station_id, observed_at)`。

### `chemistry_observations`：海水化学观测

主字段：

- 标识：`id, station_id, ship_id, observed_at, depth_m`；`depth_m` 是采样层次/深度。
- 基础水体指标：`temperature_c` 海水温度、`salinity_psu` 盐度、`pH_value` pH。
- 溶解氧：`dissolved_oxygen_umol_dm3`。
- 营养盐：`PO4_P_umol_dm3` 活性磷酸盐、`SiO3_Si_umol_dm3` 活性硅酸盐、`NO2_N_umol_dm3` 亚硝酸盐、`NO3_N_umol_dm3` 硝酸盐、`NH4_N_umol_dm3` 氨氮。
- `alkalinity_mmol_dm3`：碱度。

粒度：一个站位、一个日期、一个深度最多一条，唯一键为 `(station_id, observed_at, depth_m)`。

## 2. 查询规则

- 关联条件：`section_stations.id = hydromet_observations.station_id` 或 `section_stations.id = chemistry_observations.station_id`。
- 查询某断面：E1 用 `code LIKE 'E1%'`，E2/E3 同理。
- 查询月份使用左闭右开日期范围，例如 2022 年 5 月：`>= '2022-05-01' AND < '2022-06-01'`。
- 测量字段大多是字符串。展示原值可直接查询；计算平均值等统计量时，只转换符合数字格式的值：

```sql
CASE WHEN TRIM(field) REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'
     THEN CAST(field AS DECIMAL(12,4)) END
```

- 用户说“水温”时查 `chemistry_observations.temperature_c`；说“气温/干球温度”时查 `hydromet_observations.air_temperature_c`；只说“温度”时默认同时查询两者并标明类型。
- 水质评价主要使用化学表中的溶解氧、pH、磷酸盐、硅酸盐、亚硝酸盐、硝酸盐、氨氮、碱度，并以水温、盐度、深度作为背景。
- 数据库没有水质等级和评价标准。没有给出标准时，只能描述指标范围、变化和异常，不能直接判定国家水质等级。
- 当前没有字段：湿球温度、绝对湿度、波速、明确波向、波长、声速、断面报告。
- `wind_wave` 和 `swell` 格式未定义，不要擅自解释为数值波向。
- 两张观测表只有日期，没有观测时分秒。
