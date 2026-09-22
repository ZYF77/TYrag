"""Offline structure checks for New2 v2.4."""
import json
from pathlib import Path

path = Path(__file__).with_name("New2_v2.4_evidence_scope.json")
d = json.loads(path.read_text(encoding="utf-8"))
c = d["components"]
nodes = {n["id"]: n for n in d["graph"]["nodes"]}
assert "Agent:Draft" not in c and "Agent:Draft" not in nodes
assert "Agent:Research" not in c
assert "Agent:AnswerResearch" in c and "Agent:AnswerResearch" in nodes
ar_node = nodes["Agent:AnswerResearch"]
assert ar_node.get("type") == "agentNode", ar_node.get("type")
assert ar_node.get("data", {}).get("name"), "AnswerResearch missing data.name"

assert "Switch:Coverage" not in c

# PlanLite fields
plan_out = c["Agent:Plan"]["obj"]["params"]["outputs"]["structured"]
req = set(plan_out["required"])
assert req == {
    "route",
    "resolved_question",
    "scope_mode",
    "target_equipment_ids",
    "focused_query",
    "broad_query",
    "clarification_question",
    "boundary_response",
}
assert c["Agent:Plan"]["obj"]["params"]["max_tokens"] <= 900

ar = c["Agent:AnswerResearch"]["obj"]["params"]
assert ar["max_rounds"] == 2
assert ("禁止调用任何工具" in ar["sys_prompt"]) or ("不要调用工具" in ar["sys_prompt"])
assert "禁止" in ar["sys_prompt"] and "Target" in ar["sys_prompt"]
assert "不能迁到目标对象" in ar["sys_prompt"] or "不能迁" in ar["sys_prompt"]
tools = ar.get("tools") or []
assert len(tools) >= 3
for t in tools:
    assert t["params"]["top_n"] == 4

for key in ("Retrieval:Target", "Retrieval:Scope"):
    assert c[key]["obj"]["params"]["top_n"] == 8

# wiring: Seed -> AnswerResearch -> Switch:ResearchOutcome
assert "Agent:AnswerResearch" in c["VariableAggregator:Seed"]["downstream"]
assert "Switch:ResearchOutcome" in c["Agent:AnswerResearch"]["downstream"]

# graph consistency
for edge in d["graph"]["edges"]:
    assert edge["source"] in nodes and edge["target"] in nodes
for key, node in c.items():
    if key not in nodes:
        # allow orphan message components
        if not key.startswith("Message:"):
            # begin etc must be in graph
            if key in ("begin", "Agent:Plan", "Agent:AnswerResearch", "VariableAggregator:Seed"):
                assert key in nodes, key
        continue
    assert nodes[key]["data"]["form"] == node["obj"]["params"], key


# Message refs must not point at removed agents
blob = json.dumps(d, ensure_ascii=False)
assert "Agent:Draft@" not in blob
assert "Agent:Research@" not in blob
assert "{Agent:AnswerResearch@structured.answer}" in c["Message:Direct"]["obj"]["params"]["content"][0]
assert "{Agent:AnswerResearch@structured.answer}" in c["Message:ResearchNoEvidence"]["obj"]["params"]["content"][0]


# Edges must carry ReactFlow handles like v2.3 or import/runtime may drop Seed→AnswerResearch
for e in d["graph"]["edges"]:
    assert "sourceHandle" in e and "targetHandle" in e, e
seed_ar = [e for e in d["graph"]["edges"] if e["source"]=="VariableAggregator:Seed" and e["target"]=="Agent:AnswerResearch"]
assert seed_ar and seed_ar[0].get("sourceHandle")=="start" and seed_ar[0].get("targetHandle")=="end"


# Answer-quality gates (Sim S1/S2)
ar_sys = c["Agent:AnswerResearch"]["obj"]["params"]["sys_prompt"]
assert "够证快路径" in ar_sys or "禁止调用任何工具" in ar_sys
assert "evidence_ids` 必须非空" in ar_sys or "evidence_ids 必须非空" in ar_sys or "evidence_ids 必须" in ar_sys
eid_desc = c["Agent:AnswerResearch"]["obj"]["params"]["outputs"]["structured"]["properties"]["evidence_ids"]["description"]
assert "B" in eid_desc or "相关" in eid_desc

print("PASS: New2 v2.4 structure checks")
