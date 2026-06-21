"""Stash a line range of the primary's messages.jsonl into a blob, replacing it with a one-line
summary pointer. Used to heal context rot — toxic/repetitive stretches get collapsed out of the
active context without losing the data."""
import json
import pathlib

NAME = "PRUNE"
DESCRIPTION = (
    "Collapse a 1-indexed line range [start, end] of the primary's messages.jsonl into a single "
    "stashed pointer with an LLM summary. The original lines are archived to a blob; the primary "
    "can READ_FILE the blob to recover them. Use this to heal context rot — repetitive or toxic "
    "stretches that are bloating the primary's active context."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "start": {"type": "integer", "description": "1-indexed start line (inclusive)"},
        "end": {"type": "integer", "description": "1-indexed end line (inclusive)"},
    },
    "required": ["start", "end"],
}

def run(args):
    import agent
    start, end = int(args["start"]), int(args["end"])
    if start < 1 or end < start:
        return "error: start must be >= 1 and end >= start"
    spec = {"message": f"STASH_MESSAGE: {start} {end}", "next": 0}
    tdir = pathlib.Path(agent.AGENT_DIR).resolve().parent / "agent" / "triggers"
    tdir.mkdir(parents=True, exist_ok=True)
    name = "subconscious-stash"
    (tdir / f"{name}.json").write_text(json.dumps(spec))
    return f"created agent/triggers/{name}.json — will stash lines {start}-{end} on next tick"
