"""把现有项目与里程碑资料（fixtures/seed.json）录入事件化后端。

录入动作同样通过命令产生事件，不直接写状态：
- rules：主管部门发布的支持规则（含历史版本与生效日期）；
- program 记录：登记为项目聚合；
- milestone 记录：作为项目里程碑模板存入登记事件，
  并提供 build_stage_payloads() 供申报阶段引用，阶段承诺不被后续覆盖。
"""

from __future__ import annotations

from typing import Any

from .eventstore import Actor
from .services import CulturalFundService


def ingest_seed(service: CulturalFundService, authority: Actor, seed: dict[str, Any]) -> dict[str, Any]:
    """按 seed 录入规则与项目；返回录入结果摘要。"""
    result: dict[str, Any] = {"rules": [], "projects": [], "milestones": {}}

    for rule in seed.get("rules", []):
        events = service.publish_rule(
            authority,
            code=rule["code"],
            name=rule["name"],
            version=rule["version"],
            effective_date=rule["effective_date"],
            support_categories=rule["support_categories"],
            stage_cap=rule["stage_cap"],
            subsidy_rate=rule["subsidy_rate"],
            required_metrics=rule["required_metrics"],
            metric_dims=rule["metric_dims"],
            traffic_metric_key=rule.get("traffic_metric_key", "views"),
            notes=rule.get("notes", ""),
        )
        result["rules"].append(events[0].aggregate_id)

    milestones_by_program: dict[str, list[dict[str, Any]]] = {}
    for record in seed.get("records", []):
        kind = record.get("kind")
        if kind == "milestone":
            milestones_by_program.setdefault(record["program_id"], []).append(record)

    for record in seed.get("records", []):
        if record.get("kind") != "program":
            continue
        project_id = record["id"]
        milestones = [
            {
                "milestone_id": m["id"],
                "name": m.get("name", m["id"]),
                "evidence_types": m.get("evidence_types", []),
                "commitment": m.get("commitment", {}),
                "planned_amount": m.get("planned_amount", "0.00"),
                "due_date": m.get("due_date", ""),
            }
            for m in milestones_by_program.get(project_id, [])
        ]
        events = service.register_project(
            authority,
            project_id=project_id,
            name=record.get("name", project_id),
            region=record.get("region") or (record.get("regions") or ["未分区"])[0],
            applicant_org_id=record.get("applicant_org_id", f"org-{project_id}"),
            targets=record.get("targets", []),
            funding_tranches=record.get("funding_tranches", len(milestones) or 1),
            registered_date=record.get("registered_date"),
            milestones=milestones,
        )
        result["projects"].append(project_id)
        result["milestones"][project_id] = milestones

    return result


def build_stage_payloads(seed: dict[str, Any], program_id: str) -> list[dict[str, Any]]:
    """从 seed 里程碑构造申报阶段负载（stage_id 取里程碑 id）。"""
    payloads = []
    for record in seed.get("records", []):
        if record.get("kind") == "milestone" and record.get("program_id") == program_id:
            payloads.append(
                {
                    "stage_id": record["id"],
                    "name": record.get("name", record["id"]),
                    "planned_amount": record.get("planned_amount", "0.00"),
                    "due_date": record.get("due_date", ""),
                    "commitment": record.get("commitment", {}),
                }
            )
    return payloads
