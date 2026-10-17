"""Operator-installed runtime protocol under the original native supervisor.

AgentOps' CAO delivery launcher prepares its terminal, then consumes the existing
ready/final gate before sending input. No command comes from a task or profile.
"""
import json
import math
import os
from pathlib import Path
import time


def validate_commands(policy):
    commands = policy.get('worker_commands', {})
    if not isinstance(commands, dict) or len(commands) > 64:
        raise RuntimeError('installed worker command mapping required')
    if commands and not policy.get('admission_command'):
        raise RuntimeError('external workers require protected admission gates')
    from hermes_cli.kanban_execution_authority import protected_path
    for profile, command in commands.items():
        if (not isinstance(profile, str) or not profile or profile in {'.', '..'}
                or '/' in profile or '\\' in profile
                or not isinstance(command, list) or not 1 <= len(command) <= 16
                or not all(isinstance(arg, str) and arg and '\0' not in arg for arg in command)
                or not Path(command[0]).is_absolute()):
            raise RuntimeError('trusted absolute worker command required')
        for arg in command:
            if Path(arg).is_absolute():
                protected_path(arg)


def execute(command, request, ready_fd, final_fd, deadline):
    if (os.geteuid() == 0 or not command or request is None or ready_fd is None
            or final_fd is None or deadline is None or not math.isfinite(deadline)
            or time.time() >= deadline):
        raise RuntimeError('protected external worker gates required')
    from hermes_cli.kanban_execution_authority import environment_fd
    # Reseal after workspace resolution; no prompt enters argv or exec env.
    fd = environment_fd({'request': request})
    try:
        for descriptor in (fd, ready_fd, final_fd):
            os.set_inheritable(descriptor, True)
        os.execvpe(command[0], [*command, '--request-fd', str(fd), '--ready-fd', str(ready_fd),
                               '--final-gate-fd', str(final_fd), '--deadline', str(deadline)], os.environ)
    finally:
        os.close(fd)


def read_request(fd):
    from hermes_cli.kanban_execution_authority import read_environment
    packet = read_environment(fd)
    if set(packet) != {'request'}:
        raise RuntimeError('sealed external task request required')
    request = json.loads(packet['request'])
    if not isinstance(request, dict) or not isinstance(request.get('task'), dict):
        raise RuntimeError('sealed original task required')
    return request


def input_gate(ready_fd, final_fd, deadline):
    """Call once after runtime readiness and immediately before original input."""
    try:
        if not math.isfinite(deadline) or time.time() >= deadline:
            return False
        os.write(ready_fd, b'1')
        os.close(ready_fd)
        ready_fd = None
        granted = os.read(final_fd, 1) == b'1'
        return granted and time.time() < deadline
    finally:
        if ready_fd is not None: os.close(ready_fd)
        os.close(final_fd)
