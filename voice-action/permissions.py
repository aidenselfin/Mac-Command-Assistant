import subprocess
from pathlib import Path


def check_permissions() -> dict[str, bool]:
    return {
        "microphone": _check_microphone(),
        "full_disk": _check_full_disk(),
        "accessibility": _check_accessibility(),
    }


def _check_microphone() -> bool:
    try:
        script = """
import AVFoundation
status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_("soun")
print(status == 3)
"""
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == "True"
    except Exception:
        return False


def _check_full_disk() -> bool:
    test_path = Path.home() / "Library" / "Application Support" / ".voice-action-test"
    try:
        test_path.touch()
        test_path.unlink()
        return True
    except (PermissionError, OSError):
        return False


def _check_accessibility() -> bool:
    try:
        script = """
from ApplicationServices import AXIsProcessTrusted
print(AXIsProcessTrusted())
"""
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == "True"
    except Exception:
        return False
