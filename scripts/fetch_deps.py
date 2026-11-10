#!/usr/bin/env python3
"""下载构建要用的 Python 3.12（python-build-standalone，按固定 sha256 校验）到 runtime/python。

用法：python3 scripts/fetch_deps.py        （已经有 runtime/python 就跳过）
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PY_RUNTIME = ROOT/'runtime'/'python'
NAME = 'cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz'
URL = ('https://github.com/astral-sh/python-build-standalone/releases/download/20261026/'
       'cpython-3.12.16%2B20261026-aarch64-apple-darwin-install_only_stripped.tar.gz')
SHA256 = '353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e'


def safe_extract(tar, dest):
    """Extract without letting an entry escape `dest` (tar filters are 3.12+ only)."""
    root = Path(dest).resolve()
    for member in tar.getmembers():
        target = (root/member.name).resolve()
        if target != root and root not in target.parents:
            raise SystemExit('压缩包里有可疑路径：%s' % member.name)
        if member.issym() or member.islnk():
            link = (target.parent/member.linkname).resolve()
            if link != root and root not in link.parents:
                raise SystemExit('压缩包里有指向外面的链接：%s' % member.name)
    tar.extractall(dest)


def main():
    if (PY_RUNTIME/'bin'/'python3').exists():
        print('runtime/python 已存在，跳过')
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp)/'python.tar.gz'
        print('下载 Python 3.12（约 30 MB）：%s' % URL)
        # 系统的 curl：支持重试和代理，几十 MB 的文件比 urllib 稳。
        fetched = subprocess.run(['curl', '-fL', '--retry', '3', '--retry-delay', '2',
                                  '--connect-timeout', '20', '-o', str(archive), URL])
        if fetched.returncode != 0:
            print('下载失败。可以用浏览器下载上面的地址，再运行：')
            print('  python3 scripts/fetch_deps.py  （把下载好的文件放到 runtime/%s）' % NAME)
            manual = ROOT/'runtime'/NAME
            if not manual.exists():
                return 1
            shutil.copy(manual, archive)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != SHA256:
            print('校验失败：期望 %s，实际 %s' % (SHA256, actual))
            return 1
        print('校验通过')
        with tarfile.open(archive) as tar:
            safe_extract(tar, tmp)
        extracted = Path(tmp)/'python'
        if not (extracted/'bin'/'python3').exists():
            print('压缩包结构和预期不符（里面没有 python/bin/python3）')
            return 1
        PY_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), str(PY_RUNTIME))
    (ROOT/'runtime'/'PROVENANCE.json').write_text(json.dumps(
        {'name': NAME, 'url': URL, 'sha256': SHA256}, indent=2) + '\n', encoding='utf-8')
    # 下载来的文件带隔离标记，不清掉的话运行时会被系统拦下。
    subprocess.run(['xattr', '-cr', str(PY_RUNTIME)], check=False)
    print('已就绪：%s' % PY_RUNTIME)
    return 0


if __name__ == '__main__':
    sys.exit(main())
