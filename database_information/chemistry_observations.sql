-- document.chemistry_observations definition

CREATE TABLE `chemistry_observations` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `station_id` bigint NOT NULL,
  `ship_id` bigint DEFAULT NULL,
  `observed_at` date NOT NULL,
  `depth_m` varchar(50) NOT NULL COMMENT '层次（深度）m',
  `dissolved_oxygen_umol_dm3` varchar(50) DEFAULT NULL COMMENT '溶解氧（DO/O2 合并）μmol/dm³',
  `temperature_c` varchar(50) DEFAULT NULL COMMENT '温度 T ℃',
  `salinity_psu` varchar(50) DEFAULT NULL COMMENT '盐度 S（PSU）',
  `PO4_P_umol_dm3` varchar(50) DEFAULT NULL COMMENT '活性磷酸盐 PO4-P μmol/dm³',
  `SiO3_Si_umol_dm3` varchar(50) DEFAULT NULL COMMENT '活性硅酸盐 SiO3-Si μmol/dm³',
  `NO2_N_umol_dm3` varchar(50) DEFAULT NULL COMMENT '亚硝酸盐 NO2-N μmol/dm³',
  `NO3_N_umol_dm3` varchar(50) DEFAULT NULL COMMENT '硝酸盐 NO3-N μmol/dm³',
  `NH4_N_umol_dm3` varchar(50) DEFAULT NULL COMMENT '氨氮 NH4-N μmol/dm³',
  `pH_value` varchar(50) DEFAULT NULL COMMENT '酸碱度 pH',
  `alkalinity_mmol_dm3` varchar(50) DEFAULT NULL COMMENT '碱度 A mmol/dm³',
  PRIMARY KEY (`id`),
  UNIQUE KEY `ux_chem_station_time_depth` (`station_id`,`observed_at`,`depth_m`),
  KEY `ship_id` (`ship_id`),
  CONSTRAINT `chemistry_observations_ibfk_1` FOREIGN KEY (`station_id`) REFERENCES `section_stations` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `chemistry_observations_ibfk_2` FOREIGN KEY (`ship_id`) REFERENCES `ships` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=78999 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
