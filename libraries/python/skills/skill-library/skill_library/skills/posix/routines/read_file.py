from typing import Any, cast

from skill_library import AskUserFn, EmitFn, RunContext, RunRoutineFn
from skill_library.skills.posix import PosixSkill


async def main(
    context: RunContext,
    routine_state: dict[str, Any],
    emit: EmitFn,
    run: RunRoutineFn,
    ask_user: AskUserFn,
    filename: str,
) -> str:
    """
    Read the contents of a file.
    """
    posix_skill = cast(PosixSkill, context.skills["posix"])
    shell = posix_skill.shell
    return shell.read_file(filename)
