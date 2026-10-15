# coding: utf-8
"""
发行打包：把 `dist/<包名>` 打成可直接分发的 zip。

与上游那版（依赖 7-Zip + 文件清单）的区别：

  - **不依赖 7-Zip**：用标准库 zipfile。构建机上未必装了 7z，
    上游那版找不到 7z.exe 会直接抛异常。
  - **目录联接会被展开成真实文件**：dist 里的 `models/ top core/ LLM/ assets/ docs/`
    是指向源码树的目录联接。Python 的 os.walk **默认就会进去**
    （联接在 Python 眼里不是 symlink，实测确认），所以打进 zip 的是真内容，
    用户解压出来能直接跑 —— 这也是为什么本脚本不需要 7z 的 `-snl` 之类开关。
  - **排除规则重写**：上游那版的条件写成了
    `if 'models' in parts and path.suffix == '.zip' or path.suffix == '.cfg'`，
    按 Python 的优先级实际等价于 `(A and B) or C`，会把 models 下所有 .cfg
    都排除；更糟的是那条"深度 ≥4 不打包"会把实际模型文件（models/引擎/子目录/文件）
    全部排除。这里改成明确的规则。

默认**不打包 models/**（2GB 量级，而且用户要按自己选的引擎下载），
但**打包引擎的 llama.cpp DLL** —— 缺了它程序根本起不来。只需一份
（core/server/engines/llama/bin/，三个引擎共用同一套 DLL，详见 EXCLUDE_PREFIXES 的注释）。

用法：
    python zip_release.py                    # 打包 dist 下所有存在的发行包
    python zip_release.py --with-models      # 连模型一起打（一体包，约 2GB+）
"""

from __future__ import annotations

import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
DIST_DIR = PROJECT_ROOT / 'dist'
RELEASE_DIR = PROJECT_ROOT / 'release'

# dist 下要打包的目录（存在才打）
PACKAGES = [
    ('Speak More', 'SpeakMore'),           # 服务端 + 客户端
    ('Speak More Client', 'SpeakMore-Client'),   # 仅客户端
]

# 整目录排除（相对包根目录）
EXCLUDE_DIRS = {
    'logs',            # 构建机自己的运行日志，不该发给用户
    '__pycache__',
    'release',
    'dist',
}

# 整棵子树排除（相对包根目录的路径前缀）
#
# [补丁] 这里**不能**排除 core/server/engines/llama/ —— 早先的注释写它是
# "历史遗留目录，没有任何代码引用它"，**是错的**，实测（logs/_probe_engine_llama_bind.py）：
# 三个 GGUF 引擎的 `inference/__init__.py` 都写了 `try: from ...llama import llama`，
# 包属性一旦绑上，后面 `from . import llama` 拿到的是**共享那份**（见
# core/server/engines/*/inference/__init__.py）。也就是说源码运行时用的是
# engines/llama/，而不是各引擎自带的那份。
#
# 两边版本还不一样，后果很严重：各引擎自带的 llama.py 是**旧的**（35935 字节），
# 按 b10621 的 DLL 读它会把 llama_context_params 当成 136 字节（真实 160），
# 字段整体错位 —— 实测读出 n_threads=0、rope_freq_base=nan。源码跑共享版没事，
# 而发行包把共享版排掉了 → **发布版跑的就是这份错位的绑定**（v0.1 就是这么发出去的）。
#
# 所以现在的规则反过来：
#   - **发** core/server/engines/llama/（共享绑定 + 一份 DLL）
#   - **不发** 三个引擎各自 inference/bin 里的 DLL 副本 —— 它们只有那份旧绑定
#     会去读（共享绑定读的是自己旁边的 bin），排除掉省 ~190MB
# 三个引擎各自的 DLL 集合与共享那份完全相同，一份够用。
EXCLUDE_PREFIXES = (
    'core/server/engines/qwen_asr_gguf/inference/bin/',
    'core/server/engines/fun_asr_gguf/inference/bin/',
    'core/server/engines/force_aligner_gguf/inference/bin/',
)

# 顶层目录名形如 4 位年份的，是**日记归档**（使用记录 + 录音），
# 属于用户隐私，绝不能打进发行包。
_DIARY_DIR = re.compile(r'^\d{4}$')

# 按名字排除的文件 / 后缀
EXCLUDE_NAMES = {
    'config_local.json',            # 本地配置（含 API Key），每个用户自己生成
    'config_local.json.bak',
    'server_console.log',
    'client_console.log',
    'patch_status.log',
    'gpu_probe.json',
    'config_local_error.log',
}
# 注意：**不要排除 .pyc** —— PyInstaller 的 noarchive 模式把整个运行时以
# 松散的 .pyc 放在 internal/ 里（本包有近 5000 个），少一个都起不来
# （实测：排掉之后 exe 报 "Failed to import encodings module"）。
# 源码树里的缓存垃圾由上面 __pycache__ 那条目录规则挡掉。
#
# [补丁] 顺手挡掉所有 `.bak`：热词表写盘时会生成 `hot.txt.bak`
# （hot_file.py 的原子写），而排除名单里原先只有 config_local.json.bak，
# 于是**热词备份会被打进发行包**（合成目录自测抓到的，logs/_verify_zip_rules.py）。
# 它是构建机上的本地残留，对新用户毫无意义。
EXCLUDE_SUFFIXES = ('.tmp', '.pretest', '.bak')


def should_skip(rel: Path, with_models: bool) -> bool:
    """rel 是相对包根目录的路径。"""
    parts = rel.parts
    if not parts:
        return False

    # 目录级
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return True
    if parts[0] == 'models' and not with_models:
        return True
    if _DIARY_DIR.match(parts[0]):          # 2026/、2027/ … 日记归档
        return True
    posix = '/'.join(parts)
    if any(posix.startswith(pref) for pref in EXCLUDE_PREFIXES):
        return True

    name = parts[-1]
    if name in EXCLUDE_NAMES:
        return True
    if name.startswith('config_local.json.') and name.endswith('.bak'):
        return True
    if name.lower().endswith(EXCLUDE_SUFFIXES):
        return True
    return False


def iter_files(root: Path, with_models: bool):
    """遍历要打包的文件，产出 (绝对路径, zip 内的相对路径)。"""
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if should_skip(rel, with_models):
            continue
        yield path, rel


def warn_if_running(console_print) -> None:
    """
    有实例在跑时说一声。

    打包本身不受影响（Windows 允许读运行中的 exe），但如果用户是**刚改完代码**、
    又忘了先重新打包，zip 里会是旧产物 —— 提醒一句比事后排查划算。
    """
    try:
        import psutil
    except Exception:
        return
    names = {'speak more.exe', 'start_server.exe'}
    running = [p.info['pid'] for p in psutil.process_iter(['pid', 'name'])
               if (p.info['name'] or '').lower() in names]
    if running:
        console_print(f'  注意：检测到 {len(running)} 个正在运行的实例（PID {running}）。\n'
                      f'       打包读的是磁盘上的现有文件，不影响；\n'
                      f'       但如果你刚改过代码，记得先重新跑 pyinstaller 再打包。')


def build_zip(package_name: str, zip_stem: str, with_models: bool) -> Optional[Path]:
    """打包一个发行目录。返回 zip 路径；目录不存在时返回 None。"""
    src = DIST_DIR / package_name
    if not src.is_dir():
        print(f'  [跳过] dist/{package_name} 不存在（先跑 pyinstaller）')
        return None

    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d')
    out = RELEASE_DIR / f'{zip_stem}-{stamp}.zip'

    print(f'  打包 {package_name} → {out.name}')
    warn_if_running(print)

    count = 0
    total_bytes = 0
    t0 = time.time()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path, rel in iter_files(src, with_models):
            try:
                zf.write(path, arcname=str(Path(package_name) / rel))
                count += 1
                total_bytes += path.stat().st_size
            except Exception as e:
                print(f'    [警告] 跳过 {rel}: {e}')
            if count % 2000 == 0 and count:
                print(f'    …已写入 {count} 个文件')

    size_mb = out.stat().st_size / 1024 / 1024
    raw_mb = total_bytes / 1024 / 1024
    print(f'  完成：{count} 个文件，原始 {raw_mb:.0f}MB → 压缩后 {size_mb:.0f}MB，'
          f'耗时 {time.time() - t0:.0f}s')
    return out


def main() -> int:
    args = sys.argv[1:]
    with_models = '--with-models' in args

    print('=' * 66)
    print('发行打包' + ('（含模型一体包）' if with_models else '（不含模型）'))
    print('=' * 66)

    if not DIST_DIR.is_dir():
        print(f'找不到 {DIST_DIR}，先跑：uv run pyinstaller build.spec')
        return 1

    made: List[Tuple[str, Path]] = []
    for package_name, stem in PACKAGES:
        out = build_zip(package_name, stem, with_models)
        if out is not None:
            made.append((package_name, out))
        print()

    if not made:
        print('没有打包出任何东西 —— 检查 dist/ 下有没有构建产物')
        return 1

    print('=' * 66)
    print('产物：')
    for name, out in made:
        print(f'  {out}   ({out.stat().st_size / 1024 / 1024:.0f}MB)  源: dist/{name}')
    print('=' * 66)
    if not with_models:
        print('提示：包内**不含模型**。用户拿到后需要把模型放到 models/ 下，')
        print('      缺文件时客户端启动会直接弹窗告诉缺哪个、放哪、去哪下。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
