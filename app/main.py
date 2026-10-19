from __future__ import annotations

import ast
import operator
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(title="AI Agent Workflow Automation", version="1.0.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

class PlanRequest(BaseModel):
    goal: str = Field(min_length=5, max_length=1000)

class Workflow:
    def __init__(self, goal: str, steps: list[dict[str, Any]], requires_approval: bool):
        self.id = str(uuid.uuid4())
        self.goal = goal
        self.steps = steps
        self.requires_approval = requires_approval
        self.approved = not requires_approval
        self.status = "awaiting_approval" if requires_approval else "planned"
        self.trace: list[dict[str, Any]] = []
        self.created_at = datetime.now(timezone.utc).isoformat()

workflows: dict[str, Workflow] = {}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.USub: operator.neg}

def safe_calculate(expression: str) -> float | int:
    if len(expression) > 80:
        raise ValueError("Expression is too long.")
    tree = ast.parse(expression, mode="eval")
    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](visit(node.operand))
        raise ValueError("Only numeric arithmetic (+, -, *, /) is allowed.")
    return visit(tree)

TOOLS = {
    "knowledge_lookup": {"risk": "read_only", "description": "Search a tiny built-in sample knowledge base."},
    "calculator": {"risk": "read_only", "description": "Evaluate basic numeric arithmetic safely."},
    "draft_email": {"risk": "draft_only", "description": "Create an email draft without sending it."},
}

def plan_goal(goal: str) -> tuple[list[dict[str, Any]], bool]:
    text = goal.lower()
    steps: list[dict[str, Any]] = []
    if any(word in text for word in ["maintenance", "restart", "checklist", "manual", "knowledge", "look up", "lookup"]):
        steps.append({"tool": "knowledge_lookup", "input": {"query": goal}, "status": "pending"})
    match = re.search(r"(?:calculate|compute)s+([0-9+-*/(). ]{1,80})", goal, re.I)
    if match:
        steps.append({"tool": "calculator", "input": {"expression": match.group(1).strip()}, "status": "pending"})
    if any(word in text for word in ["email", "draft", "summarize", "summary"]):
        steps.append({"tool": "draft_email", "input": {"subject": "Workflow summary", "body": f"Summary requested for: {goal}"}, "status": "pending"})
    if not steps:
        steps.append({"tool": "knowledge_lookup", "input": {"query": goal}, "status": "pending"})
    # Drafting is non-sending; approval is still required for workflows involving a draft or multi-step action.
    requires_approval = len(steps) > 1 or any(step["tool"] == "draft_email" for step in steps)
    return steps, requires_approval

def run_tool(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if name not in TOOLS:
        raise ValueError("Tool is not allowlisted.")
    if name == "knowledge_lookup":
        query = str(payload.get("query", "")).lower()
        passages = [
            {"source": "maintenance-guide.md", "text": "Maintenance restart checklist: before restarting a machine, notify operators, confirm safe state, follow lockout/tagout, check the emergency stop and fault code. Escalate recurring faults."},
            {"source": "support-faq.md", "text": "Do not request passwords or one-time codes. Escalate security concerns and unresolved outages to a human team."},
        ]
        terms = set(re.findall(r"[a-z0-9]{3,}", query))
        matches = [p for p in passages if terms.intersection(set(re.findall(r"[a-z0-9]{3,}", p["text"].lower())))]
        return {"matches": matches, "count": len(matches)}
    if name == "calculator":
        return {"result": safe_calculate(str(payload.get("expression", "")))}
    if name == "draft_email":
        subject = str(payload.get("subject", "Workflow draft"))[:160]
        body = str(payload.get("body", ""))[:5000]
        return {"draft_only": True, "subject": subject, "body": body, "sent": False}
    raise ValueError("No handler registered for tool.")

def public_state(flow: Workflow) -> dict[str, Any]:
    return {"id": flow.id, "goal": flow.goal, "steps": flow.steps, "requires_approval": flow.requires_approval,
            "approved": flow.approved, "status": flow.status, "trace": flow.trace, "created_at": flow.created_at}

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home():
    with open("app/static/index.html", encoding="utf-8") as file:
        return file.read()

@app.get("/health")
def health():
    return {"status": "ok", "workflows": len(workflows)}

@app.get("/tools")
def list_tools():
    return {"tools": [{"name": name, **meta} for name, meta in TOOLS.items()]}

@app.post("/workflows/plan")
def create_plan(request: PlanRequest):
    steps, approval = plan_goal(request.goal)
    flow = Workflow(request.goal, steps, approval)
    workflows[flow.id] = flow
    flow.trace.append({"event": "plan_created", "at": datetime.now(timezone.utc).isoformat(), "step_count": len(steps)})
    return public_state(flow)

@app.post("/workflows/{run_id}/approve")
def approve(run_id: str):
    flow = workflows.get(run_id)
    if not flow:
        raise HTTPException(404, "Workflow not found.")
    if flow.status != "awaiting_approval":
        raise HTTPException(409, "Workflow is not waiting for approval.")
    flow.approved = True
    flow.status = "planned"
    flow.trace.append({"event": "human_approved", "at": datetime.now(timezone.utc).isoformat()})
    return public_state(flow)

@app.post("/workflows/{run_id}/execute")
def execute(run_id: str):
    flow = workflows.get(run_id)
    if not flow:
        raise HTTPException(404, "Workflow not found.")
    if flow.requires_approval and not flow.approved:
        raise HTTPException(403, "Human approval is required before execution.")
    if flow.status == "completed":
        raise HTTPException(409, "Workflow has already completed.")
    flow.status = "running"
    for step in flow.steps:
        if step["status"] == "completed":
            continue
        started = datetime.now(timezone.utc).isoformat()
        try:
            result = run_tool(step["tool"], step["input"])
            step.update({"status": "completed", "result": result})
            flow.trace.append({"event": "tool_completed", "tool": step["tool"], "at": started})
        except Exception as exc:
            step.update({"status": "failed", "error": str(exc)})
            flow.status = "failed"
            flow.trace.append({"event": "tool_failed", "tool": step["tool"], "error": str(exc), "at": started})
            return public_state(flow)
    flow.status = "completed"
    flow.trace.append({"event": "workflow_completed", "at": datetime.now(timezone.utc).isoformat()})
    return public_state(flow)

@app.get("/workflows/{run_id}")
def get_workflow(run_id: str):
    flow = workflows.get(run_id)
    if not flow:
        raise HTTPException(404, "Workflow not found.")
    return public_state(flow)
