---
name: 疑点疑区
description: 疑点疑区图斑的海域权属、生态修复项目、历史疑点、自然岸线、生态保护红线研判，结果筛选、分页清单、统计和地图展示。
---

# 疑点疑区

## 工具选择

- 分析、检查、重新研判：`analyze_suspicion`。固定①海域权属→②生态修复项目→③历史疑点→④自然岸线→⑤生态保护红线，全部调用真实规则服务，不提供规则选择参数。
- 不传 batchNo 取最新可排序批次（当前格式如 XM-2026-06）；不传 patchNos 处理整批。保留用户指定范围，单图斑也传数组。
- 默认 save=false 试算。明确要求保存、更新结果时传 true，会覆盖所选图斑同规则历史结果。逐规则查看 saved，不能把部分保存说成全部成功。
- 框架发送整体开始/结束，工具每条规则结束后发送一个process，不用模型文字模拟进度。
- 查清单、某图斑结论：`query_suspicion_results`。默认 page=1、pageSize=10，最多20；仅当需要解释具体图斑时传 includeEvidence=true。
- 筛选参数 suspicionTypes；match=any 满足任一，all 同时满足。工具先筛选全部结果，再按图斑编号排序分页。total 是匹配总数，不是当前页数量；hasMore=true 时不能把当前页当全部。用户继续查看时保持条件和pageSize不变，递增page。
- 查数量：`summarize_suspicion_results`，不用分页明细自行计算总数。该工具统计整批正式结果；指定图斑范围的本次统计使用执行摘要，不用整批统计替代。
- 查询和统计只读取空间服务已保存的正式批次结果，每次重新读取。执行摘要不暂存明细；save=false后查询到的正式结果不能称为该次试算结果。
- 用户明确要求研判后查看明细、翻页时，需要保存才能完成，说明覆盖行为并使用save=true；明确要求不保存时只提供本次摘要，不自动保存。后续查询沿用执行返回的batchNo和用户指定patchNos，保持范围一致。

## 结果口径

- SEA_RIGHTS：NO_APPROVAL 未批先建；OUT_OF_SCOPE 超范围用海，默认超出面积>0.05公顷且比例>5%；CHANGE_MODE 改变用海方式；CLEAR 该规则合规。阈值以返回值为准。
- ECO_RESTORE：ECO_OUT_OF_SCOPE 修复项目内超出权属范围；ECO_CHANGE_MODE 修复项目内改变用海方式；NO_APPROVAL 项目内无权属且不满足免审批；EXEMPT 免审批合规，不计为疑点。免审批情形包括无构筑物的植被种植、互花米草清理、沙滩补沙，或工期不足3个月的拆除。CLEAR 可能为项目外，也可能为项目内有权属且合规，结合inProject区分。
- HISTORY：HISTORY 命中历史核查记录，多条相交取核查日期最新记录，引用relatedId、checkConclusion、checkDate。历史关联不直接证明新增违法；CLEAR 未命中历史记录。
- COASTLINE：OCCUPY_COASTLINE 占用自然岸线，自然岸线占用长度>0。任意岸线长度 anyLengthM 仅为背景，不参与判定。
- REDLINE：REDLINE 占用生态保护红线，重叠面积>0；边界相切不算，红线合并后计算以免重复。
- assessedRules、complete、missingRules 及合规统计覆盖五条规则。hasSuspicion=null 表示规则结果不足且已知结论没有疑点，不能当作合规。EXEMPT只对生态修复规则合规，不抵消其他规则疑点；五条规则均齐全且全部为CLEAR或EXEMPT才计入allClearPatchCount。
- 疑点总数使用去重 suspicionPatchCount，不相加各规则计数。incompletePatchCount 和疑点数可能重叠。
- PARTIAL_SUCCESS 应说明失败规则；失败规则counts=null，不能说零疑点。未研判与读取失败要区分。
- Ha公顷、M米、Pct百分比；源overlapRatio单位未明确，不转成百分比。引用真实evidence，不编造结论。红树林→其他方式映射尚待确认。

## 地图

工具仅返回属性。通过已有 map_action 的 GEOJSON_ADD 查询展示：suspicion_patch_result 为正式图斑及结论，suspicion_patch 仅图斑，eco_redline 为红线。过滤字段先查询目录详情，不猜字段。未保存试算不能当作正式结论图层；需要时展示图斑位置并说明试算结论。
