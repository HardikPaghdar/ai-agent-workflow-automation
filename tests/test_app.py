from fastapi.testclient import TestClient
from app.main import app, workflows, safe_calculate

client = TestClient(app)

def setup_function():
    workflows.clear()

def test_safe_calculator():
    assert safe_calculate("(2 + 3) * 4") == 20

def test_calculator_rejects_code():
    try:
        safe_calculate("__import__('os').system('echo bad')")
        assert False, "expression should be rejected"
    except (ValueError, TypeError):
        pass

def test_plan_and_approval_gate():
    plan = client.post("/workflows/plan", json={"goal": "Look up maintenance checklist and draft an email summary"})
    assert plan.status_code == 200
    workflow = plan.json()
    assert workflow["requires_approval"] is True
    run = client.post(f"/workflows/{workflow['id']}/execute")
    assert run.status_code == 403
    approved = client.post(f"/workflows/{workflow['id']}/approve")
    assert approved.status_code == 200
    executed = client.post(f"/workflows/{workflow['id']}/execute")
    assert executed.status_code == 200
    assert executed.json()["status"] == "completed"

def test_unknown_workflow_is_404():
    assert client.get("/workflows/not-a-real-id").status_code == 404
