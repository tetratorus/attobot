"""Nudge: fire a one-off message into the primary's stream."""
import json
import pathlib

NAME = "NUDGE"
DESCRIPTION = "Fire a one-off `[trigger <name>] <message>` into the primary's stream next tick, then delete. Surfaces to the operator's Telegram."
PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "trigger filename (subconscious- prefix added automatically)"},
        "message": {"type": "string", "description": "text fired into the primary's stream"},
    },
    "required": ["name", "message"],
}

def run(args):
    import agent
    name = args["name"]
    if "/" in name or ".." in name:
        return "error: name must not contain '/' or '..'"
    if not name.startswith("subconscious-"):
        name = "subconscious-" + name
    spec = {"message": args["message"], "next": 0}
    primary = pathlib.Path(agent.CFG.get("primary_dir", pathlib.Path(agent.AGENT_DIR).resolve().parent / "agent"))
    tdir = primary / "triggers"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{name}.json").write_text(json.dumps(spec))
    return f"created {tdir / f'{name}.json'}"
