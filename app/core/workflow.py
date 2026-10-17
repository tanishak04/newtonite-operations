from typing import Dict, List, Set
from app.models import Status, RoleEnum

# Allowed workflow state transitions
# Open -> In Progress -> Awaiting Approval -> Resolved -> Closed
# Also allows reopening from Resolved -> In Progress
WORKFLOW_TRANSITIONS: Dict[Status, Set[Status]] = {
    Status.OPEN: {Status.IN_PROGRESS},
    Status.IN_PROGRESS: {Status.AWAITING_APPROVAL, Status.RESOLVED},
    Status.AWAITING_APPROVAL: {Status.RESOLVED, Status.IN_PROGRESS},  # Lead can approve or reject back
    Status.RESOLVED: {Status.CLOSED, Status.IN_PROGRESS},            # Close or reopen
    Status.CLOSED: set(),                                            # Terminal
}

# Strict role gates for specific state transitions
# Approval (AWAITING_APPROVAL -> RESOLVED) and terminal (RESOLVED -> CLOSED) require LEAD or higher
TRANSITION_ROLE_REQUIREMENTS: Dict[tuple[Status, Status], RoleEnum] = {
    (Status.AWAITING_APPROVAL, Status.RESOLVED): RoleEnum.LEAD,
    (Status.RESOLVED, Status.CLOSED): RoleEnum.LEAD,
}

def validate_transition(current_status: Status, target_status: Status, user_role: RoleEnum) -> tuple[bool, str]:
    if current_status == target_status:
        return False, f"Item is already in status {current_status.value}"

    allowed_targets = WORKFLOW_TRANSITIONS.get(current_status, set())
    if target_status not in allowed_targets:
        allowed_str = ", ".join([s.value for s in allowed_targets]) if allowed_targets else "None (Terminal State)"
        return False, f"Invalid state transition from {current_status.value} to {target_status.value}. Allowed next states: [{allowed_str}]"

    required_role = TRANSITION_ROLE_REQUIREMENTS.get((current_status, target_status), RoleEnum.MEMBER)
    from app.core.dependencies import ROLE_HIERARCHY
    if ROLE_HIERARCHY.get(user_role, 0) < ROLE_HIERARCHY.get(required_role, 0):
        return False, f"Insufficient permissions: transition from {current_status.value} to {target_status.value} requires {required_role.value} role, you are {user_role.value}"

    return True, ""
