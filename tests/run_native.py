#!/usr/bin/env python3
"""构建并运行隔离的原生桌宠检查，不触碰正式应用状态。"""
from pathlib import Path
import os
import plistlib
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
tmp = Path(tempfile.mkdtemp(prefix="ai-pet-native-"))
binary = tmp / "二踢脚大王-native-tests"
app = tmp / "二踢脚大王-native-tests.app"
resources = app / "Contents" / "Resources"
(app / "Contents" / "MacOS").mkdir(parents=True)
resources.mkdir(parents=True)

subprocess.run([
    "swiftc", "-D", "PET_TESTS", "-parse-as-library", "-swift-version", "5",
    "PetApp.swift", "-o", str(binary), "-framework", "Cocoa", "-framework", "WebKit",
], cwd=ROOT, check=True)
shutil.copy2(binary, app / "Contents" / "MacOS" / "二踢脚大王")
plist = plistlib.loads((ROOT / "Info.plist").read_bytes())
plist["CFBundleIdentifier"] = "local.ai-pet.native-tests"
(app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))
shutil.copy2(ROOT / "index.html", resources / "index.html")
shutil.copytree(ROOT / "public" / "assets", resources / "assets")

env = os.environ.copy()
env["PET_TEST_DIR"] = str(tmp)
result = subprocess.run([str(app / "Contents" / "MacOS" / "二踢脚大王")], env=env, text=True, capture_output=True, timeout=30)
print(result.stdout, end="")
print(result.stderr, end="")
if result.returncode != 0:
    raise SystemExit(result.returncode)
assert (tmp / "native-result.txt").exists(), "验证未生成结果"
print((tmp / "native-result.txt").read_text())
print(f"artifacts: {tmp}")
