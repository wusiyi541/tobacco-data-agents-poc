-- 数据质检 POC 演示库 schema（与 mock_api.py 的查询对齐）
CREATE TABLE dimension_map (en TEXT, cn TEXT, short_cn TEXT, definition TEXT, source TEXT);
CREATE TABLE period_dim (period_id TEXT, period_label TEXT, period_cn TEXT, date_start TEXT, date_end TEXT, is_current TEXT);
CREATE TABLE meta (key TEXT, value TEXT);
CREATE TABLE caliber (key TEXT, name TEXT, definition TEXT, formula TEXT, note TEXT);
CREATE TABLE feature_rule_map (id INTEGER, data_feature TEXT, dimension TEXT, template_name TEXT,
    rule_share TEXT, strong_share TEXT, suggested_threshold TEXT, sample_rules TEXT, note TEXT);
CREATE TABLE handling_rule (id INTEGER, dimension TEXT, symptom TEXT, handling TEXT, grade TEXT,
    grade_cn TEXT, owner_role TEXT, evidence_count TEXT, note TEXT);
CREATE TABLE quality_rule (rule_id TEXT, rule_name TEXT, rule_desc TEXT, strength TEXT, strength_cn TEXT,
    dimension TEXT, dimension_cn TEXT, template_id TEXT, template_name TEXT, template_type TEXT,
    table_id TEXT, table_name TEXT, table_description TEXT, data_domain TEXT, biz_unit_name TEXT,
    project_name TEXT, validate_object_type TEXT, validate_object_name TEXT, validate_field TEXT,
    threshold_metric TEXT, threshold_expr TEXT, rule_status TEXT, creator_name TEXT,
    gmt_create TEXT, gmt_modified TEXT);
CREATE TABLE rule_exec (exec_id TEXT, rule_id TEXT, rule_name TEXT, biz_date TEXT, ds TEXT,
    task_status TEXT, is_pass TEXT, start_time TEXT, end_time TEXT, duration_sec TEXT,
    dimension TEXT, dimension_cn TEXT, table_name TEXT, table_description TEXT, data_domain TEXT);
CREATE TABLE problem (problem_object_id TEXT, validate_object_type TEXT, problem_object_name TEXT,
    validate_object_name TEXT, table_id TEXT, table_name TEXT, table_desc TEXT, data_domain TEXT,
    rule_id TEXT, rule_name TEXT, rule_strength TEXT, dimension TEXT, dimension_cn TEXT,
    problem_status TEXT, problem_desc TEXT, submit_time TEXT, submitter TEXT, owner_names TEXT,
    validate_scope TEXT, validate_result TEXT, ds INTEGER);
CREATE TABLE problem_ticket (ticket_no INTEGER, domain_l1 TEXT, domain_l2 TEXT, table_cn TEXT,
    table_en TEXT, problem_desc TEXT, submitter TEXT, submit_date TEXT, status TEXT, owner TEXT,
    category TEXT, dimension TEXT, cause_solution TEXT, plan_date TEXT, actual_date TEXT,
    lead_time_days INTEGER, remark TEXT, priority TEXT);
CREATE TABLE requirement (req_no INTEGER, req_type TEXT, domain TEXT, req_desc TEXT, biz_caliber TEXT,
    tech_caliber TEXT, priority TEXT, expect_date TEXT, submitter TEXT, submit_date TEXT,
    communicated TEXT, status TEXT, handler TEXT, todo TEXT, conclusion TEXT, plan_date TEXT,
    actual_date TEXT, lead_time_days INTEGER, remark TEXT);
CREATE TABLE lineage (table_name TEXT, upstream TEXT, downstream TEXT, note TEXT);
