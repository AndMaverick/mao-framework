"""The MAO diagnostic: score an AI system on Model, Action, and Orchestration."""

from __future__ import annotations

QUESTIONS = {
    "model": [
        ("m1", "Every production model has a documented capability profile and risk clearance."),
        ("m2", "Model choice is routed by cost, latency, and risk, not hardcoded."),
        ("m3", "An offline evaluation set gates every model change."),
        ("m4", "A tested fallback exists for when the primary model degrades."),
    ],
    "action": [
        ("a1", "Every tool the system can call has an owner and a risk rating."),
        ("a2", "Permissions are least-privilege per role."),
        ("a3", "Side-effecting actions above a risk threshold require human approval."),
        ("a4", "Actions are reversible, or have a documented compensation path."),
    ],
    "orchestration": [
        ("o1", "Plans are explicit dependency graphs, not implicit prompt chains."),
        ("o2", "Every step output is verified before downstream use."),
        ("o3", "Every run produces a tamper-evident audit trail."),
        ("o4", "Spend and retries are bounded per run."),
    ],
}


def score(answers: dict) -> dict:
    """answers maps question id to bool. Unanswered counts as no."""
    layers = {}
    for layer, questions in QUESTIONS.items():
        yes = sum(1 for qid, _ in questions if answers.get(qid) is True)
        gaps = [text for qid, text in questions if answers.get(qid) is not True]
        layers[layer] = {"score": round(100 * yes / len(questions)), "gaps": gaps}
    weakest = min(layers, key=lambda name: (layers[name]["score"], name))
    overall = round(sum(v["score"] for v in layers.values()) / len(layers))
    return {"overall": overall, "weakest": weakest, "layers": layers}


def report(answers: dict) -> str:
    result = score(answers)
    lines = [f"# MAO diagnostic: {result['overall']}/100", ""]
    for layer, data in result["layers"].items():
        lines.append(f"## {layer.title()}: {data['score']}/100")
        lines.extend(f"- Gap: {gap}" for gap in data["gaps"])
        if not data["gaps"]:
            lines.append("- No gaps found.")
        lines.append("")
    lines.append(f"Start with **{result['weakest']}**. It is the weakest layer, so it is where failure will originate.")
    return "\n".join(lines)
