# ② 数据质检

**定位**：面向数据运营人员的质检助手——不写代码也能问"哪些表问题最多、为什么、怎么处理"。支持批量文件导入与结果清单导出（对客确认的硬性要求）。

![效果](../docs/02-数据质检-门户与质检助手.png)

## 组成

| 文件 | 说明 |
|---|---|
| `build_demo_data.py` + `schema_demo.sql` | 生成虚构质检底座 `dq_poc.db`：质检规则 960+、规则执行 2000+、问题 2000+、问题工单 120、用数需求 80、处理规则知识 40 条 |
| `mock_api.py`（8090） | 零依赖 mock API：六维统计、按域/表/维度下钻、规则执行成功率、处理建议检索 |
| `demo_server.py`（8096） | 统一演示页：共享平台门户打底 + 右侧质检助手对话框 |
| `report_builder.py` | 质检报告生成（统计口径在代码里一次算清，模型不复算） |
| `openapi.json` | mock API 的 OpenAPI 描述 |
| `register_dify_tool.py` | 把 mock API 注册为 Dify 工作区自定义工具（幂等） |
| `build_chatflow.py` / `build_chatflow_v3.py` | 自动构建质检 Chatflow（意图识别 → 工具调用 → 结果组织） |
| `e2e_*_test.py` | 端到端用例：意图 / 报告 / 对话 |

## 设计要点

- **数由代码算，话由模型说**：所有派生字段（维度中文化、数据域标签、执行时长、阈值换算）在数据构建层一次算清，模型侧禁止自行计算
- **分级建议**：处理建议分三级——可直接执行 / 疑似待判 / 现象上报（`handling_rule.grade`），回答时附证据条数
- **口径显式化**：维度映射、统计期、强弱规则口径全部落在 `caliber` / `dimension_map` 表，可人工校对后重跑

## 运行

```bash
python build_demo_data.py        # 生成 dq_poc.db（虚构数据）
python demo_server.py --port 8096
```
