from __future__ import annotations

import subprocess
from pathlib import Path

from bemine.phantom import (
    phantom_build,
    phantom_install,
    phantom_status,
)


def _project(tmp_path: Path, *, apk: bool = True) -> Path:
    root = tmp_path / "phantom-service"
    app = root / "app"
    (app / "src" / "main").mkdir(parents=True)
    (app / "build" / "outputs" / "apk" / "debug").mkdir(parents=True)
    (root / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    (app / "build.gradle.kts").write_text(
        'android { namespace = "dev.phantom.service"; defaultConfig { applicationId = "dev.phantom.service" } }\n',
        encoding="utf-8",
    )
    (app / "src" / "main" / "AndroidManifest.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
  <application>
    <service android:name=".PhantomAccessibilityService"
      android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE">
      <intent-filter>
        <action android:name="android.accessibilityservice.AccessibilityService" />
      </intent-filter>
    </service>
  </application>
</manifest>
""",
        encoding="utf-8",
    )
    if apk:
        (app / "build" / "outputs" / "apk" / "debug" / "app-debug.apk").write_bytes(b"apk")
    return root


def _java_home(tmp_path: Path) -> Path:
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "javac").write_text("#!/bin/sh\n", encoding="utf-8")
    return home


def _android_home(tmp_path: Path) -> Path:
    home = tmp_path / "android-sdk"
    home.mkdir()
    return home


class Runner:
    def __init__(self, *, devices: str, installed: bool = False, accessibility: str = ""):
        self.devices = devices
        self.installed = installed
        self.accessibility = accessibility
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        command = [str(part) for part in args]
        self.calls.append((command, kwargs))
        if command[-2:] == ["devices", "-l"]:
            return subprocess.CompletedProcess(command, 0, self.devices, "")
        if "install" in command:
            self.installed = True
            return subprocess.CompletedProcess(command, 0, "Success\n", "")
        if command[-3:] == ["pm", "path", "dev.phantom.service"]:
            if self.installed:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "package:/data/app/dev.phantom.service/base.apk\n",
                    "",
                )
            return subprocess.CompletedProcess(command, 1, "", "package not found\n")
        if command[-4:] == [
            "settings",
            "get",
            "secure",
            "enabled_accessibility_services",
        ]:
            return subprocess.CompletedProcess(command, 0, self.accessibility, "")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_phantom_status_reports_unauthorized_phone(tmp_path, monkeypatch):
    root = _project(tmp_path)
    java_home = _java_home(tmp_path)
    monkeypatch.setattr("bemine.phantom.shutil.which", lambda name: "/usr/bin/adb")
    runner = Runner(devices="List of devices attached\nRZCTC0M17SJ unauthorized usb:1\n")

    status = phantom_status(project_root=root, java_home=java_home, runner=runner)

    assert status["status"] == "blocked"
    assert status["apk"]["exists"] is True
    assert status["adb"]["status"] == "attention_required"
    assert status["selected_device"] is None
    assert status["install_ready"] is False
    assert any("unauthorized" in blocker for blocker in status["blockers"])


def test_phantom_status_verifies_installed_accessibility_service(tmp_path, monkeypatch):
    root = _project(tmp_path)
    java_home = _java_home(tmp_path)
    monkeypatch.setattr("bemine.phantom.shutil.which", lambda name: "/usr/bin/adb")
    runner = Runner(
        devices="List of devices attached\nphone device product:sdk\n",
        installed=True,
        accessibility="dev.phantom.service/dev.phantom.service.PhantomAccessibilityService\n",
    )

    status = phantom_status(project_root=root, java_home=java_home, runner=runner)

    assert status["status"] == "installed"
    assert status["selected_device"]["serial"] == "phone"
    assert status["installed"] is True
    assert status["accessibility_enabled"] is True


def test_phantom_install_refuses_unauthorized_phone(tmp_path, monkeypatch):
    root = _project(tmp_path)
    java_home = _java_home(tmp_path)
    monkeypatch.setattr("bemine.phantom.shutil.which", lambda name: "/usr/bin/adb")
    runner = Runner(devices="List of devices attached\nphone unauthorized usb:1\n")

    result = phantom_install(project_root=root, java_home=java_home, runner=runner)

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert any("unauthorized" in blocker for blocker in result["blockers"])
    assert not any("install" in command for command, _kwargs in runner.calls)


def test_phantom_install_dry_run_returns_adb_command(tmp_path, monkeypatch):
    root = _project(tmp_path)
    java_home = _java_home(tmp_path)
    monkeypatch.setattr("bemine.phantom.shutil.which", lambda name: "/usr/bin/adb")
    runner = Runner(devices="List of devices attached\nphone device product:sdk\n")

    result = phantom_install(
        project_root=root,
        java_home=java_home,
        runner=runner,
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["status"] == "dry_run"
    assert result["command"][:4] == ["/usr/bin/adb", "-s", "phone", "install"]
    assert not any(command[0:4] == ["/usr/bin/adb", "-s", "phone", "install"] for command, _ in runner.calls)


def test_phantom_install_builds_missing_apk_then_verifies_install(tmp_path, monkeypatch):
    root = _project(tmp_path, apk=False)
    java_home = _java_home(tmp_path)
    android_home = _android_home(tmp_path)
    monkeypatch.setattr("bemine.phantom.shutil.which", lambda name: "/usr/bin/adb")
    monkeypatch.setattr("bemine.phantom.resolve_android_home", lambda: android_home)

    class BuildRunner(Runner):
        def __call__(self, args, **kwargs):
            command = [str(part) for part in args]
            if command == ["./gradlew", ":app:assembleDebug"]:
                self.calls.append((command, kwargs))
                apk = root / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
                apk.write_bytes(b"built")
                return subprocess.CompletedProcess(command, 0, "built\n", "")
            return super().__call__(args, **kwargs)

    runner = BuildRunner(devices="List of devices attached\nphone device product:sdk\n")

    result = phantom_install(
        project_root=root,
        java_home=java_home,
        runner=runner,
        build=True,
    )

    assert result["ok"] is True
    assert result["status"] == "installed"
    assert result["build"]["ok"] is True
    assert result["postflight"]["installed"] is True
    gradle_call = next(call for call in runner.calls if call[0] == ["./gradlew", ":app:assembleDebug"])
    assert gradle_call[1]["env"]["JAVA_HOME"] == str(java_home)
    assert gradle_call[1]["env"]["ANDROID_HOME"] == str(android_home)
    assert gradle_call[1]["env"]["ANDROID_SDK_ROOT"] == str(android_home)


def test_phantom_build_reports_gradle_failure(tmp_path, monkeypatch):
    root = _project(tmp_path, apk=False)
    java_home = _java_home(tmp_path)
    android_home = _android_home(tmp_path)
    monkeypatch.setattr("bemine.phantom.shutil.which", lambda name: "/usr/bin/adb")
    monkeypatch.setattr("bemine.phantom.resolve_android_home", lambda: android_home)

    def fail_runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "compile failed\n")

    result = phantom_build(project_root=root, java_home=java_home, runner=fail_runner)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "compile failed" in result["stderr"]
