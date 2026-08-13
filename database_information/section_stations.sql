-- document.section_stations definition

CREATE TABLE `section_stations` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `code` varchar(100) NOT NULL COMMENT '断面站点代码：A/B/C/E1001 等',
  `longitude` varchar(100) DEFAULT NULL COMMENT '经度',
  `latitude` varchar(100) DEFAULT NULL COMMENT '纬度',
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`)
) ENGINE=InnoDB AUTO_INCREMENT=163 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


