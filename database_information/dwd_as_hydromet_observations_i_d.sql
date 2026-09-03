-- donghai_dwd.dwd_as_hydromet_observations_i_d definition

CREATE TABLE `dwd_as_hydromet_observations_i_d` (
  `station_id` bigint NOT NULL COMMENT '国控站点ID',
  `observed_at` date NOT NULL COMMENT '观测时间',
  `depth_m` varchar(50) NOT NULL DEFAULT "" COMMENT '水深 m',
  `id` bigint NOT NULL COMMENT '自增ID',
  `ship_id` bigint NULL COMMENT '观测船id',
  `sea_state` varchar(100) NULL COMMENT '海况',
  `wave_type` varchar(100) NULL COMMENT '波型',
  `max_wave_height_m` decimal(5,2) NULL COMMENT '最大波高 m',
  `wave_level` varchar(100) NULL COMMENT '波级',
  `sea_glow_type` varchar(200) NULL COMMENT '海发光类型',
  `sea_glow_intensity` varchar(100) NULL COMMENT '海发光强度',
  `wind_wave` varchar(100) NULL COMMENT '风浪',
  `swell` varchar(100) NULL COMMENT '涌浪',
  `wind_speed_ms` decimal(5,1) NULL COMMENT '风速 （m/s）',
  `relative_humidity` decimal(5,1) NULL COMMENT '相对湿度 %',
  `air_pressure_hpa` decimal(7,1) NULL COMMENT '气压 hPa',
  `visibility_km` decimal(6,1) NULL COMMENT '能见度 km',
  `total_cloud` decimal(3,1) NULL COMMENT '总云量（%）',
  `low_cloud` decimal(3,1) NULL COMMENT '低云量（%）',
  `transparency_m` decimal(5,1) NULL COMMENT '透明度 m',
  `water_color` varchar(200) NULL COMMENT '水色',
  `wind_direction_deg` int NULL COMMENT '风向 °',
  `effective_wave_height_m` decimal(5,2) NULL COMMENT '有效波高 （m）',
  `effective_wave_period_s` decimal(5,1) NULL COMMENT '有效波周期 （s）',
  `air_temperature_c` decimal(5,1) NULL COMMENT '干球温度（ ℃）',
  `station_name` varchar(100) NULL COMMENT '站位名称',
  `ship_name` varchar(100) NULL COMMENT '船舶名称',
  `longitude` decimal(10,6) NULL COMMENT '经度',
  `latitude` decimal(10,6) NULL COMMENT '纬度'
) ENGINE=OLAP
UNIQUE KEY(`station_id`, `observed_at`, `depth_m`)
COMMENT '断面水文数据';
