import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jobhunter_service.browser import (
    BROKER_HELPER, BrowserCapacityError, BrowserError, DockerBrowserManager,
    validate_browser_result,
)
from jobhunter_service.browser_broker import (
    BrowserBroker, BrokerConfig, BrokerFailure, CONTAINER_PREFIX, NETWORK, RELAY_NETWORK, RELAY_PREFIX, load_config, relay_command,
)


def container(profile="u11", expiry=None, viewer=32000, cdp=32001, root="/tmp/test-data"):
    return {
        "Name": "/" + CONTAINER_PREFIX + profile,
        "Config": {"Image": "jobhunter-browser:local", "User": f"{os.getuid()}:{os.getgid()}",
                   "Labels": {"jobhunter.managed": "browser", "jobhunter.profile": profile,
                              "jobhunter.expires_at": str(expiry or time.time() + 1800)}},
        "State": {"Running": True},
        "HostConfig": {"Privileged": False, "NetworkMode": NETWORK, "CapDrop": ["ALL"],
                       "SecurityOpt": ["no-new-privileges"], "Memory": 1024 ** 3,
                       "NanoCpus": 2_000_000_000, "PidsLimit": 256, "ShmSize": 256 * 1024 ** 2},
        "Mounts": [{"Type": "bind", "Source": str(root) + "/" + profile + "/browser-profile", "Destination": "/data", "RW": True},
                   {"Type": "bind", "Source": str(root) + "/" + profile + "/output", "Destination": "/documents", "RW": False}],
        "NetworkSettings": {"Networks": {NETWORK: {"IPAddress": "172.30.77.10"}}, "Ports": {"6080/tcp": None, "9223/tcp": None}},
    }


def relay_container(profile="u11", expiry=None):
    result = container(profile, expiry)
    result["Name"] = "/" + RELAY_PREFIX + profile
    result["Config"]["Labels"]["jobhunter.managed"] = "browser-relay"
    result["Config"]["Entrypoint"] = ["/usr/bin/tini"]
    result["Config"]["Cmd"] = relay_command("172.30.77.10", 1800)
    result["HostConfig"].update({"ReadonlyRootfs": True, "Memory": 128 * 1024 ** 2, "NanoCpus": 500_000_000,
                                "PidsLimit": 64, "NetworkMode": RELAY_NETWORK})
    result["Mounts"] = []
    result["NetworkSettings"]["Networks"][RELAY_NETWORK] = {}
    result["NetworkSettings"]["Ports"] = {
        "6080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32000"}],
        "9223/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32001"}],
    }
    return result


class Docker:
    def __init__(self, root):
        self.containers = {}
        self.relays = {}
        self.calls = []
        self.internal = True
        self.firewall = True
        self.root = root

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        command = args[1:]
        result, code = "", 0
        if args[0] == "/usr/sbin/iptables":
            code = 0 if self.firewall else 1
        elif command[:2] == ["network", "inspect"]:
            target = command[2]
            result = json.dumps([{"Name": target, "Internal": self.internal if target == NETWORK else False, "Driver": "bridge", "IPAM": {"Config": [{"Subnet": "172.30.77.0/24"}]}}])
        elif command[:2] == ["network", "connect"]:
            pass
        elif command[:2] == ["container", "ls"]:
            relay = "label=jobhunter.managed=browser-relay" in command
            result = "\n".join((RELAY_PREFIX if relay else CONTAINER_PREFIX) + profile for profile in (self.relays if relay else self.containers))
        elif command[:2] == ["container", "inspect"]:
            relay = command[-1].startswith(RELAY_PREFIX)
            profile = command[-1].removeprefix(RELAY_PREFIX if relay else CONTAINER_PREFIX)
            values = self.relays if relay else self.containers
            result, code = (json.dumps([values[profile]]), 0) if profile in values else ("", 1)
        elif command[:2] == ["container", "rm"]:
            relay = command[-1].startswith(RELAY_PREFIX)
            (self.relays if relay else self.containers).pop(command[-1].removeprefix(RELAY_PREFIX if relay else CONTAINER_PREFIX))
        elif command[:2] == ["container", "run"]:
            name = command[command.index("--name") + 1]
            relay = name.startswith(RELAY_PREFIX)
            profile = name.removeprefix(RELAY_PREFIX if relay else CONTAINER_PREFIX)
            expiry = next(item.split("=", 1)[1] for item in command if item.startswith("jobhunter.expires_at="))
            if relay:
                self.relays[profile] = relay_container(profile, float(expiry))
                self.relays[profile]["Config"]["Cmd"] = command[command.index("jobhunter-browser:local") + 1:]
            else:
                self.containers[profile] = container(profile, float(expiry), root=self.root)
            result = "synthetic-container-id"
        else:
            raise AssertionError("unexpected docker operation: " + repr(command))
        return SimpleNamespace(returncode=code, stdout=result, stderr="never-display-docker-stderr")


@pytest.fixture
def broker(tmp_path):
    root = tmp_path.resolve() / "data"
    (root / "service").mkdir(parents=True)
    (root / "u11").mkdir()
    (root / "u22").mkdir()
    with sqlite3.connect(root / "service" / "registry.sqlite3") as db:
        db.execute("CREATE TABLE members(user_id INTEGER, profile_id TEXT, status TEXT)")
        db.executemany("INSERT INTO members VALUES(?,?,?)", [(11, "u11", "active"), (22, "u22", "active"), (33, "u33", "pending")])
    docker = Docker(root)
    return BrowserBroker(BrokerConfig(root), runner=docker, owner_uid=os.getuid(), lock_path=root / "service" / "browser.lock")


def test_browser_start_uses_only_fixed_isolation_flags_and_candidate_volume(broker):
    session = broker.execute("start", "u11")
    assert session["profile_id"] == "u11"
    assert session["viewer_port"] == 32000 and session["cdp_port"] == 32001
    args, kwargs = next(call for call in broker._runner.calls if call[0][1:3] == ["container", "run"])
    for flag in ("--memory=1g", "--cpus=2", "--pids-limit=256", "--shm-size=256m", "--pull=never"):
        assert flag in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert args[args.index("--network") + 1] == NETWORK
    assert "--publish" not in args
    assert args[args.index("--mount") + 1] == f"type=bind,src={broker.config.data_root}/u11/browser-profile,dst=/data"
    assert f"type=bind,src={broker.config.data_root}/u11/output,dst=/documents,readonly" in args
    assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "HOME=/data" in args
    assert args[-1] == "jobhunter-browser:local"
    assert "--privileged" not in args and "docker.sock" not in " ".join(args)
    assert set(kwargs["env"]) == {"PATH", "LANG"}
    assert (broker.config.data_root / "u11" / "browser-profile").stat().st_mode & 0o777 == 0o700
    relay_args = next(call[0] for call in broker._runner.calls if call[0][1:3] == ["container", "run"] and RELAY_PREFIX + "u11" in call[0])
    assert "127.0.0.1::6080" in relay_args and "127.0.0.1::9223" in relay_args
    assert "--read-only" in relay_args and "--mount" not in relay_args


def test_second_candidate_cannot_share_or_reuse_first_browser(broker):
    first = broker.execute("start", "u11")
    with pytest.raises(BrokerFailure) as error:
        broker.execute("start", "u22")
    assert error.value.code == "capacity"
    assert broker.execute("start", "u11") == first
    assert set(broker._runner.containers) == {"u11"}


def test_stop_keeps_browser_profile_for_future_login(broker):
    broker.execute("start", "u11")
    profile = broker.config.data_root / "u11" / "browser-profile"
    (profile / "synthetic-cookie-store").write_text("synthetic-only")
    assert broker.execute("stop", "u11") == {"profile_id": "u11", "status": "stopped"}
    assert (profile / "synthetic-cookie-store").exists()
    assert not broker._runner.containers
    assert not broker._runner.relays


def test_expired_browser_is_reaped_before_new_candidate_starts(broker):
    broker._runner.containers["u11"] = container(expiry=time.time() - 1, root=broker.config.data_root)
    assert broker.execute("start", "u22")["profile_id"] == "u22"
    assert set(broker._runner.containers) == {"u22"}


def test_revoked_candidate_loses_browser_on_status(broker):
    broker.execute("start", "u11")
    with sqlite3.connect(broker.config.data_root / "service" / "registry.sqlite3") as db:
        db.execute("UPDATE members SET status='revoked' WHERE user_id=11")
    assert broker.execute("status", "u11")["status"] == "stopped"
    assert not broker._runner.containers


@pytest.mark.parametrize("profile", ["u33", "u99"])
def test_inactive_or_missing_registration_cannot_start(broker, profile):
    with pytest.raises(BrokerFailure) as error:
        broker.execute("start", profile)
    assert error.value.code == "inactive"
    assert not broker._runner.calls


@pytest.mark.parametrize("profile", ["../u11", "u11; id", "u0", "u-1", "u11/../u22", "--privileged", 11, None])
def test_profile_ids_cannot_inject_paths_or_commands(broker, profile):
    with pytest.raises(BrokerFailure, match="profile"):
        broker.execute("start", profile)
    assert not broker._runner.calls


def test_symlinked_profile_is_rejected_without_launch(broker, tmp_path):
    (broker.config.data_root / "u11").rmdir()
    (broker.config.data_root / "u11").symlink_to(tmp_path)
    with pytest.raises(BrokerFailure, match="configuration"):
        broker.execute("start", "u11")
    assert not any(call[0][1:3] == ["container", "run"] for call in broker._runner.calls)


def test_symlinked_browser_volume_is_rejected(broker, tmp_path):
    (broker.config.data_root / "u11" / "browser-profile").symlink_to(tmp_path)
    with pytest.raises(BrokerFailure, match="configuration"):
        broker.execute("start", "u11")


def test_unrelated_container_with_matching_name_is_not_removed(broker):
    other = container()
    other["Config"]["Labels"] = {}
    broker._runner.containers["u11"] = other
    with pytest.raises(BrokerFailure, match="container"):
        broker.execute("stop", "u11")
    assert "u11" in broker._runner.containers
    assert not any(call[0][1:3] == ["container", "rm"] for call in broker._runner.calls)


def test_browser_cannot_start_on_network_with_unrestricted_egress(broker):
    broker._runner.internal = False
    with pytest.raises(BrokerFailure, match="network"):
        broker.execute("start", "u11")
    assert not broker._runner.containers


def test_browser_cannot_start_without_host_firewall_boundary(broker):
    broker._runner.firewall = False
    with pytest.raises(BrokerFailure, match="firewall"):
        broker.execute("start", "u11")
    assert not broker._runner.containers


def test_existing_browser_with_public_port_is_rejected(broker):
    broker.execute("start", "u11")
    broker._runner.relays["u11"]["NetworkSettings"]["Ports"]["6080/tcp"][0]["HostIp"] = "0.0.0.0"
    with pytest.raises(BrokerFailure, match="container"):
        broker.execute("status", "u11")


def test_existing_browser_on_extra_network_is_rejected(broker):
    broker.execute("start", "u11")
    broker._runner.containers["u11"]["NetworkSettings"]["Networks"]["host-access"] = {}
    with pytest.raises(BrokerFailure, match="container"):
        broker.execute("status", "u11")


def test_relay_cannot_forward_to_another_candidate_or_mount_files(broker):
    broker.execute("start", "u11")
    broker._runner.relays["u11"]["Config"]["Cmd"][-1] = "172.30.77.99"
    with pytest.raises(BrokerFailure, match="container"):
        broker.execute("status", "u11")


def test_orphan_relay_is_removed_before_allocating_new_browser(broker):
    broker._runner.relays["u11"] = relay_container()
    assert broker.execute("start", "u22")["profile_id"] == "u22"
    assert set(broker._runner.relays) == {"u22"}


def test_docker_errors_do_not_propagate_host_details(broker):
    broker._runner = Mock(side_effect=subprocess.TimeoutExpired(["docker", "secret"], 35, stderr="private-cookie-value"))
    with pytest.raises(BrokerFailure) as error:
        broker.execute("start", "u11")
    assert str(error.value) == "docker"


def test_manager_calls_only_fixed_helper_with_sanitized_environment():
    payload = {"profile_id": "u11", "status": "running", "viewer_port": 32000, "cdp_port": 32001, "expires_at": time.time() + 1000}
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload)))
    assert DockerBrowserManager(runner=runner).start("u11") == payload
    args, kwargs = runner.call_args.args[0], runner.call_args.kwargs
    assert args == ["/usr/bin/sudo", "-n", "--", BROKER_HELPER, "start", "u11"]
    assert set(kwargs["env"]) == {"PATH", "LANG"}


def test_manager_capacity_failure_has_useful_safe_message():
    runner = Mock(return_value=SimpleNamespace(returncode=1, stdout='{"error":"capacity"}', stderr="private details"))
    with pytest.raises(BrowserCapacityError, match="retry"):
        DockerBrowserManager(runner=runner).start("u22")


def test_manager_rejects_wrong_candidate_browser():
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"profile_id":"u22","status":"stopped"}'))
    with pytest.raises(BrowserError):
        DockerBrowserManager(runner=runner).status("u11")


@pytest.mark.parametrize("field,value", [("viewer_port", 80), ("cdp_port", True), ("expires_at", float("inf")), ("expires_at", time.time() - 1), ("cookie", "never-return")])
def test_manager_rejects_invalid_session_metadata(field, value):
    payload = {"profile_id": "u11", "status": "running", "viewer_port": 32000, "cdp_port": 32001, "expires_at": time.time() + 1000}
    payload[field] = value
    with pytest.raises(BrowserError):
        validate_browser_result(payload, "u11")


def test_root_config_permissions_and_paths_are_validated(tmp_path):
    root = tmp_path.resolve()
    docker = root / "docker"
    docker.write_text("#!/bin/false\n")
    docker.chmod(0o755)
    config = root / "browser.json"
    config.write_text(json.dumps({"data_root": str(root), "docker_path": str(docker)}))
    config.chmod(0o600)
    parsed = load_config(config, owner_uid=os.getuid())
    assert parsed.data_root == root and parsed.ttl_seconds == 1800
    config.chmod(0o666)
    with pytest.raises(BrokerFailure, match="configuration"):
        load_config(config, owner_uid=os.getuid())


def test_root_config_cannot_be_replaced_by_symlink(tmp_path):
    root = tmp_path.resolve()
    real = root / "real.json"
    real.write_text("{}")
    link = root / "browser.json"
    link.symlink_to(real)
    with pytest.raises(BrokerFailure, match="configuration"):
        load_config(link, owner_uid=os.getuid())
