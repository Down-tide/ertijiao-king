#!/bin/zsh
# 构建：编译、打包、验证、签名，结果放在 dist/：
#   dist/二踢脚大王.app
#   dist/ErTiJiaoDaWang-<版本>-macOS-arm64.zip
# 不会安装，也不会关闭正在运行的应用。
set -euo pipefail
setopt null_glob
cd "$(dirname "$0")"
TRAPZERR() {
  echo ""
  echo "构建失败，没有改动任何已安装的应用。请把上面的报错贴出来。"
  echo "本次构建的临时文件保存在：${WORK:-未创建}"
  return 1
}

PET_APP="二踢脚大王.app"
PET_EXE="二踢脚大王"
DIST="dist"
ARCH="$(uname -m)"
SRC_RUNTIME="runtime/python"
PYBIN="$SRC_RUNTIME/bin/python3"

echo "0/5 检查 Python…"
# 构建用的 Python 放在 runtime/python，会整份打进应用（开发者自备，见 README「从源码构建」）。
if [[ ! -x "$PYBIN" ]]; then
  echo "缺少 runtime/python/bin/python3。可以运行 python3 scripts/fetch_deps.py 下载一份，或自己放一份 Python 3.12。"
  false
fi
PYVER="$("$PYBIN" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
  3.9|3.12) ;;
  *) echo "runtime/python 是 Python $PYVER，不受支持（zstandard 只有 3.9 和 3.12 的版本）。"
     false ;;
esac
echo "   Python $PYVER：$PYBIN"

WORK="$(mktemp -d)"
PET_BUILD="$WORK/$PET_APP"
RES="$PET_BUILD/Contents/Resources"
mkdir -p "$PET_BUILD/Contents/MacOS" "$RES/assets"

echo "1/5 编译…"
swiftc -O -parse-as-library -swift-version 5 -target "$ARCH-apple-macos13.0" \
  PetApp.swift -o "$PET_BUILD/Contents/MacOS/$PET_EXE" -framework Cocoa -framework WebKit

echo "2/5 打包…"
cp Info.plist "$PET_BUILD/Contents/Info.plist"
cp index.html collector.py ssh_remote.py "$RES/"
cp -R vendor "$RES/vendor"
"$PYBIN" -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import zstandard' "$RES/vendor" \
  || { echo "runtime/python 无法加载 vendor 里的 zstandard，多半是版本不对（要 3.9 或 3.12）。"; false; }
cp public/assets/pet-*.png "$RES/assets/"
cp -R public/assets/logos "$RES/assets/"
cp public/assets/AppIcon.icns "$RES/"
# 整份运行时打进应用：桌宠从 Resources/python/runtime/bin/python3 启动采集器。
mkdir -p "$RES/python"
cp -RL "$SRC_RUNTIME" "$RES/python/runtime"
cp runtime/PROVENANCE.json "$RES/python/" 2>/dev/null || true
# 采集器只用标准库里的基础模块：去掉界面库、包管理器、头文件这些用不到的部分，
# zip 小一些。只删应用里的这份，runtime/python 本身不动。去掉之后下面会真的跑一遍采集器。
APP_PY="$RES/python/runtime"
# 只删本次临时构建目录里的东西：路径不对就停，绝不碰别处。
[[ -n "$WORK" && "$APP_PY" == "$WORK"/* && -d "$APP_PY/lib" ]] || { echo "内部错误：精简目录不对（$APP_PY）"; false; }
for junk in "$APP_PY"/lib/python3*/{idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data,site-packages} \
            "$APP_PY"/lib/python3*/config-* "$APP_PY"/lib/python3*/lib-dynload/_tkinter.* \
            "$APP_PY"/lib/{tcl,tk,itcl,thread}* "$APP_PY"/lib/libtcl* "$APP_PY"/lib/pkgconfig \
            "$APP_PY"/include "$APP_PY"/share \
            "$APP_PY"/bin/{pip,idle,2to3,pydoc}* "$APP_PY"/bin/python*-config; do
  if [[ "$junk" == "$APP_PY"/* ]]; then rm -rf -- "$junk"; fi
done
"$PYBIN" -I -B -c 'from pathlib import Path; import shutil,sys; [shutil.rmtree(p) for p in Path(sys.argv[1]).rglob("__pycache__")]' "$PET_BUILD"
# 下载来的运行时可能带隔离标记，执行前先清掉，免得被系统拦下。
xattr -cr "$PET_BUILD"

echo "3/5 验证…"
# 用应用里那份（已精简的）Python 跑，确认去掉的东西没有影响采集器。
PY="$APP_PY/bin/python3"
"$PY" -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import zstandard; print("  解压组件可用", zstandard.__version__)' "$RES/vendor"
"$PY" -I -B "$RES/collector.py" --once --no-ssh --state-dir "$WORK/verify" > /dev/null
"$PY" -I -B "$RES/ssh_remote.py" build-check 0 1 > /dev/null
# 发布包不能带任何具体机器：默认配置里必须是空的。
"$PY" -I -B "$RES/collector.py" --dump-config --state-dir "$WORK/cfg" > "$WORK/default-config.json"
"$PY" -I -B -c 'import json,sys; c=json.load(open(sys.argv[1])); assert c["remote"]["hosts"] == [], "默认配置不能带服务器"' "$WORK/default-config.json"
# 运行时写出的缓存会破坏签名，签名前必须没有。
if CACHES="$(find "$PET_BUILD" -name __pycache__ -print)"; then
  if [[ -n "$CACHES" ]]; then
    echo "包内出现了 Python 缓存，会破坏签名："
    print -r -- "$CACHES"
    false
  fi
fi
codesign --force --deep --sign "${AI_PET_SIGNING_IDENTITY:--}" "$PET_BUILD"
codesign --verify --deep --strict "$PET_BUILD"
# 应用标识和数据目录不能是开发版本使用的那一套，否则会共用设置和任务状态。
BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$PET_BUILD/Contents/Info.plist")"
DATA_DIR="$(/usr/libexec/PlistBuddy -c 'Print :PetDataDirectory' "$PET_BUILD/Contents/Info.plist" 2>/dev/null || true)"
if [[ "$BUNDLE_ID" == local.ai-pet* || -z "$DATA_DIR" || "$DATA_DIR" == "AI桌宠" ]]; then
  echo "Info.plist 的应用标识或数据目录无效（$BUNDLE_ID / ${DATA_DIR:-未设置}）。"
  false
fi
echo "   应用标识 $BUNDLE_ID，数据目录 ~/Library/Application Support/$DATA_DIR"

echo "4/5 放进 dist/ …"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PET_BUILD/Contents/Info.plist")"
# 压缩包名只用英文字母、数字和符号，便于在各处下载和分发。
ZIP="ErTiJiaoDaWang-$VERSION-macOS-$ARCH.zip"
rm -rf -- "$DIST/$PET_APP" "$DIST"/ErTiJiaoDaWang-*.zip
mkdir -p "$DIST"
# ditto 复制，保留签名所需的扩展属性和包内的相对链接。
ditto "$PET_BUILD" "$DIST/$PET_APP"
ditto -c -k --keepParent "$DIST/$PET_APP" "$DIST/$ZIP"
"$PYBIN" -I -B -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' "$WORK"

echo "5/5 完成"
echo "  应用：$PWD/$DIST/$PET_APP（$(du -sh "$DIST/$PET_APP" | cut -f1)）"
echo "  压缩包：$PWD/$DIST/$ZIP（$(du -sh "$DIST/$ZIP" | cut -f1)）"
echo "安装方法见 README「安装」。"
