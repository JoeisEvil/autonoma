from pathlib import Path

from . import commands, hooks, schemas


def register(ctx):
    runtime = hooks.Runtime(ctx)
    for name, callback in runtime.hook_map().items():
        ctx.register_hook(name, callback)
    ctx.register_tool(
        name="autonoma_status",
        toolset="autonoma",
        schema=schemas.AUTONOMA_STATUS,
        handler=runtime.status_tool,
        description=schemas.AUTONOMA_STATUS["description"],
        emoji="🛡️",
    )
    commands.register(ctx, runtime)
    ctx.register_skill("autonoma", Path(__file__).parent / "skills" / "autonoma" / "SKILL.md")
