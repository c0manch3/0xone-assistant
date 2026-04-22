"""RQ3 — FS type detection on Darwin (devil ID-C4).

Confirms:
  * os.statvfs has no f_fstypename attribute on Darwin (devil C4 reproduced).
  * `stat -f "%T"` returns FILE TYPE (Directory/RegularFile), NOT FS type on macOS
    — a common blind spot; don't use for this purpose.
  * `mount` output parsing is the reliable cross-POSIX method.
  * Linux: `stat -f -c '%T' <path>` DOES return FS type (different meaning of %T!).

Probe targets: /, ~, /tmp, iCloud Mobile Documents, CloudStorage.

Run:  python3 plan/phase4/spikes/rq3_fs_type.py
Output: plan/phase4/spikes/rq3_fs_type.txt
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "rq3_fs_type.txt"


def probe_statvfs_has_fstypename() -> dict:
    try:
        s = os.statvfs(str(Path.home()))
    except OSError as exc:
        return {"error": str(exc)}
    attrs = [a for a in dir(s) if a.startswith("f_")]
    return {"attrs": attrs, "has_f_fstypename": hasattr(s, "f_fstypename")}


def detect_fs_type_darwin(path: Path) -> str:
    """Find FS type backing `path` by parsing `mount` output.

    macOS `mount` format:
      '/dev/disk1s1 on / (apfs, sealed, local, ...)'
    We extract the first word inside the parens.
    """
    try:
        target = Path(path).resolve()
    except OSError:
        return "unknown"
    try:
        r = subprocess.run(["mount"], capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"
    if r.returncode != 0:
        return "unknown"
    best_len = -1
    best_fs = "unknown"
    for line in r.stdout.splitlines():
        if " on " not in line:
            continue
        try:
            _, rest = line.split(" on ", 1)
            mp, tail = rest.split(" ", 1)
        except ValueError:
            continue
        try:
            target.relative_to(mp)
        except ValueError:
            if str(target) != mp:
                continue
        m = re.search(r"\(([a-z0-9]+)", tail)
        if m and len(mp) > best_len:
            best_len = len(mp)
            best_fs = m.group(1)
    return best_fs


def detect_fs_type_linux(path: Path) -> str:
    """Linux: ``stat -f -c '%T' <path>`` returns e.g. 'ext4', 'btrfs'."""
    try:
        r = subprocess.run(
            ["stat", "-f", "-c", "%T", str(path)],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0:
            return (r.stdout.strip() or "unknown").lower()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


def detect_fs_type(path: Path) -> str:
    """Cross-platform detection. Returns canonical lowercase string."""
    if sys.platform == "darwin":
        return detect_fs_type_darwin(path)
    if sys.platform.startswith("linux"):
        return detect_fs_type_linux(path)
    return "unknown"


def demonstrate_stat_T_trap() -> str:
    """On macOS, `stat -f "%T"` is FILE type (Directory), NOT FS type."""
    r = subprocess.run(
        ["stat", "-f", "%HT", "/"],
        capture_output=True,
        text=True,
        timeout=3,
    )
    return r.stdout.strip()


def main() -> int:
    lines: list[str] = []

    def w(s: str = "") -> None:
        lines.append(s)
        print(s)

    w(f"platform: {sys.platform}")
    w(f"python: {sys.version.split()[0]}")
    w()

    w("## 1. os.statvfs.f_fstypename — devil C4 reproduction")
    st = probe_statvfs_has_fstypename()
    w(f"  attrs: {st.get('attrs')}")
    w(f"  has_f_fstypename: {st.get('has_f_fstypename')}")
    if st.get("has_f_fstypename") is False:
        w("  CONFIRMED: devil-wave-1 ID-C4 correct — attribute absent on Darwin.")
    w()

    if sys.platform == "darwin":
        w("## 2. `stat -f \"%T\"` macOS trap")
        file_type = demonstrate_stat_T_trap()
        w(f"  stat -f '%HT' / → {file_type!r}  (this is FILE type, NOT FS type)")
        w("  => on macOS do NOT use `stat -f %T` for FS type detection.")
        w()

    w("## 3. Recommended approach — `mount` output parsing (macOS)")
    targets: list[Path] = []
    for p in [
        "/",
        "/tmp",
        os.path.expanduser("~"),
        os.path.expanduser("~/Documents"),
        os.path.expanduser("~/Library/Mobile Documents"),
        os.path.expanduser("~/Library/CloudStorage"),
        os.path.expanduser("~/Dropbox"),
        os.path.expanduser("~/.local/share/0xone-assistant/vault"),
        "/dev",
    ]:
        path = Path(p)
        if path.exists():
            targets.append(path)

    observed_types: set[str] = set()
    for tgt in targets:
        fs = detect_fs_type(tgt)
        observed_types.add(fs)
        w(f"  {str(tgt)!r:<72} fs={fs}")
    w()

    w(f"## 4. Observed FS types on this machine: {sorted(observed_types)!r}")
    w("    (limited sample — owner workstation has no iCloud/SMB/Dropbox mounts attached)")
    w()

    w("## 5. Proposed whitelist")
    SAFE_FS = sorted({"apfs", "hfs", "hfsplus", "ufs", "ext2", "ext3", "ext4", "btrfs", "xfs", "tmpfs", "zfs"})
    UNSAFE_FS = sorted({"smbfs", "afpfs", "nfs", "nfs4", "cifs", "fuse", "osxfuse", "webdav", "msdos", "exfat", "vfat"})
    w(f"  SAFE_FS_TYPES   = {SAFE_FS!r}")
    w(f"  UNSAFE_FS_TYPES = {UNSAFE_FS!r}")
    w("    Not in either list → treat as 'unknown' and also warn.")
    w()

    w("## 6. Path-prefix heuristics (macOS-specific)")
    w("""
    Cloud-sync directories often report APFS but behave unsafely:
      - ~/Library/Mobile Documents/...  → iCloud Drive (FileProvider on 10.15+)
      - ~/Library/CloudStorage/...      → iCloud/OneDrive/GoogleDrive/Dropbox (modern)
      - ~/Dropbox/...                   → Dropbox Smart Sync (may be FUSE or FileProvider)
      - /Volumes/...                    → external volumes (often exFAT/msdos)
    Combine FS-type check + path prefix for best warning coverage.
    """)

    w("## Recommended function (drop into _memory_core.py)")
    w("""
import os, re, subprocess, sys
from pathlib import Path

_SAFE_FS_TYPES = frozenset({'apfs', 'hfs', 'hfsplus', 'ufs', 'ext2', 'ext3',
                            'ext4', 'btrfs', 'xfs', 'tmpfs', 'zfs'})
_UNSAFE_FS_TYPES = frozenset({'smbfs', 'afpfs', 'nfs', 'nfs4', 'cifs',
                              'fuse', 'osxfuse', 'webdav', 'msdos', 'exfat', 'vfat'})

_UNSAFE_PATH_HINTS = (
    ('~/Library/Mobile Documents', 'iCloud Drive'),
    ('~/Library/CloudStorage',     'CloudStorage (iCloud/OneDrive/GoogleDrive/Dropbox)'),
    ('~/Dropbox',                  'Dropbox'),
)


def _detect_fs_type_darwin(path: Path) -> str:
    try:
        target = path.resolve()
    except OSError:
        return 'unknown'
    try:
        r = subprocess.run(['mount'], capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 'unknown'
    if r.returncode != 0:
        return 'unknown'
    best_len = -1
    best_fs = 'unknown'
    for line in r.stdout.splitlines():
        if ' on ' not in line:
            continue
        try:
            _, rest = line.split(' on ', 1)
            mp, tail = rest.split(' ', 1)
        except ValueError:
            continue
        try:
            target.relative_to(mp)
        except ValueError:
            if str(target) != mp:
                continue
        m = re.search(r'\\(([a-z0-9]+)', tail)
        if m and len(mp) > best_len:
            best_len = len(mp)
            best_fs = m.group(1)
    return best_fs


def _detect_fs_type_linux(path: Path) -> str:
    try:
        r = subprocess.run(
            ['stat', '-f', '-c', '%T', str(path)],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            return (r.stdout.strip() or 'unknown').lower()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return 'unknown'


def detect_fs_type(path: Path) -> str:
    '''Best-effort FS type detection. Returns 'unknown' on any failure.'''
    if sys.platform == 'darwin':
        return _detect_fs_type_darwin(path)
    if sys.platform.startswith('linux'):
        return _detect_fs_type_linux(path)
    return 'unknown'


def warn_if_vault_unsafe_for_flock(path: Path, log) -> None:
    '''Log a loud warning if vault FS is likely to break fcntl.flock.

    Called at end of configure_memory. Single source of truth for the
    R8/D2 mitigation in phase-4 plan.
    '''
    fs = detect_fs_type(path)
    p_str = str(path.resolve())
    hint: str | None = None
    for frag, label in _UNSAFE_PATH_HINTS:
        if os.path.expanduser(frag) in p_str:
            hint = label
            break
    if fs in _UNSAFE_FS_TYPES:
        log.warning('memory_vault_fs_unsafe', vault=p_str, fs=fs,
                    note='advisory flock may be a no-op; writes may race')
    elif hint:
        log.warning('memory_vault_cloudsync_detected', vault=p_str,
                    fs=fs, hint=hint,
                    note='cloud-sync directories may silently drop atomic renames or flock guarantees')
    elif fs == 'unknown':
        log.info('memory_vault_fs_unknown', vault=p_str,
                 note='fs type detection failed; concurrent writes assumed OK')
    elif fs not in _SAFE_FS_TYPES:
        log.warning('memory_vault_fs_unrecognised', vault=p_str, fs=fs)
""")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
