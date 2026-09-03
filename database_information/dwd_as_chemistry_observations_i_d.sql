-- donghai_dwd.dwd_as_chemistry_observations_i_d definition

CREATE TABLE `dwd_as_chemistry_observations_i_d` (
  `station_id` bigint NOT NULL COMMENT '站点ID',
  `observed_at` date NOT NULL COMMENT '观测时间',
  `depth_m` varchar(50) NOT NULL COMMENT '层次（深度m）',
  `id` bigint NOT NULL COMMENT '自增ID',
  `ship_id` bigint NULL COMMENT '观测船ID',
  `dissolved_oxygen_umol_dm3` decimal(8,2) NULL COMMENT '溶解氧 （μmol/dm³）',
  `temperature_c` decimal(5,2) NULL COMMENT '温度 T（ ℃）',
  `salinity_psu` decimal(6,3) NULL COMMENT '盐度 S（PSU）',
  `po4_p_umol_dm3` decimal(8,3) NULL COMMENT '活性磷酸盐 PO4-P （μmol/dm³）',
  `sio3_si_umol_dm3` decimal(8,3) NULL COMMENT '活性硅酸盐 SiO3-Si （μmol/dm³）',
  `no2_n_umol_dm3` decimal(8,3) NULL COMMENT '亚硝酸盐 NO2-N （μmol/dm³）',
  `no3_n_umol_dm3` decimal(8,3) NULL COMMENT '硝酸盐 NO3-N （μmol/dm³）',
  `nh4_n_umol_dm3` decimal(8,3) NULL COMMENT '氨氮 NH4-N （μmol/dm³）',
  `ph_value` decimal(4,2) NULL COMMENT '酸碱度 pH',
  `alkalinity_mmol_dm3` decimal(6,3) NULL COMMENT '碱度 A （mmol/dm³）',
  `station_name` varchar(100) NULL COMMENT '站位名称',
  `ship_name` varchar(100) NULL COMMENT '船舶名称',
  `longitude` decimal(10,6) NULL COMMENT '经度',
  `latitude` decimal(10,6) NULL COMMENT '纬度'
) ENGINE=OLAP
UNIQUE KEY(`station_id`, `observed_at`, `depth_m`, `id`)
COMMENT '断面化学数据';
