from __future__ import annotations

import json

from ..db import BemineDB


class CredentialProxy:
    def __init__(self, db: BemineDB) -> None:
        self.db = db

    def check(self, request_id: str, *, actor: str) -> dict:
        request = self.db.get_permission_request(request_id)
        if request is None:
            raise ValueError(f"unknown permission request: {request_id}")
        if request["status"] != "approved":
            raise PermissionError(f"permission request is {request['status']}")

        result = {
            "ok": True,
            "request_id": request_id,
            "credential_id": request["credential_id"],
            "credential_name": request["credential_name"],
            "credential_kind": request["credential_kind"],
            "proxy": "bemine",
            "mode": "brokered_operations_only",
        }
        self.db.record_audit_event(
            category="credential_proxy",
            event_type="credential_proxy_authorized",
            actor=actor,
            device_id=request["device_id"],
            project_id=request["project_id"],
            session_id=request["session_id"],
            payload_json=json.dumps(result, sort_keys=True),
        )
        return result
