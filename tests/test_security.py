"""Security regression tests: pipe authorization, per-user binding, TPM wrapping, clamps."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

if sys.platform != "win32":  # pragma: no cover - project is Windows only
    pytest.skip("Windows only", allow_module_level=True)

from engine.ipc_server import ClientIdentity, handle_request
from engine.store import (
    ProfileStore,
    clamp_service_threshold,
    load_service_config,
)
from engine.tpm import TPM_BLOB_PREFIX, TpmUnavailableError

DIM = 8


def _emb(seed: float = 0.1) -> list[float]:
    return [seed + i * 0.01 for i in range(DIM)]


def _user(name: str = "alice", sid: str = "S-1-5-21-1-1-1-1001", **kw) -> ClientIdentity:
    return ClientIdentity(sid=sid, username=name, domain="PC", **kw)


SYSTEM = ClientIdentity(sid="S-1-5-18", username="SYSTEM", domain="NT AUTHORITY", is_system=True, privileged=True)


class FakeTpm:
    """Reversible stand-in for the NCrypt TPM key (never touches the real TPM)."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.wrapped = 0

    def wrap(self, plaintext: bytes) -> bytes:
        if self.fail:
            raise TpmUnavailableError("no TPM")
        self.wrapped += 1
        return b"W" + plaintext[::-1]

    def unwrap(self, ciphertext: bytes) -> bytes:
        assert ciphertext.startswith(b"W")
        return ciphertext[1:][::-1]


# ---- TPM wrapping ----------------------------------------------------------------------


def test_tpm_wraps_password_and_never_stores_plaintext(tmp_path: Path) -> None:
    tpm = FakeTpm()
    path = tmp_path / "profiles.enc"
    store = ProfileStore(path, local_machine=False, use_tpm=True, tpm_protector=tpm)
    store.upsert_embeddings("alice", [_emb()], append=False)
    store.set_credentials("alice", "S3cret!pw", domain=".")
    assert tpm.wrapped == 1

    reloaded = ProfileStore(path, local_machine=False, use_tpm=True, tpm_protector=tpm)
    profile = reloaded.get_user("alice")
    assert profile is not None
    assert profile.password == ""
    assert profile.password_blob.startswith(TPM_BLOB_PREFIX)
    assert profile.has_password
    assert profile.resolve_password(tpm) == "S3cret!pw"


def test_tpm_failure_falls_back_to_dpapi_only(tmp_path: Path) -> None:
    path = tmp_path / "profiles.enc"
    store = ProfileStore(path, local_machine=False, use_tpm=True, tpm_protector=FakeTpm(fail=True))
    store.upsert_embeddings("bob", [_emb()], append=False)
    store.set_credentials("bob", "pw", domain=".")
    profile = ProfileStore(path, local_machine=False, use_tpm=False).get_user("bob")
    assert profile is not None
    assert profile.password == "pw"
    assert profile.password_blob == ""
    assert profile.resolve_password() == "pw"


# ---- threshold clamp -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [(0.1, 0.60), (0.55, 0.60), (0.0, 0.60), (0.99, 0.80), (0.62, 0.62), (float("nan"), None)],
)
def test_clamp_service_threshold(value: float, expected: float | None) -> None:
    out = clamp_service_threshold(value)
    assert 0.60 <= out <= 0.80
    if expected is not None:
        assert out == pytest.approx(expected)


# ---- pipe authorization ----------------------------------------------------------------


def test_unprivileged_caller_cannot_verify_cancel_or_enumerate(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.enc", local_machine=False)
    store.upsert_embeddings("alice", [_emb()], append=False)
    called = []

    def verifier(**kw):
        called.append(kw)
        return {"status": "ok"}

    user = _user()
    assert handle_request({"cmd": "verify", "username": "alice"}, store, verifier, client=user)["reason"] == "forbidden"
    assert handle_request({"cmd": "cancel"}, store, client=user)["reason"] == "forbidden"
    assert not called
    ping = handle_request({"cmd": "ping"}, store, client=user)
    assert ping["status"] == "ok" and "enrolled_users" not in ping

    sys_ping = handle_request({"cmd": "ping"}, store, client=SYSTEM)
    assert sys_ping["enrolled_users"] == ["alice"]


def test_management_requires_identity_and_binds_to_caller(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.enc", local_machine=False)
    assert handle_request({"cmd": "profile_get"}, store)["reason"] == "forbidden"  # no identity
    assert handle_request({"cmd": "profile_get"}, store, client=SYSTEM)["reason"] == "forbidden"

    alice = _user("alice")
    r = handle_request({"cmd": "profile_enroll", "embeddings": [_emb()], "append": False}, store, client=alice)
    assert r["status"] == "ok"
    r = handle_request({"cmd": "profile_set_password", "password": "pw", "domain": "."}, store, client=alice)
    assert r["status"] == "ok"

    got = handle_request({"cmd": "profile_get"}, store, client=alice)
    assert got["profile"]["username"] == "alice"
    assert got["profile"]["has_password"] is True
    assert "pw" not in json.dumps(got)  # password never leaves the service

    # A different account that reuses the name (e.g. renamed / recreated) is rejected by SID.
    impostor = _user("alice", sid="S-1-5-21-9-9-9-1001")
    assert handle_request({"cmd": "profile_get"}, store, client=impostor)["reason"] == "forbidden"
    assert handle_request({"cmd": "profile_delete"}, store, client=impostor)["reason"] == "forbidden"

    # Bob only sees / touches his own (empty) profile.
    bob = _user("bob", sid="S-1-5-21-1-1-1-1002")
    assert handle_request({"cmd": "profile_get"}, store, client=bob)["profile"] is None
    assert handle_request({"cmd": "profile_delete"}, store, client=bob)["deleted"] is False
    assert store.get_user("alice") is not None


def test_enroll_validation_limits(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.enc", local_machine=False)
    alice = _user()
    too_many = [_emb(i / 100) for i in range(33)]
    r = handle_request({"cmd": "profile_enroll", "embeddings": too_many, "append": False}, store, client=alice)
    assert r["reason"] == "bad_request"

    batch = [_emb(i / 100) for i in range(20)]
    assert handle_request({"cmd": "profile_enroll", "embeddings": batch, "append": False}, store, client=alice)["status"] == "ok"
    r = handle_request({"cmd": "profile_enroll", "embeddings": batch, "append": True}, store, client=alice)
    assert r["reason"] == "too_many_samples"

    bad = handle_request({"cmd": "profile_enroll", "embeddings": [[float("nan")] * DIM]}, store, client=alice)
    assert bad["status"] != "ok"

    assert handle_request({"cmd": "profile_enroll", "embeddings": [_emb()], "append": False}, store, client=alice)["status"] == "ok"
    mismatch = handle_request({"cmd": "profile_enroll", "embeddings": [[0.1] * (DIM + 1)], "append": True}, store, client=alice)
    assert mismatch["status"] != "ok"


def test_config_set_requires_admin_and_clamps(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.enc", local_machine=False)
    cfg_path = tmp_path / "config.json"
    msg = {"cmd": "config_set", "threshold": 0.05, "require_motion": True, "require_open_eyes": False}

    standard = _user()
    assert handle_request(msg, store, client=standard, config_path=cfg_path)["reason"] == "forbidden"
    assert not cfg_path.exists()

    admin = _user(is_admin_member=True)
    r = handle_request(msg, store, client=admin, config_path=cfg_path)
    assert r["status"] == "ok"
    cfg = load_service_config(cfg_path)
    assert cfg.threshold == pytest.approx(0.60)
    assert cfg.require_motion is True

    non_bool = dict(msg, require_motion="yes")
    assert handle_request(non_bool, store, client=admin, config_path=cfg_path)["status"] != "ok"

    # Anyone may read the (non-secret) config.
    assert handle_request({"cmd": "config_get"}, store, client=standard, config_path=cfg_path)["status"] == "ok"


def test_set_password_rejected_when_windows_refuses_it(tmp_path: Path, monkeypatch) -> None:
    import engine.ipc_server as ipc

    seen = []

    def fake_validate(username, domain, password):
        seen.append((username, domain))
        return "invalid_password"

    monkeypatch.setattr(ipc, "_validate_logon_password", fake_validate)
    store = ProfileStore(tmp_path / "p.enc", local_machine=False)
    alice = _user()
    handle_request({"cmd": "profile_enroll", "embeddings": [_emb()], "append": False}, store, client=alice)
    r = handle_request({"cmd": "profile_set_password", "password": "wrong", "domain": "."}, store, client=alice)
    assert r["reason"] == "invalid_password"
    assert seen == [("alice", ".")]
    assert not store.get_user("alice").has_password


def test_logon_validator_maps_windows_errors(monkeypatch) -> None:
    import pywintypes
    import engine.ipc_server as ipc

    monkeypatch.undo()  # drop the autouse stub so the real validator runs

    def failing(code):
        def _logon(*_a):
            raise pywintypes.error(code, "LogonUser", "x")
        return _logon

    monkeypatch.setattr(ipc.win32security, "LogonUser", failing(1326))
    assert ipc._validate_logon_password("a", ".", "p") == "invalid_password"
    monkeypatch.setattr(ipc.win32security, "LogonUser", failing(1909))
    assert ipc._validate_logon_password("a", ".", "p") == "account_locked"
    monkeypatch.setattr(ipc.win32security, "LogonUser", failing(1385))  # logon type not granted
    assert ipc._validate_logon_password("a", ".", "p") is None


def test_service_client_rejects_pipe_server_outside_session0(monkeypatch) -> None:
    import engine.service_client as sc

    client = sc.ServiceProfileClient()
    monkeypatch.setattr(sc.win32pipe, "GetNamedPipeServerSessionId", lambda _h: 1)
    with pytest.raises(sc.ServiceUnavailableError, match="not in session 0"):
        client._verify_server(object())


# ---- looks (enrollment passes) ---------------------------------------------------------


def test_looks_track_enrollment_passes_and_delete_one(tmp_path: Path) -> None:
    path = tmp_path / "p.enc"
    store = ProfileStore(path, local_machine=False)
    store.upsert_embeddings("alice", [_emb(0.1)] * 3, append=False)
    store.upsert_embeddings("alice", [_emb(0.5)] * 2, append=True)
    profile = ProfileStore(path, local_machine=False).get_user("alice")
    assert [l["count"] for l in profile.looks] == [3, 2]

    first = profile.looks[0]["id"]
    after = store.delete_look("alice", first)
    assert len(after.embeddings) == 2
    assert after.embeddings[0][0] == pytest.approx(0.5)
    assert [l["count"] for l in after.looks] == [2]

    with pytest.raises(ValueError):
        store.delete_look("alice", after.looks[0]["id"])  # refuse to drop the last look
    with pytest.raises(KeyError):
        store.delete_look("alice", "nope")

    store.upsert_embeddings("alice", [_emb()] * 4, append=False)
    assert [l["count"] for l in store.get_user("alice").looks] == [4]


def test_legacy_profile_without_looks_is_split_into_chunks(tmp_path: Path) -> None:
    from engine.store import UserProfile

    legacy = UserProfile.from_dict({"username": "bob", "embeddings": [_emb()] * 20})
    assert [l["count"] for l in legacy.looks] == [8, 8, 4]
    assert legacy.looks[0]["id"] == "legacy-0"
    bad = UserProfile.from_dict(
        {"username": "bob", "embeddings": [_emb()] * 5, "looks": [{"id": "a", "count": 2}]}
    )
    assert [l["count"] for l in bad.looks] == [5]


def test_delete_look_over_pipe_is_bound_to_caller(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.enc", local_machine=False)
    alice = _user()
    handle_request({"cmd": "profile_enroll", "embeddings": [_emb()] * 2, "append": False}, store, client=alice)
    r = handle_request({"cmd": "profile_enroll", "embeddings": [_emb()] * 3, "append": True}, store, client=alice)
    looks = r["profile"]["looks"]
    assert [l["count"] for l in looks] == [2, 3]

    impostor = _user("alice", sid="S-1-5-21-9-9-9-1001")
    msg = {"cmd": "profile_delete_look", "look_id": looks[0]["id"]}
    assert handle_request(msg, store, client=impostor)["reason"] == "forbidden"
    assert handle_request(msg, store, client=SYSTEM)["reason"] == "forbidden"

    r = handle_request(msg, store, client=alice)
    assert r["status"] == "ok" and len(r["profile"]["embeddings"]) == 3
    assert handle_request(msg, store, client=alice)["reason"] == "not_found"
    last = {"cmd": "profile_delete_look", "look_id": r["profile"]["looks"][0]["id"]}
    assert handle_request(last, store, client=alice)["reason"] == "last_look"
    assert handle_request({"cmd": "profile_delete_look", "look_id": 5}, store, client=alice)["reason"] == "bad_request"


def test_purge_data_requires_yes_and_removes_data_dir(tmp_path, monkeypatch, capsys):
    import engine.store as store_mod
    import engine.tpm as tpm_mod
    from engine import cli

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "profiles.enc").write_bytes(b"secret")
    monkeypatch.setattr(store_mod, "get_default_store_path", lambda: data_dir / "profiles.enc")
    calls = []
    monkeypatch.setattr(tpm_mod.TpmKeyProtector, "delete_key", lambda self: calls.append(self.key_name) or True)

    assert cli.main(["purge-data"]) == 2
    assert data_dir.exists() and not calls

    assert cli.main(["purge-data", "--yes"]) == 0
    assert not data_dir.exists()
    assert calls == [tpm_mod.DEFAULT_KEY_NAME]
