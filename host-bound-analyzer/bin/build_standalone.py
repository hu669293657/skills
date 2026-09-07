# -*- coding: utf-8 -*-
"""host-bound-analyzer 单文件构建器：把 host_bound 整包内嵌为一个可执行脚本。

用法（在项目根目录执行）：
    python bin/build_standalone.py            # 生成 bin/host_bound_standalone.py
    python bin/build_standalone.py --selftest # 生成后立即冒烟验证 version 子命令

产物 bin/host_bound_standalone.py：
  - 仅依赖 Python >= 3.6 标准库；拷贝到任意 Linux 训练机即可执行
  - 首次运行时把内嵌 zip 解包到临时目录并注册 atexit 清理（不留痕）
  - 与 `python -m host_bound` 完全同一套逻辑（collect/analyze/report/run/run-collect/validate/version）
  - 额外支持 `--unpack <dir>`：把内嵌包解到指定目录（供现场微调 rules/*.yaml 后
    以 `PYTHONPATH=<dir> python -m host_bound ...` 方式运行；不清理，需手工删除）
  - 每次运行自校验：内嵌数据 sha256 与声明值不一致即拒绝启动

重新构建时机：修改了 host_bound/ 下任何源码或 rules/*.yaml 后必须重跑本脚本。
"""

import base64
import hashlib
import io
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(ROOT, "host_bound")
OUT_PATH = os.path.join(ROOT, "bin", "host_bound_standalone.py")

PAYLOAD_TOKEN = "@@PAYLOAD_LINES@@"
SHA256_TOKEN = "@@PAYLOAD_SHA256@@"

SKIP_DIRS = ("__pycache__",)
SKIP_EXTS = (".pyc", ".pyo")

LAUNCHER_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""host-bound-analyzer 单文件版（自解压启动器，由 bin/build_standalone.py 生成，勿手改）。

用法与 `python -m host_bound` 完全一致，例如：
    python3 host_bound_standalone.py collect --auto-detect --duration 60 --sanitize
    python3 host_bound_standalone.py run-collect --auto-detect -o ./hb_out --sanitize
    python3 host_bound_standalone.py run host_bound_collection_xxx.tar.gz -o report.html
    python3 host_bound_standalone.py version
    python3 host_bound_standalone.py --unpack ./hb_pkg   # 解出内嵌包（可选，调试用）
"""

import base64
import hashlib
import io
import os
import shutil
import sys
import tempfile
import zipfile
import atexit

PAYLOAD_SHA256 = "@@PAYLOAD_SHA256@@"

PAYLOAD_B64 = (
@@PAYLOAD_LINES@@
)


def _fail(msg):
    sys.stderr.write("host-bound 单文件启动器错误: %s\\n" % msg)
    sys.exit(1)


def _unpack_to(app_root):
    """把内嵌 zip 解包到 app_root 并返回校验信息。"""
    try:
        payload = base64.b64decode("".join(PAYLOAD_B64))
    except Exception as exc:  # 数据被截断/篡改
        _fail("内嵌数据解码失败: %r" % (exc,))
    digest = hashlib.sha256(payload).hexdigest()
    if digest != PAYLOAD_SHA256:
        _fail("内嵌数据校验失败（期望 %s，实际 %s）；文件可能被改动或传输出错，"
              "请重新分发" % (PAYLOAD_SHA256, digest))
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            zf.extractall(app_root)
    except zipfile.BadZipFile as exc:
        _fail("内嵌包损坏: %r" % (exc,))
    if not os.path.isdir(os.path.join(app_root, "host_bound")):
        _fail("内嵌包结构异常（缺少 host_bound/ 目录）")
    return payload


def _bootstrap():
    if sys.version_info < (3, 6):
        _fail("需要 Python >= 3.6，当前为 %s；请改用 python3 或升级 Python"
              % sys.version.split()[0])
    argv = sys.argv[1:]
    if argv and argv[0] == "--unpack":
        dest = os.path.abspath(argv[1]) if len(argv) > 1 else os.getcwd()
        _unpack_to(dest)
        print("内嵌包已解压: %s" % dest)
        print("现场微调 rules/*.yaml 后可运行: "
              "PYTHONPATH=%s python -m host_bound <command>" % dest)
        sys.exit(0)
    app_root = tempfile.mkdtemp(prefix="host_bound_app_")
    atexit.register(shutil.rmtree, app_root, ignore_errors=True)
    _unpack_to(app_root)
    sys.path.insert(0, app_root)
    return app_root


def main():
    _bootstrap()
    from host_bound.cli import main as cli_main
    rc = cli_main()
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
'''


def collect_files():
    """收集包内全部源码与规则 YAML（排除 __pycache__/*.pyc）。"""
    files = []
    for dirpath, dirnames, filenames in os.walk(PKG_DIR):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if name.endswith(SKIP_EXTS):
                continue
            full = os.path.join(dirpath, name)
            arc = "host_bound/" + os.path.relpath(full, PKG_DIR).replace("\\", "/")
            files.append((full, arc))
    return files


def build():
    files = collect_files()
    if not files:
        raise SystemExit("未找到任何包文件，请确认在项目根目录运行")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, arc in files:
            zf.write(full, arc)
    payload = buf.getvalue()
    sha = hashlib.sha256(payload).hexdigest()

    lines = [base64.b64encode(payload[i:i + 57]).decode("ascii")
             for i in range(0, len(payload), 57)]
    joined = ",\n".join('    "%s"' % ln for ln in lines)

    text = LAUNCHER_TEMPLATE
    text = text.replace("@@PAYLOAD_LINES@@", joined, 1)
    text = text.replace(PAYLOAD_TOKEN, sha, 1)
    text = text.replace(SHA256_TOKEN, sha, 1)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    src_kb = sum(os.path.getsize(p) for p, _ in files) / 1024.0
    print("构建完成: %s" % os.path.relpath(OUT_PATH, ROOT))
    print("  内嵌文件: %d 个（源码 %.1f KB）" % (len(files), src_kb))
    print("  内嵌包:   %.1f KB (deflate) -> 脚本总大小 %.1f KB"
          % (len(payload) / 1024.0, os.path.getsize(OUT_PATH) / 1024.0))
    print("  sha256:   %s" % sha)
    return OUT_PATH


def selftest(script):
    print("冒烟验证: %s version" % os.path.relpath(script, ROOT))
    r = subprocess.run([sys.executable, script, "version"],
                       capture_output=True, text=True)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or "host-bound" not in out:
        raise SystemExit("自检失败: rc=%s\nstdout=%s\nstderr=%s"
                         % (r.returncode, r.stdout, r.stderr))
    print("  %s" % out)
    print("自检通过")


if __name__ == "__main__":
    path = build()
    if "--selftest" in sys.argv:
        selftest(path)
