"""Merge the main line with the Plan A v4 line.

main (0036-0045: studies, rules, graph views, decision packages, Workshop
apps, ontology queries, derived projections, process models) and Plan A v4
(0036-0095: decision cases, Action runtime, mapping review and apply runs,
pipeline manifests and schedules, service principals, Agent sessions,
temporal stream hardening) create disjoint tables, so the merge carries no
DDL.
"""
revision = "0096_merge_main_plan_a_v4"
down_revision = ("0045_process_models", "0095_agent_definitions")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
