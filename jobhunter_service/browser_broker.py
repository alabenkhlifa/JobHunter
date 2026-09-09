"""Fixed Docker operations for the root-owned JobHunter browser helper.

The installed helper and this module must be root-owned, outside a writable
checkout. Its only command-line inputs are an operation and validated profile
ID. Operator configuration is read from one fixed root-owned file.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import quote

from .browser import BrowserError, valid_profile

CONFIG_PATH = Path("/etc/jobhunter/browser.json")
NETWORK = "jobhunter_browser"
RELAY_NETWORK = "jobhunter_relay"
RELAY_PREFIX = "jobhunter-browser-relay-"
SUBNET = "172.30.77.0/24"
MANAGED_LABEL = "jobhunter.managed=browser"
CONTAINER_PREFIX = "jobhunter-browser-"
_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
RELAY_SCRIPT = 'socat TCP-LISTEN:6080,fork,reuseaddr TCP:"$1":6080 & exec socat TCP-LISTEN:9223,fork,reuseaddr TCP:"$1":9223'


def relay_command(address: str, ttl: int) -> list[str]:
    return ["--", "/usr/bin/timeout", "--signal=TERM", "--kill-after=5", str(ttl),
            "/bin/sh", "-c", RELAY_SCRIPT, "jobhunter-relay", address]


class BrokerFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _no_symlinks(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise BrokerFailure("configuration")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise BrokerFailure("configuration")
        except FileNotFoundError:
            break


def _operator_file(path: Path, owner_uid: int) -> None:
    _no_symlinks(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
        raise BrokerFailure("configuration")


@dataclass(frozen=True)
class BrokerConfig:
    data_root: Path
    ttl_seconds: int = 1800
    image: str = "jobhunter-browser:local"
    docker_path: str = "/usr/bin/docker"


def load_config(path: Path = CONFIG_PATH, *, owner_uid: int = 0) -> BrokerConfig:
    try:
        _operator_file(path, owner_uid)
        if path.stat().st_size > 4096:
            raise BrokerFailure("configuration")
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(value) - {"data_root", "ttl_seconds", "image", "docker_path"}:
            raise BrokerFailure("configuration")
        root = Path(value["data_root"])
        _no_symlinks(root)
        if not root.is_dir() or "," in str(root) or any(ord(char) < 32 for char in str(root)):
            raise BrokerFailure("configuration")
        ttl = value.get("ttl_seconds", 1800)
        if type(ttl) is not int or not 60 <= ttl <= 3600:
            raise BrokerFailure("configuration")
        image = value.get("image", "jobhunter-browser:local")
        docker = value.get("docker_path", "/usr/bin/docker")
        if not isinstance(image, str) or not image or len(image) > 200 or any(char.isspace() for char in image) or image.startswith("-"):
            raise BrokerFailure("configuration")
        if not isinstance(docker, str) or not Path(docker).is_absolute() or ".." in Path(docker).parts:
            raise BrokerFailure("configuration")
        # An operator-selected executable is trusted only while root-owned and
        # not writable by the restricted service user.
        _operator_file(Path(docker), owner_uid)
        if not os.access(docker, os.X_OK):
            raise BrokerFailure("configuration")
        return BrokerConfig(root, ttl, image, docker)
    except (OSError, ValueError, TypeError, KeyError):
        raise BrokerFailure("configuration") from None


class BrowserBroker:
    def __init__(self, config: BrokerConfig, *, runner=None, clock=None, owner_uid: int = 0, lock_path: Path | None = None):
        self.config = config
        self._runner = runner or subprocess.run
        self._clock = clock or time.time
        self.owner_uid = owner_uid
        self.lock_path = lock_path or Path("/run/jobhunter-browser.lock")

    def _identity(self) -> tuple[int, int]:
        _no_symlinks(self.config.data_root)
        info = self.config.data_root.stat()
        if info.st_uid <= 0 or info.st_gid <= 0 or info.st_mode & 0o022:
            raise BrokerFailure("configuration")
        return info.st_uid, info.st_gid

    @contextmanager
    def _lock(self):
        path = self.lock_path
        _no_symlinks(path)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o022:
                raise BrokerFailure("configuration")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _active(self, profile_id: str) -> bool:
        path = self.config.data_root / "service" / "registry.sqlite3"
        _no_symlinks(path)
        if not path.is_file():
            raise BrokerFailure("configuration")
        try:
            db = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=5)
            try:
                row = db.execute("SELECT user_id,status FROM members WHERE profile_id=?", (profile_id,)).fetchone()
            finally:
                db.close()
            return bool(row and row[0] == int(profile_id[1:]) and row[1] == "active")
        except sqlite3.Error:
            raise BrokerFailure("registry") from None

    def _docker(self, arguments: list[str], *, missing_ok=False):
        try:
            result = self._runner([self.config.docker_path, *arguments], capture_output=True,
                                  text=True, timeout=35, env=_ENV)
        except (OSError, subprocess.TimeoutExpired):
            raise BrokerFailure("docker") from None
        if len(result.stdout) > 1024 * 1024:
            raise BrokerFailure("docker")
        if result.returncode:
            if missing_ok and result.returncode == 1:
                return None
            raise BrokerFailure("docker")
        return result.stdout.strip()

    def _inspect(self, profile_id: str, *, relay: bool = False):
        prefix = RELAY_PREFIX if relay else CONTAINER_PREFIX
        raw = self._docker(["container", "inspect", prefix + profile_id], missing_ok=True)
        if raw is None:
            return None
        try:
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) != 1:
                raise ValueError
            value = values[0]
            labels = value["Config"]["Labels"]
            if (value["Name"] != "/" + prefix + profile_id or
                    labels.get("jobhunter.managed") != ("browser-relay" if relay else "browser") or labels.get("jobhunter.profile") != profile_id):
                raise ValueError
            expires = float(labels["jobhunter.expires_at"])
            if not math.isfinite(expires):
                raise ValueError
            return value, expires
        except (KeyError, TypeError, ValueError):
            raise BrokerFailure("container") from None

    def _browser_address(self, container) -> str:
        try:
            address = container["NetworkSettings"]["Networks"][NETWORK]["IPAddress"]
            parsed = ipaddress.ip_address(address)
            if parsed not in ipaddress.ip_network(SUBNET) or str(parsed) in {"172.30.77.0", "172.30.77.1", "172.30.77.255"}:
                raise ValueError
            return str(parsed)
        except (KeyError, TypeError, ValueError):
            raise BrokerFailure("container") from None

    def _running_result(self, profile_id: str, container, expires: float) -> dict:
        try:
            if not container["State"]["Running"] or expires <= self._clock() or expires > self._clock() + 3605:
                raise ValueError
            host = container["HostConfig"]
            uid, gid = self._identity()
            if (host["Privileged"] or host["NetworkMode"] != NETWORK or
                    set(container["NetworkSettings"]["Networks"]) != {NETWORK}):
                raise ValueError
            if (container["Config"].get("Image") != self.config.image or
                    container["Config"].get("User") != f"{uid}:{gid}" or
                    host.get("CapDrop") != ["ALL"] or
                    not any(option in {"no-new-privileges", "no-new-privileges:true"}
                            for option in host.get("SecurityOpt", [])) or
                    host.get("Memory") != 1024 ** 3 or host.get("NanoCpus") != 2_000_000_000 or
                    host.get("PidsLimit") != 256 or host.get("ShmSize") != 256 * 1024 ** 2):
                raise ValueError
            mounts = container.get("Mounts", [])
            expected = {"/data": (self.config.data_root / profile_id / "browser-profile", True),
                        "/documents": (self.config.data_root / profile_id / "output", False)}
            if not isinstance(mounts, list) or len(mounts) != 2:
                raise ValueError
            if {mount.get("Destination") for mount in mounts} != set(expected):
                raise ValueError
            for mount in mounts:
                source, writable = expected[mount["Destination"]]
                _no_symlinks(source)
                if (mount.get("Type") != "bind" or mount.get("Source") != str(source)
                        or mount.get("RW") is not writable):
                    raise ValueError
            address = self._browser_address(container)
            if any(container["NetworkSettings"].get("Ports", {}).values()) or host.get("PortBindings"):
                raise ValueError
            relay_info = self._inspect(profile_id, relay=True)
            if relay_info is None:
                raise ValueError
            relay, relay_expiry = relay_info
            relay_host = relay["HostConfig"]
            if (relay_expiry != expires or relay["State"].get("Running") is not True or
                    relay["Config"].get("Image") != self.config.image or
                    relay["Config"].get("User") != f"{uid}:{gid}" or
                    relay["Config"].get("Entrypoint") != ["/usr/bin/tini"] or
                    relay["Config"].get("Cmd") != relay_command(address, self.config.ttl_seconds) or
                    relay_host.get("Privileged") is not False or relay_host.get("ReadonlyRootfs") is not True or
                    relay_host.get("CapDrop") != ["ALL"] or
                    not any(option in {"no-new-privileges", "no-new-privileges:true"} for option in relay_host.get("SecurityOpt", [])) or
                    relay_host.get("Memory") != 128 * 1024 ** 2 or relay_host.get("NanoCpus") != 500_000_000 or
                    relay_host.get("PidsLimit") != 64 or relay.get("Mounts") != [] or
                    relay_host.get("NetworkMode") != RELAY_NETWORK or
                    set(relay["NetworkSettings"]["Networks"]) != {NETWORK, RELAY_NETWORK}):
                raise ValueError
            ports = relay["NetworkSettings"]["Ports"]
            def port(container_port):
                bindings = ports[container_port]
                if not isinstance(bindings, list) or len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
                    raise ValueError
                result = int(bindings[0]["HostPort"])
                if not 1024 <= result <= 65535:
                    raise ValueError
                return result
            viewer, cdp = port("6080/tcp"), port("9223/tcp")
            if viewer == cdp:
                raise ValueError
            return {"profile_id": profile_id, "status": "running", "viewer_port": viewer,
                    "cdp_port": cdp, "expires_at": expires}
        except (KeyError, TypeError, ValueError):
            raise BrokerFailure("container") from None

    def _remove(self, profile_id: str) -> None:
        # Inspect the labels immediately before removal. A matching name alone
        # never grants permission to remove an unrelated existing container.
        main = self._inspect(profile_id)
        if self._inspect(profile_id, relay=True) is not None:
            self._docker(["container", "rm", "--force", RELAY_PREFIX + profile_id])
        if main is not None:
            self._docker(["container", "rm", "--force", CONTAINER_PREFIX + profile_id])

    def _live(self) -> dict:
        relay_names = self._docker(["container", "ls", "--all", "--filter", "label=jobhunter.managed=browser-relay",
                                    "--format", "{{.Names}}"])
        for name in relay_names.splitlines():
            if not name.startswith(RELAY_PREFIX):
                raise BrokerFailure("container")
            profile = name[len(RELAY_PREFIX):]
            try:
                valid_profile(profile)
            except BrowserError:
                raise BrokerFailure("container") from None
            if self._inspect(profile) is None:
                self._remove(profile)
        names = self._docker(["container", "ls", "--all", "--filter", "label=" + MANAGED_LABEL,
                              "--format", "{{.Names}}"])
        live = {}
        for name in names.splitlines():
            if not name.startswith(CONTAINER_PREFIX):
                raise BrokerFailure("container")
            profile_id = name[len(CONTAINER_PREFIX):]
            try:
                valid_profile(profile_id)
            except BrowserError:
                raise BrokerFailure("container") from None
            inspected = self._inspect(profile_id)
            if inspected is None:
                continue
            container, expires = inspected
            if expires <= self._clock() or not container.get("State", {}).get("Running") or not self._active(profile_id):
                self._remove(profile_id)
                continue
            relay_info = self._inspect(profile_id, relay=True)
            if relay_info is None or not relay_info[0].get("State", {}).get("Running"):
                self._remove(profile_id)
                continue
            live[profile_id] = self._running_result(profile_id, container, expires)
        return live

    def _volume(self, profile_id: str) -> Path:
        profile = self.config.data_root / profile_id
        _no_symlinks(profile)
        if not profile.is_dir():
            raise BrokerFailure("configuration")
        path = profile / "browser-profile"
        _no_symlinks(path)
        path.mkdir(mode=0o700, exist_ok=True)
        if not path.is_dir():
            raise BrokerFailure("configuration")
        # The fixed image runs as the restricted service UID. Files stay private
        # (0600), including generated PDFs mounted read-only under /documents.
        uid, gid = self._identity()
        if self.owner_uid == 0:
            os.chown(path, uid, gid)
        os.chmod(path, 0o700)
        return path

    def _documents(self, profile_id: str) -> Path:
        path = self.config.data_root / profile_id / "output"
        _no_symlinks(path)
        uid, gid = self._identity()
        if not path.exists():
            path.mkdir(mode=0o700)
            if self.owner_uid == 0:
                os.chown(path, uid, gid)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise BrokerFailure("configuration")
        return path

    def _validate_network(self) -> None:
        try:
            values = json.loads(self._docker(["network", "inspect", NETWORK]))
            if (not isinstance(values, list) or len(values) != 1 or values[0].get("Name") != NETWORK
                    or values[0].get("Internal") is not True or values[0].get("Driver") != "bridge"):
                raise ValueError
            subnets = values[0].get("IPAM", {}).get("Config", [])
            if len(subnets) != 1 or subnets[0].get("Subnet") != SUBNET:
                raise ValueError
            relays = json.loads(self._docker(["network", "inspect", RELAY_NETWORK]))
            if (not isinstance(relays, list) or len(relays) != 1 or relays[0].get("Name") != RELAY_NETWORK
                    or relays[0].get("Internal") is not False or relays[0].get("Driver") != "bridge"):
                raise ValueError
        except (TypeError, ValueError):
            raise BrokerFailure("network") from None
        checks = [
            ["-C", "INPUT", "-s", SUBNET, "-j", "JOBHUNTER_BROWSER"],
            ["-C", "JOBHUNTER_BROWSER", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            ["-C", "JOBHUNTER_BROWSER", "-j", "DROP"],
        ]
        try:
            for check in checks:
                result = self._runner(["/usr/sbin/iptables", *check], capture_output=True, text=True, timeout=10, env=_ENV)
                if result.returncode:
                    raise BrokerFailure("firewall")
        except (OSError, subprocess.TimeoutExpired):
            raise BrokerFailure("firewall") from None

    def _start_relay(self, profile_id: str, container, expiry: float) -> None:
        address = self._browser_address(container)
        uid, gid = self._identity()
        self._docker([
            "container", "run", "--detach", "--name", RELAY_PREFIX + profile_id,
            "--pull=never", "--restart=no", "--stop-timeout=5",
            "--label", "jobhunter.managed=browser-relay", "--label", "jobhunter.profile=" + profile_id,
            "--label", "jobhunter.expires_at=" + str(expiry), "--network", RELAY_NETWORK,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
            "--memory=128m", "--cpus=0.5", "--pids-limit=64", "--user", f"{uid}:{gid}",
            "--publish", "127.0.0.1::6080", "--publish", "127.0.0.1::9223",
            "--entrypoint", "/usr/bin/tini", self.config.image, *relay_command(address, self.config.ttl_seconds),
        ])
        self._docker(["network", "connect", NETWORK, RELAY_PREFIX + profile_id])

    def execute(self, operation: str, profile_id: str) -> dict:
        try:
            valid_profile(profile_id)
        except BrowserError:
            raise BrokerFailure("profile") from None
        if operation not in {"start", "stop", "status"}:
            raise BrokerFailure("operation")
        with self._lock():
            if operation == "stop":
                self._remove(profile_id)
                return {"profile_id": profile_id, "status": "stopped"}
            if not self._active(profile_id):
                if operation == "status":
                    self._remove(profile_id)
                    return {"profile_id": profile_id, "status": "stopped"}
                raise BrokerFailure("inactive")
            self._validate_network()
            live = self._live()
            if profile_id in live:
                return live[profile_id]
            if operation == "status":
                return {"profile_id": profile_id, "status": "stopped"}
            if live:
                raise BrokerFailure("capacity")
            volume = self._volume(profile_id)
            documents = self._documents(profile_id)
            uid, gid = self._identity()
            expiry = self._clock() + self.config.ttl_seconds
            arguments = [
                "container", "run", "--detach", "--name", CONTAINER_PREFIX + profile_id,
                "--pull=never", "--restart=no", "--stop-timeout=10",
                "--label", MANAGED_LABEL, "--label", "jobhunter.profile=" + profile_id,
                "--label", "jobhunter.expires_at=" + str(expiry),
                "--network", NETWORK, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory=1g", "--cpus=2", "--pids-limit=256", "--shm-size=256m",
                "--user", f"{uid}:{gid}",
                "--mount", f"type=bind,src={volume},dst=/data",
                "--mount", f"type=bind,src={documents},dst=/documents,readonly",
                "--env", "HOME=/data",
                "--env", "JOBHUNTER_BROWSER_TTL=" + str(self.config.ttl_seconds), self.config.image,
            ]
            self._docker(arguments)
            inspected = self._inspect(profile_id)
            if inspected is None:
                raise BrokerFailure("docker")
            try:
                self._start_relay(profile_id, inspected[0], expiry)
                return self._running_result(profile_id, *inspected)
            except BrokerFailure:
                self._remove(profile_id)
                raise


def main(argv=None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) != 2 or os.geteuid() != 0:
            raise BrokerFailure("configuration")
        result = BrowserBroker(load_config()).execute(arguments[0], arguments[1])
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except BrokerFailure as error:
        print(json.dumps({"error": error.code}))
    except Exception:
        # Never reveal subprocess stderr, host paths, cookies, or browser state.
        print(json.dumps({"error": "unavailable"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
