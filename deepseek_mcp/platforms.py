"""Cross-platform process, path and helper primitives.

Everything here is a small, injectable function so the test suite can exercise
macOS, Linux and Windows behaviour on any host. Callers pass an explicit
``system`` only in tests; production uses the current platform.

Python 3.9 compatible; standard library only.
"""

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WINDOWS = "Windows"
MACOS = "Darwin"
LINUX = "Linux"


def normalize_system(system=None):
    text = str(system or "").strip()
    if text in (WINDOWS, MACOS, LINUX):
        return text
    low = text.lower()
    if low in ("windows", "win32", "nt"):
        return WINDOWS
    if low in ("darwin", "macos", "mac"):
        return MACOS
    if low in ("linux", "posix"):
        return LINUX
    if os.name == "nt":
        return WINDOWS
    if sys.platform == "darwin":
        return MACOS
    return LINUX


def home_dir(env=None):
    env = os.environ if env is None else env
    raw = env.get("USERPROFILE") or env.get("HOME")
    return str(raw or os.path.expanduser("~"))


def default_paths(system=None, home=None, env=None):
    """Return private default locations for config, runs, jobs and state."""
    system = normalize_system(system)
    env = os.environ if env is None else env
    home = home or home_dir(env)
    if system == WINDOWS:
        config_root = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        local_root = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return {
            "config_dir": os.path.join(config_root, "deepseek-harness-mcp"),
            "data_dir": os.path.join(local_root, "DeepSeek-MCP"),
            "temp_dir": (env.get("TEMP") or env.get("TMP")
                         or os.path.join(local_root, "Temp")),
        }
    if system == LINUX:
        config_root = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        state_root = env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
        data_root = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        return {
            "config_dir": os.path.join(config_root, "deepseek-harness-mcp"),
            "data_dir": os.path.join(data_root, "DeepSeek-MCP"),
            "state_dir": os.path.join(state_root, "DeepSeek-MCP"),
            "temp_dir": env.get("TMPDIR") or tempfile.gettempdir(),
        }
    # macOS keeps the historical locations for backward compatibility.
    return {
        "config_dir": os.path.join(home, ".config", "deepseek-harness-mcp"),
        "data_dir": os.path.join(home, "DeepSeek-MCP"),
        "temp_dir": env.get("TMPDIR") or tempfile.gettempdir(),
    }


def _existing(paths):
    result = []
    for raw in paths:
        try:
            text = os.path.expandvars(os.path.expanduser(str(raw)))
            if text and Path(text).is_file():
                result.append(text)
        except (TypeError, ValueError, OSError):
            continue
    return result


def chrome_candidates(system=None, env=None):
    """Trusted Chrome/Edge paths for the platform, in preference order."""
    system = normalize_system(system)
    env = os.environ if env is None else env
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    names = ("chrome-headless-shell", "chromium", "chromium-browser",
             "google-chrome", "google-chrome-stable")
    paths = []
    if system == WINDOWS:
        roots = [env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"),
                 env.get("LOCALAPPDATA")]
        for root in [item for item in roots if item]:
            paths.extend([
                os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
            ])
    elif system == MACOS:
        paths.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ])
    else:
        paths.extend([
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium", "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ])
    for name in names:
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            paths.insert(0, found)
    return _existing(paths)


def dsh_candidates(system=None, env=None):
    """Common DeepSeek Harness CLI locations; DSH_BIN still wins."""
    system = normalize_system(system)
    env = os.environ if env is None else env
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    paths = []
    if system == MACOS:
        paths.append("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")
    elif system == WINDOWS:
        local = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        paths.extend([
            os.path.join(local, "Programs", "dsh", "dsh.exe"),
            os.path.join(local, "DeepSeek", "Harness", "dsh.exe"),
        ])
    paths.extend([
        os.path.join(home, ".local", "bin", "dsh"),
        "/usr/local/bin/dsh",
        "/opt/homebrew/bin/dsh",
    ])
    found = shutil.which("dsh", path=env.get("PATH"))
    if found:
        paths.insert(0, found)
    return _existing(paths)


def popen_kwargs(system=None):
    """Detach a child so it survives the MCP client reconnecting."""
    system = normalize_system(system)
    if system == WINDOWS:
        flags = 0
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def pid_alive(pid, system=None):
    """Safe liveness check (Windows os.kill(pid, 0) can terminate)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if normalize_system(system) == WINDOWS:
        handle = None
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION; signal 0 on Windows is
            # CTRL_C_EVENT and must never be used as a liveness probe.
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
        finally:
            if handle:
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _pid_alive(pid):
    return pid_alive(pid)


def terminate_tree(pid, system=None, runner=None, timeout=5.0):
    """Terminate one process and its children; never signal our own group.

    POSIX: SIGTERM to the child's own process group (when it leads one), wait,
    then SIGKILL. Windows: ``taskkill /PID <pid> /T /F``. Returns a small dict
    for the run record; never raises.
    """
    system = normalize_system(system)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"terminated": False, "reason": "invalid pid"}
    if pid <= 0 or pid == os.getpid():
        return {"terminated": False, "reason": "refused unsafe pid"}
    runner = runner or subprocess.run
    if system == WINDOWS:
        try:
            proc = runner(["taskkill", "/PID", str(pid), "/T", "/F"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)
            code = getattr(proc, "returncode", 1)
            return {"terminated": code == 0, "method": "taskkill", "exit_code": code}
        except Exception as exc:
            return {"terminated": False, "method": "taskkill", "reason": str(exc)}
    pgid = None
    try:
        pgid = os.getpgid(pid)
    except (OSError, ValueError):
        pgid = None
    # Only signal a group when the target leads it. Signalling the server's own
    # group would kill the MCP client process.
    use_group = pgid is not None and pgid == pid and pgid != os.getpgid(0)
    try:
        if use_group:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError) as exc:
        return {"terminated": False, "method": "killpg" if use_group else "kill",
                "reason": str(exc)}
    deadline = time.time() + max(0.0, float(timeout))
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    if _pid_alive(pid):
        try:
            if use_group:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
    return {"terminated": True, "method": "killpg" if use_group else "kill"}


def keep_awake_command(system=None, runner=None):
    """Return an argv that keeps the host awake, or ``None``.

    Windows is handled in-process with ``SetThreadExecutionState`` (see
    mac.py), so it returns ``None`` here with a marker.
    """
    system = normalize_system(system)
    if system == MACOS:
        return ["/usr/bin/caffeinate", "-i", "-s", "-m"]
    if system == LINUX:
        if shutil.which("systemd-inhibit"):
            return ["systemd-inhibit", "--what=idle:sleep", "--why=DeepSeek MCP",
                    "--mode=block", "sleep", "infinity"]
        return None
    return None


def screenshot_command(system=None, path="", env=None):
    """Return an argv that captures ``path``, or ``None`` when unsupported."""
    system = normalize_system(system)
    env = os.environ if env is None else env
    if system == MACOS:
        binary = "/usr/sbin/screencapture"
        return [binary, "-x", "-t", "png", str(path)] if Path(binary).is_file() else None
    if system == WINDOWS:
        escaped = str(path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
            "$g=[System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); "
            "$bmp.Save('%s',[System.Drawing.Imaging.ImageFormat]::Png)" % escaped
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    for name, prefix in (("gnome-screenshot", ["-f"]), ("scrot", []),
                         ("grim", []), ("spectacle", ["-b", "-o"])):
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            return [found] + prefix + [str(path)]
    return None


def restart_client_command(system=None, app_name="Claude"):
    """Return a platform command that restarts ``app_name``, or ``None``.

    This is the single resolver: it delegates to
    :func:`restart_helper.resolve_commands` instead of keeping a second
    Linux opener list.  The returned mapping still has the historical
    ``quit``/``open`` keys and adds ``verify``.
    """
    system = normalize_system(system)
    name = str(app_name or "").strip()
    if not name:
        return None
    try:
        from . import restart_helper
    except Exception:
        try:
            import restart_helper  # type: ignore
        except Exception:
            return None
    family = {"Darwin": "macos", "Windows": "windows",
              "Linux": "linux"}.get(system)
    if not family:
        return None
    try:
        commands = restart_helper.resolve_commands(family, name)
    except Exception:
        return None
    return {
        "quit": list(commands.get("close") or []),
        "open": list(commands.get("open") or []),
        "verify": list(commands.get("verify") or []),
        "expected_executable": commands.get("expected_executable"),
    }


def sandbox_support(system=None):
    """Deprecated shim: describe the OS sandbox layer via ``safety``.

    The single authority is :func:`deepseek_mcp.safety.sandbox_status`; this
    function exists for one minor so older callers keep a mapping with
    ``kind``/``available``/``reason``.
    """
    system = normalize_system(system)
    try:
        from . import safety
        status = safety.sandbox_status(system=system)
    except Exception as exc:  # pragma: no cover - defensive
        return {"kind": "none", "available": False,
                "reason": "sandbox status unavailable: %s" % exc.__class__.__name__}
    kind = status.get("kind") or "none"
    enforcement = status.get("enforcement") or "none"
    return {
        "kind": kind,
        "available": kind not in ("none", "") and enforcement != "none",
        "enforcement": enforcement,
        "abi": status.get("abi"),
        "reason": status.get("reason"),
    }


def canonical_path(path, system=None):
    r"""Return a canonical absolute path (Windows: no extended prefix).

    Case is deliberately preserved: NTFS is case-insensitive, but records,
    workspace roots and run journals must report the spelling the caller used
    (``D:\a\...``, not ``d:\a\...``).  Containment comparisons
    apply ``normcase`` themselves.
    """
    text = os.path.expanduser(os.path.expandvars(str(path)))
    text = os.path.realpath(os.path.abspath(text))
    if normalize_system(system) == WINDOWS:
        if text.lower().startswith("\\\\?\\unc\\"):
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
    return text


def reparse_point(path, system=None):
    """True for a symlink or a Windows reparse point/junction.

    Python 3.9 has no ``os.path.isjunction``; ``st_file_attributes`` is the
    supported check on Windows and ``lstat`` never follows the link.
    """
    try:
        if normalize_system(system) == WINDOWS:
            attrs = getattr(os.lstat(str(path)), "st_file_attributes", 0)
            return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
        return os.path.islink(str(path))
    except (OSError, ValueError, TypeError):
        return False


def inside_path(path, root, system=None):
    """True when *path* is *root* or below it after canonicalization."""
    try:
        path = canonical_path(path, system)
        root = canonical_path(root, system)
        if normalize_system(system) == WINDOWS:
            path = os.path.normcase(path)
            root = os.path.normcase(root)
        return os.path.commonpath([path, root]) == root
    except (OSError, ValueError, TypeError):
        return False


def split_command(text, system=None):
    """Split a command string: JSON argv wins, else shlex with OS quoting."""
    if text is None:
        return []
    if isinstance(text, (list, tuple)):
        return [str(item) for item in text]
    raw = str(text).strip()
    if not raw:
        return []
    if raw.startswith("["):
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("command must be a JSON list")
        return [str(item) for item in parsed]
    return shlex.split(raw, posix=normalize_system(system) != WINDOWS)


def _windows_systemroot():
    return os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"


def _icacls_binary():
    return os.path.join(_windows_systemroot(), "System32", "icacls.exe")


def _windows_user_sid():
    """Return the current user SID string, or ``None``; never raises."""
    try:
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        access_handle = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(access_handle)):
            return None
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(access_handle, 1, None, 0,
                                         ctypes.byref(needed))
            if not needed.value:
                return None
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                    access_handle, 1, buffer, needed.value, ctypes.byref(needed)):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            string_sid = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(
                    sid, ctypes.byref(string_sid)):
                return None
            try:
                return str(string_sid.value)
            finally:
                kernel32.LocalFree(string_sid)
        finally:
            kernel32.CloseHandle(access_handle)
    except Exception:
        return None


def _windows_trustees(sid):
    """Return the casefolded trustee spellings icacls may print for this user.

    ``icacls`` renders an ACE trustee as ``DOMAIN\\user`` (or
    ``COMPUTER\\user`` for a local account), never as the literal SID that
    ``/grant:r *SID:F`` accepted.  The environment spelling is the normal case;
    the caller asks ``whoami`` only when this set does not match.
    """
    names = set()
    if sid:
        names.add(str(sid).casefold())
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if user:
        for domain in (os.environ.get("USERDOMAIN"),
                       os.environ.get("COMPUTERNAME")):
            if domain:
                names.add(("%s\\%s" % (domain, user)).casefold())
    return names


def _windows_whoami_trustees():
    """Return ``domain\\user`` names from whoami; never raises."""
    binary = os.path.join(_windows_systemroot(), "System32", "whoami.exe")
    try:
        result = subprocess.run(
            [binary], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=5.0)
        if int(getattr(result, "returncode", 1)) != 0:
            return set()
        output = (getattr(result, "stdout", b"") or b"").decode(
            "utf-8", "replace")
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    return {line.strip().casefold() for line in output.splitlines()
            if "\\" in line.strip()}


#: Trustees that are part of the normal Windows DACL for a file created by an
#: elevated process.  They are trusted system principals, not ordinary users;
#: their presence does not make the file readable by other accounts.
_WINDOWS_TRUSTED_ACE_TRUSTEES = (
    "nt authority\\system",
    "builtin\\administrators",
    "nt authority\\local service",
    "nt authority\\network service",
    "creator owner",
    "creator group",
    "owner rights",
)


def _windows_acl_is_private(aces, names):
    """True when one explicit F ACE names *names* and the rest are trusted."""
    owner_full = False
    for line in aces:
        folded = line.casefold()
        if any(("%s:(" % name) in folded for name in names):
            if not folded.endswith("(f)"):
                return False
            owner_full = True
            continue
        if not any(("%s:" % trusted) in folded
                   for trusted in _WINDOWS_TRUSTED_ACE_TRUSTEES):
            return False
    return owner_full


def _windows_private_acl(path, is_dir):
    sid = _windows_user_sid()
    binary = _icacls_binary()
    if not sid or not os.path.isfile(binary):
        return False
    try:
        result = subprocess.run(
            [binary, str(path), "/inheritance:r", "/grant:r", "*%s:F" % sid],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10.0)
        if int(getattr(result, "returncode", 1)) != 0:
            return False
        check = subprocess.run(
            [binary, str(path)], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10.0)
        output = (getattr(check, "stdout", b"") or b"").decode(
            "utf-8", "replace")
        if int(getattr(check, "returncode", 1)) != 0:
            return False
        # icacls renders a trustee as DOMAIN\user, not as S-1-5-...; the old
        # ``sid in output`` test therefore always failed on a normal runner.
        # A file created in a hosted temp directory can also carry explicit
        # SYSTEM/Administrators ACEs from the creator's default DACL, so a
        # single-ACE test is too strict.  Accept the current user's full-control
        # ACE plus only well-known system principals; any other trustee
        # (Everyone, Users, a foreign account) fails the read-back.  Inherited
        # or inherit-only ACEs mean /inheritance:r did not take effect.
        aces = [line.strip() for line in output.splitlines()
                if line.strip().endswith(")") and ":(" in line]
        if not aces or any("(I)" in line for line in aces):
            return False
        expected = _windows_trustees(sid)
        if _windows_acl_is_private(aces, expected):
            return True
        # The account name can differ from the environment spelling (renamed
        # account, Microsoft/AzureAD alias); one SID-bound whoami lookup
        # covers that case, then re-check.
        return _windows_acl_is_private(
            aces, expected | _windows_whoami_trustees())
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def private_permissions(path, system=None, is_dir=False):
    """Owner-only permissions: 0700/0600 POSIX, owner-only ACL on Windows."""
    if normalize_system(system) == WINDOWS:
        return _windows_private_acl(path, is_dir)
    mode = 0o700 if is_dir else 0o600
    try:
        os.chmod(str(path), mode, follow_symlinks=False)
        return True
    except (TypeError, NotImplementedError):
        try:
            if os.path.islink(str(path)):
                return False
            os.chmod(str(path), mode)
            return True
        except OSError:
            return False
    except OSError:
        return False


@contextlib.contextmanager
def file_lock(path, timeout=10.0, system=None):
    """Bounded exclusive lock: fcntl.flock or msvcrt.locking.

    Raises the built-in ``TimeoutError`` after *timeout* seconds.  The lock
    file itself is kept (unlinking a locked file is racy); callers own the
    parent directory.
    """
    path = Path(os.path.expanduser(str(path)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    system = normalize_system(system)
    deadline = time.time() + max(0.0, float(timeout))
    handle = None
    try:
        # 0600: a lock file is private state and must not be group/other
        # readable just because the process umask is 022.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "a+b")
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise TimeoutError("cannot open lock %s: %s" % (path, exc))
    acquired = False
    try:
        if system == WINDOWS:
            try:
                import msvcrt
            except ImportError:
                msvcrt = None
            if msvcrt is not None:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                        break
                    except OSError:
                        if time.time() >= deadline:
                            raise TimeoutError("lock timeout: %s" % path)
                        time.sleep(0.02)
        if not acquired:
            try:
                import fcntl
            except ImportError:
                fcntl = None
            if fcntl is not None:
                while True:
                    try:
                        fcntl.flock(handle.fileno(),
                                    fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except OSError:
                        if time.time() >= deadline:
                            raise TimeoutError("lock timeout: %s" % path)
                        time.sleep(0.02)
        if not acquired:
            # Last resort: an O_EXCL sibling file with bounded retries.
            fallback = str(path) + ".excl"
            while True:
                try:
                    fd = os.open(fallback, os.O_CREAT | os.O_EXCL | os.O_RDWR,
                                 0o600)
                    os.close(fd)
                    acquired = True
                    break
                except FileExistsError:
                    if time.time() >= deadline:
                        raise TimeoutError("lock timeout: %s" % path)
                    time.sleep(0.02)
        yield handle
    finally:
        if acquired:
            try:
                if system == WINDOWS:
                    try:
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                else:
                    try:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            handle.close()
        except Exception:
            pass


def secure_tree(root, system=None):
    """Best-effort recursive private permissions; returns (dirs, files).

    An absent, ``None`` or blank path is a no-op. In particular ``""`` must
    not become ``"."`` and chmod the process working directory.
    """
    dirs = files = 0
    if root is None or not str(root).strip():
        return dirs, files
    root = Path(root)
    if not root.exists():
        return dirs, files
    for current, subdirs, names in os.walk(str(root)):
        if private_permissions(current, system, is_dir=True):
            dirs += 1
        for name in names:
            if private_permissions(os.path.join(current, name), system, is_dir=False):
                files += 1
    return dirs, files
