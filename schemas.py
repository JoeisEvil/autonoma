AUTONOMA_STATUS = {
    "name": "autonoma_status",
    "description": (
        "Show which kinds of actions you have earned the right to do without asking the user "
        "(file writes, deletes, shell, git, network, messaging, spending, system), with trust "
        "scores and thresholds. Call it before planning multi-step work, so you can batch the "
        "actions that will need approval and tell the user up front. Also reports whether an "
        "unfinished earlier session left a handoff."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "class": {
                "type": "string",
                "description": "Optional action class to show, e.g. fs.write or git.remote.",
            },
        },
        "required": [],
    },
}
