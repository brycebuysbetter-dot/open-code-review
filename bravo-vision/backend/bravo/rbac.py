from bravo.enums import ApprovalKind, Role

P = {
    "leads:read", "leads:write", "conversations:read", "conversations:write",
    "tasks:read", "tasks:write", "deals:analyze", "offers:write", "buyers:read",
    "buyers:write", "contracts:write", "activity:read", "agents:run",
    "approvals:read", "users:manage", "campaigns:write",
}

_ALL_APPROVE = {f"approve:{k.value}" for k in ApprovalKind}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.ADMIN: P | _ALL_APPROVE,
    Role.MANAGER: (P - {"users:manage"})
    | {f"approve:{k.value}" for k in (ApprovalKind.MESSAGE, ApprovalKind.CAMPAIGN, ApprovalKind.OFFER, ApprovalKind.BUYER_BLAST, ApprovalKind.CALL)},
    Role.ACQUISITIONS: {
        "leads:read", "leads:write", "conversations:read", "conversations:write",
        "tasks:read", "tasks:write", "deals:analyze", "offers:write", "activity:read",
        "agents:run", "approvals:read", "approve:message", "approve:call",
    },
    Role.DISPOSITIONS: {
        "leads:read", "buyers:read", "buyers:write", "tasks:read", "tasks:write",
        "deals:analyze", "agents:run", "approvals:read", "approve:buyer_blast",
    },
    Role.VIEWER: {"leads:read", "conversations:read", "tasks:read", "buyers:read", "approvals:read", "activity:read"},
}

# Kinds where the requester may never be the approver, even if their role allows approving.
NO_SELF_APPROVAL = {ApprovalKind.OFFER, ApprovalKind.CONTRACT, ApprovalKind.PAYMENT, ApprovalKind.DESTRUCTIVE}


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())
