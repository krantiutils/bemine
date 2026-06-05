from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import config_string
from .db import BemineDB
from .totp import verify_totp


@dataclass(frozen=True)
class CredentialProfile:
    credential_id: str
    name: str
    kind: str
    storage_ref: str
    approval_policy: str
    totp_required: bool
    metadata: dict


class CredentialInventory:
    def __init__(
        self, db: BemineDB, *, home: Path | None = None, device_id: str = ""
    ) -> None:
        self.db = db
        self.home = (home or Path.home()).expanduser().resolve()
        self.device_id = device_id

    def scan(self) -> list[CredentialProfile]:
        profiles = self._baseline_profiles()
        profiles.extend(self._detected_profiles())
        for profile in profiles:
            self.db.upsert_credential_profile(
                credential_id=profile.credential_id,
                name=profile.name,
                kind=profile.kind,
                storage_ref=profile.storage_ref,
                approval_policy=profile.approval_policy,
                totp_required=profile.totp_required,
            )
            self.db.record_audit_event(
                category="credential",
                event_type="credential_profile_scanned",
                actor="credential_inventory",
                device_id=self.device_id or None,
                payload_json=json.dumps(
                    {
                        "credential_id": profile.credential_id,
                        "name": profile.name,
                        "kind": profile.kind,
                        "storage_ref": profile.storage_ref,
                        "approval_policy": profile.approval_policy,
                        "totp_required": profile.totp_required,
                        "metadata": profile.metadata,
                    },
                    sort_keys=True,
                ),
            )
        return profiles

    def _baseline_profiles(self) -> list[CredentialProfile]:
        return [
            CredentialProfile(
                credential_id="github:personal",
                name="GitHub personal account",
                kind="shared_identity",
                storage_ref="external://github/personal",
                approval_policy="baseline_shared",
                totp_required=False,
                metadata={"declared": True},
            ),
            CredentialProfile(
                credential_id="github:ampixa",
                name="GitHub org access: ampixa",
                kind="shared_identity_org",
                storage_ref="external://github/ampixa",
                approval_policy="baseline_shared",
                totp_required=False,
                metadata={"declared": True, "org": "ampixa"},
            ),
            CredentialProfile(
                credential_id="codeberg:primary",
                name="Codeberg primary account",
                kind="shared_identity",
                storage_ref="external://codeberg/primary",
                approval_policy="baseline_shared",
                totp_required=False,
                metadata={"declared": True},
            ),
            CredentialProfile(
                credential_id="huggingface:ampixa",
                name="Hugging Face org access: ampixa",
                kind="shared_identity_org",
                storage_ref="external://huggingface/ampixa",
                approval_policy="baseline_shared",
                totp_required=False,
                metadata={"declared": True, "org": "ampixa"},
            ),
            CredentialProfile(
                credential_id="signing:git",
                name="Git signing layer",
                kind="shared_identity",
                storage_ref="external://signing/git",
                approval_policy="baseline_shared",
                totp_required=False,
                metadata={"declared": True},
            ),
            CredentialProfile(
                credential_id="fmw:cdjk-mbp",
                name="Legacy ~/.fmw credential proxy on cdjk-mbp",
                kind="legacy_secret_bundle",
                storage_ref="proxy://credentials/fmw/cdjk-mbp",
                approval_policy="discord_totp",
                totp_required=True,
                metadata={"declared": True, "device": "cdjk-mbp", "proxy_only": True},
            ),
            CredentialProfile(
                credential_id="aws:any",
                name="AWS credentials",
                kind="cost_incurring",
                storage_ref="external://aws",
                approval_policy="discord_totp",
                totp_required=True,
                metadata={"declared": True},
            ),
            CredentialProfile(
                credential_id="google:any",
                name="Google API credentials",
                kind="cost_incurring",
                storage_ref="external://google",
                approval_policy="discord_totp",
                totp_required=True,
                metadata={"declared": True},
            ),
        ]

    def _detected_profiles(self) -> list[CredentialProfile]:
        profiles: list[CredentialProfile] = []
        fmw = self.home / ".fmw"
        if fmw.exists():
            profiles.append(
                CredentialProfile(
                    credential_id="fmw:legacy",
                    name="Legacy ~/.fmw credential bundle",
                    kind="legacy_secret_bundle",
                    storage_ref=f"file://{fmw}",
                    approval_policy="discord_totp",
                    totp_required=True,
                    metadata=_path_metadata(fmw),
                )
            )
        aws = self.home / ".aws"
        if aws.exists():
            profiles.append(
                CredentialProfile(
                    credential_id=f"aws:device:{self.device_id or 'local'}",
                    name=f"AWS credentials on {self.device_id or 'local'}",
                    kind="cost_incurring",
                    storage_ref=f"file://{aws}",
                    approval_policy="discord_totp",
                    totp_required=True,
                    metadata=_path_metadata(aws) | {"files": _child_names(aws)},
                )
            )
        gh = self.home / ".config" / "gh" / "hosts.yml"
        if gh.exists():
            profiles.append(
                CredentialProfile(
                    credential_id=f"github:device:{self.device_id or 'local'}",
                    name=f"GitHub CLI auth on {self.device_id or 'local'}",
                    kind="shared_identity",
                    storage_ref=f"file://{gh}",
                    approval_policy="baseline_shared",
                    totp_required=False,
                    metadata=_path_metadata(gh),
                )
            )
        hf = self.home / ".cache" / "huggingface" / "token"
        if hf.exists():
            profiles.append(
                CredentialProfile(
                    credential_id=f"huggingface:device:{self.device_id or 'local'}",
                    name=f"Hugging Face token on {self.device_id or 'local'}",
                    kind="shared_identity_org",
                    storage_ref=f"file://{hf}",
                    approval_policy="baseline_shared",
                    totp_required=False,
                    metadata=_path_metadata(hf),
                )
            )
        return profiles


class PermissionGate:
    def __init__(self, db: BemineDB, *, totp_secret: str | None = None) -> None:
        self.db = db
        secret_env = config_string("security", "totp_secret_env", "BEMINE_TOTP_SECRET")
        self.totp_secret = totp_secret or os.environ.get(secret_env, "")

    def request(
        self,
        *,
        credential_id: str,
        reason: str,
        project_id: str | None = None,
        session_id: str | None = None,
        device_id: str | None = None,
        runtime: str | None = None,
    ) -> str:
        profile = self.db.get_credential_profile(credential_id)
        if profile is None:
            raise ValueError(f"unknown credential profile: {credential_id}")
        request_id = f"perm_{uuid.uuid4().hex[:12]}"
        self.db.create_permission_request(
            request_id=request_id,
            credential_id=credential_id,
            reason=reason,
            project_id=project_id,
            session_id=session_id,
            device_id=device_id,
            runtime=runtime,
        )
        self.db.record_audit_event(
            category="permission",
            event_type="permission_requested",
            actor="permission_gate",
            device_id=device_id,
            project_id=project_id,
            session_id=session_id,
            payload_json=json.dumps(
                {
                    "request_id": request_id,
                    "credential_id": credential_id,
                    "reason": reason,
                    "runtime": runtime,
                },
                sort_keys=True,
            ),
        )
        return request_id

    def approve(self, request_id: str, *, actor: str, totp_code: str = "") -> None:
        request = self.db.get_permission_request(request_id)
        if request is None:
            raise ValueError(f"unknown permission request: {request_id}")
        if request["status"] != "pending":
            raise ValueError(f"permission request is already {request['status']}")
        if int(request["totp_required"] or 0):
            if not self.totp_secret:
                raise ValueError("BEMINE_TOTP_SECRET is required for this approval")
            if not verify_totp(self.totp_secret, totp_code):
                raise ValueError("invalid TOTP code")
        self.db.resolve_permission_request(
            request_id=request_id,
            status="approved",
            resolved_by=actor,
        )
        self.db.record_audit_event(
            category="permission",
            event_type="permission_approved",
            actor=actor,
            device_id=request["device_id"],
            project_id=request["project_id"],
            session_id=request["session_id"],
            payload_json=json.dumps(
                {"request_id": request_id, "credential_id": request["credential_id"]},
                sort_keys=True,
            ),
        )

    def deny(self, request_id: str, *, actor: str) -> None:
        request = self.db.get_permission_request(request_id)
        if request is None:
            raise ValueError(f"unknown permission request: {request_id}")
        if request["status"] != "pending":
            raise ValueError(f"permission request is already {request['status']}")
        self.db.resolve_permission_request(
            request_id=request_id,
            status="denied",
            resolved_by=actor,
        )
        self.db.record_audit_event(
            category="permission",
            event_type="permission_denied",
            actor=actor,
            device_id=request["device_id"],
            project_id=request["project_id"],
            session_id=request["session_id"],
            payload_json=json.dumps(
                {"request_id": request_id, "credential_id": request["credential_id"]},
                sort_keys=True,
            ),
        )


def _path_metadata(path: Path) -> dict:
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    return {
        "exists": True,
        "is_dir": path.is_dir(),
        "mode_octal": oct(mode),
        "size": st.st_size,
        "world_readable": bool(mode & stat.S_IROTH),
        "group_readable": bool(mode & stat.S_IRGRP),
        "mtime": int(st.st_mtime),
    }


def _child_names(path: Path) -> list[str]:
    if not path.is_dir():
        return []
    return sorted(p.name for p in path.iterdir() if p.is_file())
