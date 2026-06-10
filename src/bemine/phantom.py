from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .capabilities import _parse_adb_devices
from .config import config_string


DEFAULT_PROJECT_ROOT = Path("/home/cdjk/phantom/packages/android/phantom-service")
DEFAULT_APPLICATION_ID = "dev.phantom.service"
DEFAULT_SERVICE_CLASS = ".PhantomAccessibilityService"
DEFAULT_DEBUG_APK = Path("app/build/outputs/apk/debug/app-debug.apk")
DEFAULT_JAVA_HOME_CANDIDATES = (
    Path("/usr/lib/jvm/java-17-openjdk-amd64"),
    Path("/usr/lib/jvm/java-21-openjdk-amd64"),
    Path("/usr/lib/jvm/default-java"),
)
DEFAULT_ANDROID_HOME_CANDIDATES = (
    Path.home() / "Android" / "Sdk",
    Path.home() / "android-sdk",
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


def phantom_status(
    *,
    project_root: str | Path | None = None,
    serial: str = "",
    java_home: str | Path | None = None,
    runner: Runner = subprocess.run,
    timeout: int = 8,
) -> dict[str, Any]:
    root = resolve_project_root(project_root)
    app_id = resolve_application_id(root)
    service_component = resolve_service_component(root, app_id)
    apk = select_apk(root)
    adb = adb_capability(runner=runner, timeout=timeout)
    selected_device, device_blockers = select_adb_device(adb, serial=serial)
    build = build_readiness(root, java_home=java_home)

    blockers: list[str] = []
    next_actions: list[str] = []
    if not root.exists():
        blockers.append(f"Phantom project root does not exist: {root}")
        next_actions.append("Set [phantom].project_root to the Android service project.")
    if not (root / "gradlew").exists():
        blockers.append(f"Gradle wrapper is missing: {root / 'gradlew'}")
    if apk is None:
        blockers.append("No Phantom APK was found under the Android service project.")
        next_actions.append("Run `bemine phantom build` after the Java/Gradle toolchain is ready.")
        if not build["java_compiler"]:
            blockers.append("No usable Java compiler was found for Gradle.")

    blockers.extend(device_blockers)
    if not adb.get("available"):
        next_actions.append("Install Android platform-tools so `adb` is available.")
    elif device_blockers:
        next_actions.append("Unlock the phone and accept the Android USB debugging prompt.")

    installed: bool | None = None
    package_path = ""
    accessibility_enabled: bool | None = None
    accessibility_services = ""
    if selected_device:
        package = adb_shell(
            adb,
            ["pm", "path", app_id],
            serial=selected_device["serial"],
            runner=runner,
            timeout=timeout,
        )
        package_path = package["stdout"].strip()
        installed = package["returncode"] == 0 and bool(package_path)
        accessibility = adb_shell(
            adb,
            ["settings", "get", "secure", "enabled_accessibility_services"],
            serial=selected_device["serial"],
            runner=runner,
            timeout=timeout,
        )
        if accessibility["returncode"] == 0:
            accessibility_services = accessibility["stdout"].strip()
            accessibility_enabled = accessibility_service_enabled(
                accessibility_services,
                service_component=service_component,
                app_id=app_id,
            )

    install_ready = apk is not None and selected_device is not None and root.exists()
    if installed and accessibility_enabled is False:
        next_actions.append("Enable Phantom Service in Android Accessibility settings.")
    elif install_ready and not installed:
        next_actions.append("Run `bemine phantom install` to install the APK on the authorized phone.")

    status_label = "blocked"
    if installed:
        status_label = "installed"
    elif install_ready:
        status_label = "ready"

    return {
        "schema": "bemine.phantom.v1",
        "ok": True,
        "status": status_label,
        "project_root": str(root),
        "project_root_exists": root.exists(),
        "application_id": app_id,
        "accessibility_service": service_component,
        "apk": apk_payload(apk),
        "adb": adb,
        "selected_device": selected_device,
        "install_ready": install_ready,
        "installed": installed,
        "package_path": package_path,
        "accessibility_enabled": accessibility_enabled,
        "accessibility_services": accessibility_services,
        "build": build,
        "blockers": blockers,
        "next_actions": dedupe(next_actions),
    }


def phantom_build(
    *,
    project_root: str | Path | None = None,
    java_home: str | Path | None = None,
    runner: Runner = subprocess.run,
    timeout: int = 600,
) -> dict[str, Any]:
    root = resolve_project_root(project_root)
    build = build_readiness(root, java_home=java_home)
    blockers: list[str] = []
    if not root.exists():
        blockers.append(f"Phantom project root does not exist: {root}")
    if not build["gradlew_exists"]:
        blockers.append(f"Gradle wrapper is missing: {build['gradlew']}")
    if not build["java_compiler"]:
        blockers.append("No usable Java compiler was found for Gradle.")
    if not build["android_sdk"]:
        blockers.append("No usable Android SDK was found for Gradle.")
    if blockers:
        return {
            "ok": False,
            "status": "blocked",
            "project_root": str(root),
            "build": build,
            "blockers": blockers,
        }

    env = os.environ.copy()
    if build["java_home"]:
        env["JAVA_HOME"] = build["java_home"]
        env["PATH"] = f"{Path(build['java_home']) / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    if build["android_home"]:
        env["ANDROID_HOME"] = build["android_home"]
        env["ANDROID_SDK_ROOT"] = build["android_home"]

    command = ["./gradlew", ":app:assembleDebug"]
    proc = runner(
        command,
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    apk = select_apk(root)
    ok = proc.returncode == 0 and apk is not None
    return {
        "ok": ok,
        "status": "built" if ok else "failed",
        "project_root": str(root),
        "command": command,
        "returncode": proc.returncode,
        "stdout": tail(proc.stdout),
        "stderr": tail(proc.stderr),
        "apk": apk_payload(apk),
        "build": build,
        "blockers": [] if ok else ["Gradle build failed or produced no APK."],
    }


def phantom_install(
    *,
    project_root: str | Path | None = None,
    serial: str = "",
    build: bool = False,
    dry_run: bool = False,
    java_home: str | Path | None = None,
    runner: Runner = subprocess.run,
    timeout: int = 600,
) -> dict[str, Any]:
    build_result: dict[str, Any] | None = None
    root = resolve_project_root(project_root)
    apk = select_apk(root)
    if apk is None and build:
        build_result = phantom_build(
            project_root=root,
            java_home=java_home,
            runner=runner,
            timeout=timeout,
        )
        if not build_result["ok"]:
            return {
                "ok": False,
                "status": "blocked",
                "dry_run": dry_run,
                "build": build_result,
                "blockers": build_result.get("blockers", []),
            }
        apk = select_apk(root)

    preflight = phantom_status(
        project_root=root,
        serial=serial,
        java_home=java_home,
        runner=runner,
        timeout=min(timeout, 30),
    )
    blockers = list(preflight.get("blockers") or [])
    if apk is None and "No Phantom APK was found under the Android service project." not in blockers:
        blockers.append("No Phantom APK was found under the Android service project.")
    if blockers:
        return {
            "ok": False,
            "status": "blocked",
            "dry_run": dry_run,
            "build": build_result,
            "preflight": preflight,
            "blockers": blockers,
        }

    selected = preflight.get("selected_device") or {}
    adb = preflight.get("adb") or {}
    adb_path = str(adb.get("path") or "adb")
    device_serial = str(selected.get("serial") or "")
    if not device_serial:
        return {
            "ok": False,
            "status": "blocked",
            "dry_run": dry_run,
            "build": build_result,
            "preflight": preflight,
            "blockers": ["No authorized Android device was selected."],
        }

    command = [adb_path, "-s", device_serial, "install", "-r", str(apk)]
    if dry_run:
        return {
            "ok": True,
            "status": "dry_run",
            "dry_run": True,
            "build": build_result,
            "preflight": preflight,
            "command": command,
            "apk": apk_payload(apk),
            "device": selected,
            "blockers": [],
        }

    proc = runner(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    ok = proc.returncode == 0 and "Success" in f"{proc.stdout}\n{proc.stderr}"
    postflight = None
    if ok:
        postflight = phantom_status(
            project_root=root,
            serial=device_serial,
            java_home=java_home,
            runner=runner,
            timeout=min(timeout, 30),
        )
        ok = bool(postflight.get("installed"))
    return {
        "ok": ok,
        "status": "installed" if ok else "failed",
        "dry_run": False,
        "build": build_result,
        "preflight": preflight,
        "postflight": postflight,
        "command": command,
        "apk": apk_payload(apk),
        "device": selected,
        "install": {
            "returncode": proc.returncode,
            "stdout": tail(proc.stdout),
            "stderr": tail(proc.stderr),
        },
        "blockers": [] if ok else ["ADB install failed or package verification did not pass."],
    }


def render_phantom_status(status: Mapping[str, Any]) -> str:
    lines = [f"Phantom phone workflow: {status.get('status', 'unknown')}"]
    lines.append(f"Root: {status.get('project_root')}")
    apk = status.get("apk") or {}
    lines.append(f"APK: {apk.get('path') if apk.get('exists') else 'missing'}")
    adb = status.get("adb") or {}
    adb_status = adb.get("status") or "unknown"
    adb_path = adb.get("path") or "adb"
    lines.append(f"ADB: {adb_status} ({adb_path})")
    devices = adb.get("devices") or []
    if devices:
        for device in devices:
            lines.append(f"Device: {device.get('serial')} state={device.get('state')}")
    selected = status.get("selected_device") or {}
    if selected:
        lines.append(f"Selected: {selected.get('serial')}")
    installed = status.get("installed")
    if installed is not None:
        lines.append(f"Installed: {installed}")
    accessibility = status.get("accessibility_enabled")
    if accessibility is not None:
        lines.append(f"Accessibility enabled: {accessibility}")
    blockers = status.get("blockers") or []
    if blockers:
        lines.append("Blockers:")
        lines.extend(f"- {blocker}" for blocker in blockers)
    next_actions = status.get("next_actions") or []
    if next_actions:
        lines.append("Next:")
        lines.extend(f"- {action}" for action in next_actions)
    return "\n".join(lines) + "\n"


def render_phantom_build(result: Mapping[str, Any]) -> str:
    lines = [f"Phantom build: {result.get('status', 'unknown')}"]
    apk = result.get("apk") or {}
    if apk.get("exists"):
        lines.append(f"APK: {apk.get('path')}")
    if result.get("returncode") is not None:
        lines.append(f"Return code: {result.get('returncode')}")
    blockers = result.get("blockers") or []
    if blockers:
        lines.append("Blockers:")
        lines.extend(f"- {blocker}" for blocker in blockers)
    stderr = str(result.get("stderr") or "").strip()
    if stderr:
        lines.append("Stderr tail:")
        lines.append(stderr)
    return "\n".join(lines) + "\n"


def render_phantom_install(result: Mapping[str, Any]) -> str:
    lines = [f"Phantom install: {result.get('status', 'unknown')}"]
    device = result.get("device") or {}
    if device:
        lines.append(f"Device: {device.get('serial')}")
    apk = result.get("apk") or {}
    if apk.get("exists"):
        lines.append(f"APK: {apk.get('path')}")
    command = result.get("command") or []
    if command:
        lines.append("Command: " + " ".join(str(part) for part in command))
    blockers = result.get("blockers") or []
    if blockers:
        lines.append("Blockers:")
        lines.extend(f"- {blocker}" for blocker in blockers)
    install = result.get("install") or {}
    if install:
        lines.append(f"Return code: {install.get('returncode')}")
        stderr = str(install.get("stderr") or "").strip()
        if stderr:
            lines.append("Stderr tail:")
            lines.append(stderr)
    return "\n".join(lines) + "\n"


def resolve_project_root(project_root: str | Path | None = None) -> Path:
    if project_root:
        return Path(project_root).expanduser().resolve()
    raw = os.environ.get("BEMINE_PHANTOM_ROOT", "").strip()
    raw = raw or config_string("phantom", "project_root", "")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_PROJECT_ROOT


def resolve_application_id(root: Path) -> str:
    configured = config_string("phantom", "application_id", "").strip()
    if configured:
        return configured
    gradle = root / "app" / "build.gradle.kts"
    if gradle.exists():
        match = re.search(
            r'applicationId\s*=\s*"([^"]+)"',
            gradle.read_text(encoding="utf-8", errors="ignore"),
        )
        if match:
            return match.group(1)
    return DEFAULT_APPLICATION_ID


def resolve_service_component(root: Path, app_id: str) -> str:
    configured = config_string("phantom", "accessibility_service", "").strip()
    raw_service = configured or service_name_from_manifest(root) or DEFAULT_SERVICE_CLASS
    if "/" in raw_service:
        return raw_service
    if raw_service.startswith("."):
        return f"{app_id}/{app_id}{raw_service}"
    return f"{app_id}/{raw_service}"


def service_name_from_manifest(root: Path) -> str:
    manifest = root / "app" / "src" / "main" / "AndroidManifest.xml"
    if not manifest.exists():
        return ""
    try:
        tree = ET.parse(manifest)
    except ET.ParseError:
        return ""
    namespace = "{http://schemas.android.com/apk/res/android}"
    for service in tree.findall(".//service"):
        name = str(service.attrib.get(f"{namespace}name") or "").strip()
        if not name:
            continue
        for action in service.findall("./intent-filter/action"):
            if action.attrib.get(f"{namespace}name") == "android.accessibilityservice.AccessibilityService":
                return name
    return ""


def select_apk(root: Path) -> Path | None:
    preferred = root / DEFAULT_DEBUG_APK
    if preferred.exists():
        return preferred
    if not root.exists():
        return None
    candidates = [path for path in root.glob("**/*.apk") if path.is_file()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def apk_payload(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"exists": False, "path": ""}
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
    }


def adb_capability(*, runner: Runner = subprocess.run, timeout: int = 8) -> dict[str, Any]:
    path = shutil.which("adb")
    if not path:
        return {"available": False, "status": "not_installed", "devices": []}
    result: dict[str, Any] = {
        "available": True,
        "path": path,
        "status": "unknown",
        "devices": [],
    }
    try:
        proc = runner(
            [path, "devices", "-l"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        return result
    result["devices"] = _parse_adb_devices(proc.stdout)
    if proc.returncode != 0:
        result["status"] = "error"
        result["error"] = (proc.stderr or proc.stdout).strip()
    elif result["devices"]:
        states = {device["state"] for device in result["devices"]}
        result["status"] = "authorized" if states == {"device"} else "attention_required"
    else:
        result["status"] = "no_devices"
    return result


def select_adb_device(
    adb: Mapping[str, Any],
    *,
    serial: str = "",
) -> tuple[dict[str, str] | None, list[str]]:
    blockers: list[str] = []
    if not adb.get("available"):
        return None, ["ADB is not installed or not on PATH."]
    devices = [
        {"serial": str(device.get("serial") or ""), "state": str(device.get("state") or "")}
        for device in adb.get("devices") or []
    ]
    if not devices:
        return None, ["No Android devices are attached over ADB."]
    if serial:
        matches = [device for device in devices if device["serial"] == serial]
        if not matches:
            return None, [f"Requested Android device is not attached: {serial}"]
        device = matches[0]
        if device["state"] != "device":
            return None, [f"Android device {serial} is {device['state']}; authorize USB debugging."]
        return device, []
    authorized = [device for device in devices if device["state"] == "device"]
    if authorized:
        return authorized[0], []
    for device in devices:
        blockers.append(
            f"Android device {device['serial']} is {device['state']}; authorize USB debugging."
        )
    return None, blockers


def adb_shell(
    adb: Mapping[str, Any],
    args: Sequence[str],
    *,
    serial: str,
    runner: Runner = subprocess.run,
    timeout: int = 8,
) -> dict[str, Any]:
    command = [str(adb.get("path") or "adb"), "-s", serial, "shell", *args]
    proc = runner(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def build_readiness(
    root: Path,
    *,
    java_home: str | Path | None = None,
) -> dict[str, Any]:
    resolved_java_home = resolve_java_home(java_home)
    android_home = resolve_android_home()
    javac = Path(resolved_java_home) / "bin" / "javac" if resolved_java_home else None
    return {
        "gradlew": str(root / "gradlew"),
        "gradlew_exists": (root / "gradlew").exists(),
        "java_home": str(resolved_java_home) if resolved_java_home else "",
        "java_compiler": bool(javac and javac.exists()),
        "javac": str(javac) if javac else "",
        "android_home": str(android_home) if android_home else "",
        "android_sdk": bool(android_home),
    }


def resolve_java_home(java_home: str | Path | None = None) -> Path | None:
    if java_home:
        return Path(java_home).expanduser().resolve()
    configured = config_string("phantom", "java_home", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    env_java_home = os.environ.get("JAVA_HOME", "").strip()
    candidates = []
    if env_java_home:
        candidates.append(Path(env_java_home))
    candidates.extend(DEFAULT_JAVA_HOME_CANDIDATES)
    javac = shutil.which("javac")
    if javac:
        candidates.append(Path(javac).resolve().parent.parent)
    for candidate in candidates:
        if (candidate.expanduser() / "bin" / "javac").exists():
            return candidate.expanduser().resolve()
    return None


def resolve_android_home() -> Path | None:
    configured = config_string("phantom", "android_home", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.exists() else None
    candidates: list[Path] = []
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidates.append(Path(raw))
    candidates.extend(DEFAULT_ANDROID_HOME_CANDIDATES)
    for candidate in candidates:
        path = candidate.expanduser()
        if path.exists():
            return path.resolve()
    return None


def accessibility_service_enabled(
    enabled_services: str,
    *,
    service_component: str,
    app_id: str,
) -> bool:
    normalized = {part.strip() for part in enabled_services.split(":") if part.strip()}
    short = service_component.replace(f"{app_id}/{app_id}.", f"{app_id}/.")
    return service_component in normalized or short in normalized


def tail(text: str, *, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
