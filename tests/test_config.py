from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from homevitals.config import load_config


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.dump(data))


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_single_user(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert len(cfg.users) == 1
    assert cfg.users[0].name == "default"


# ---------------------------------------------------------------------------
# Household rules
# ---------------------------------------------------------------------------

def _household_user(name, customer_id="cid", garmin_email=None, **extra):
    user = {
        "name": name,
        "eufy": {"email": "scale@example.com", "password": "pw"},
        "garmin": {"email": garmin_email or f"{name.lower()}@example.com", "password": "pw"},
    }
    if customer_id is not None:
        user["eufy"]["customer_id"] = customer_id
    user.update(extra)
    return user


def _store_household_passwords(names):
    from homevitals.credentials import store_password
    for name in names:
        store_password(f"{name}:eufy", "pw")
        store_password(f"{name}:garmin", "pw")


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_two_users_with_customer_ids_load(_keyring, fixtures):
    _store_household_passwords(["Chris", "Jane"])
    cfg = load_config(fixtures.path("config_household.yaml"))
    assert [u.name for u in cfg.users] == ["Chris", "Jane"]
    assert [u.eufy.customer_id for u in cfg.users] == ["adult-a-0001", "adult-b-0002"]
    assert [u.garmin.email for u in cfg.users] == ["adult-a@example.com", "adult-b@example.com"]
    assert {u.eufy.email for u in cfg.users} == {"scale@example.com"}


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_two_users_missing_customer_id_fails_naming_the_user(_keyring, fixtures):
    _store_household_passwords(["Chris", "Jane"])
    with pytest.raises(ValueError, match=r"User 'Jane' has no eufy\.customer_id"):
        load_config(fixtures.path("config_household_missing_customer_id.yaml"))


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_duplicate_user_names_rejected(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {"users": [_household_user("Chris", "a"), _household_user("chris", "b", "other@example.com")]})
    with pytest.raises(ValueError, match="User names must be unique"):
        load_config(path)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_blank_user_name_rejected(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    user = _household_user("Chris", "a")
    user["name"] = "  "
    _write(path, {"users": [user]})
    with pytest.raises(ValueError, match="Every user needs a name"):
        load_config(path)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_two_users_same_customer_id_rejected(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {"users": [_household_user("Chris", "same-0001"), _household_user("Jane", "same-0001")]})
    with pytest.raises(ValueError, match=r"linked to the same Eufy profile \(\.\.\.0001\)"):
        load_config(path)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_two_users_same_garmin_email_rejected_case_insensitive(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {"users": [
        _household_user("Chris", "a", "Shared@Example.com"),
        _household_user("Jane", "b", " shared@example.com "),
    ]})
    with pytest.raises(ValueError, match="both use the Garmin account"):
        load_config(path)


@pytest.mark.parametrize("section", ["strava", "zwift"])
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_multi_user_rejects_strava_and_zwift_sections(_keyring, tmp_path: Path, section):
    path = tmp_path / "config.yaml"
    extra = {section: {"client_id": "1", "client_secret": "s"} if section == "strava"
             else {"email": "z@example.com", "password": "pw"}}
    _write(path, {"users": [_household_user("Chris", "a", **extra), _household_user("Jane", "b")]})
    with pytest.raises(ValueError, match=f"has a '{section}' section"):
        load_config(path)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_single_user_without_customer_id_still_loads(_keyring, fixtures):
    _store_household_passwords(["default"])
    cfg = load_config(fixtures.path("config_legacy_single_user.yaml"))
    assert len(cfg.users) == 1
    assert cfg.users[0].name == "default"
    assert cfg.users[0].eufy.customer_id is None


def test_validate_household_users_is_pure():
    from homevitals.config import validate_household_users

    users = [_household_user("Chris", "a"), _household_user("Jane", None)]
    with patch("homevitals.credentials.get_password", side_effect=AssertionError("vault touched")):
        with pytest.raises(ValueError, match="has no eufy.customer_id"):
            validate_household_users(users, "config.yaml")
        validate_household_users([_household_user("Chris", "a"), _household_user("Jane", "b")], "config.yaml")


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_household_rules_run_before_password_lookup(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    users = [_household_user("Chris", "a"), _household_user("Jane", None)]
    for u in users:
        del u["eufy"]["password"], u["garmin"]["password"]
    _write(path, {"users": users})
    # No passwords stored anywhere: the household rule must fail first, not the password lookup.
    with pytest.raises(ValueError, match="has no eufy.customer_id"):
        load_config(path)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_parses_customer_id(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw", "customer_id": "cust-42"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].eufy.customer_id == "cust-42"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_customer_id_defaults_none(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].eufy.customer_id is None


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_customer_id_coerced_to_str(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw", "customer_id": 12345},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].eufy.customer_id == "12345"


# --- Empty or shapeless config -----------------------------------------------
#
# A config that is not a mapping with a non-empty users list used to die on
# AttributeError ('NoneType' object has no attribute 'get') or a bare KeyError.
# Both callers print the exception text, so the message has to name the file
# and the way out.


@pytest.mark.parametrize("content", [
    "",
    "   \n\n   \n",
    "- one\n- two\n",
    "sync_interval_minutes: 15\n",
    "users: []\n",
    "users:\n",
    "users: not-a-list\n",
])
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_rejects_shapeless_document(_keyring, tmp_path: Path, content: str):
    path = tmp_path / "config.yaml"
    path.write_text(content)

    with pytest.raises(ValueError) as exc:
        load_config(path)

    message = str(exc.value)
    assert str(path) in message
    assert "homevitals" in message


# --- Strava client secret ----------------------------------------------------
#
# The Strava API app's client secret used to live in plain text in config.yaml,
# unlike every other secret. It now resolves from the credential store, with
# the YAML value still honored for configs that have not been migrated yet.


def _strava_config(secret: str | None) -> dict:
    strava: dict = {"client_id": "12345"}
    if secret is not None:
        strava["client_secret"] = secret
    return {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "strava": strava,
        }],
    }


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_resolves_strava_secret_from_vault(_keyring, tmp_path: Path):
    from homevitals.credentials import store_password

    store_password("default:strava", "vault-secret")
    path = tmp_path / "config.yaml"
    _write(path, _strava_config(None))

    cfg = load_config(path)
    assert cfg.users[0].strava.client_secret == "vault-secret"
    assert cfg.users[0].strava.client_id == "12345"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_falls_back_to_yaml_strava_secret(_keyring, tmp_path: Path):
    """A config that has not been migrated yet must keep working."""
    path = tmp_path / "config.yaml"
    _write(path, _strava_config("yaml-secret"))

    cfg = load_config(path)
    assert cfg.users[0].strava.client_secret == "yaml-secret"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_prefers_vault_strava_secret_over_yaml(_keyring, tmp_path: Path):
    from homevitals.credentials import store_password

    store_password("default:strava", "vault-secret")
    path = tmp_path / "config.yaml"
    _write(path, _strava_config("yaml-secret"))

    cfg = load_config(path)
    assert cfg.users[0].strava.client_secret == "vault-secret"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_missing_strava_secret_names_setup_strava(_keyring, tmp_path: Path):
    """--update-password prompts for the Eufy and Garmin account passwords and
    would never fix this, so the message must name --setup-strava instead."""
    path = tmp_path / "config.yaml"
    _write(path, _strava_config(None))

    with pytest.raises(ValueError, match="--setup-strava"):
        load_config(path)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_allows_zwift_only_and_resolves_password(_keyring, tmp_path: Path):
    from homevitals.credentials import store_password

    store_password("default:zwift", "vault-password")
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "eufy-password"},
            "zwift": {"email": "z@example.com"},
        }],
    })

    cfg = load_config(path)
    assert cfg.users[0].garmin is None
    assert cfg.users[0].strava is None
    assert cfg.users[0].zwift.email == "z@example.com"
    assert cfg.users[0].zwift.password == "vault-password"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_load_config_zwift_password_env_fallback(_keyring, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ZWIFT_TEST_PASSWORD", "environment-password")
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "eufy-password"},
            "zwift": {"email": "z@example.com", "password": "${ZWIFT_TEST_PASSWORD}"},
        }],
    })

    assert load_config(path).users[0].zwift.password == "environment-password"



@patch("homevitals.credentials._keyring_available", return_value=False)
def test_customer_id_is_stripped_like_the_validator_compares_it(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {"users": [_household_user("Chris", "  adult-a-0001 "), _household_user("Jane", "adult-b-0002")]})
    cfg = load_config(path)
    assert cfg.users[0].eufy.customer_id == "adult-a-0001"


# ---------------------------------------------------------------------------
# The omron section
# ---------------------------------------------------------------------------

def _omron_users(chris_omron, jane_omron=None):
    chris = _household_user("Chris", "a", "chris@example.com")
    jane = _household_user("Jane", "b", "jane@example.com")
    if chris_omron is not None:
        chris["omron"] = chris_omron
    if jane_omron is not None:
        jane["omron"] = jane_omron
    return [chris, jane]


def _load_omron(tmp_path: Path, chris_omron, jane_omron=None):
    from homevitals.credentials import store_password
    path = tmp_path / "config.yaml"
    _write(path, {"users": _omron_users(chris_omron, jane_omron)})
    store_password("Chris:omron", "omron-pw")
    store_password("Jane:omron", "omron-pw-2")
    return load_config(path)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_valid_omron_section_loads(_keyring, tmp_path: Path):
    cfg = _load_omron(tmp_path, {"email": "chris@example.com", "country": "CA"})
    assert len(cfg.users) == 2


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_section_loads_into_omron_config(_keyring, tmp_path: Path):
    from homevitals.config import OmronConfig
    cfg = _load_omron(tmp_path, {"email": "  chris@example.com ", "country": "CA"})
    assert cfg.users[0].omron == OmronConfig(email="chris@example.com", password="omron-pw", country="CA",
                                             server=None, user_number=None)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_user_without_omron_has_omron_none(_keyring, tmp_path: Path):
    cfg = _load_omron(tmp_path, {"email": "chris@example.com", "country": "CA"})
    assert cfg.users[1].omron is None


@pytest.mark.parametrize("section", [{"country": "CA"}, {"email": "  ", "country": "CA"}, "not-a-mapping", "", []])
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_section_requires_email(_keyring, tmp_path: Path, section):
    with pytest.raises(ValueError, match="User 'Chris' has an omron section without an email"):
        _load_omron(tmp_path, section)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_email_must_look_like_an_address(_keyring, tmp_path: Path):
    with pytest.raises(ValueError, match="has an omron.email that is not an email address"):
        _load_omron(tmp_path, {"email": "chris", "country": "CA"})


@pytest.mark.parametrize("country", [None, "", "C", "CAN", "C1", 12])
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_section_requires_two_letter_country(_keyring, tmp_path: Path, country):
    section = {"email": "chris@example.com"}
    if country is not None:
        section["country"] = country
    with pytest.raises(ValueError, match="has no valid omron.country"):
        _load_omron(tmp_path, section)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_country_is_normalised_to_upper_case(_keyring, tmp_path: Path):
    cfg = _load_omron(tmp_path, {"email": "chris@example.com", "country": " ca "})
    assert cfg.users[0].omron.country == "CA"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_v1_country_is_refused_without_server_override(_keyring, tmp_path: Path):
    with pytest.raises(ValueError, match="use OMRON's older server system"):
        _load_omron(tmp_path, {"email": "chris@example.com", "country": "JP"})


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_v1_country_allowed_with_server_override(_keyring, tmp_path: Path):
    cfg = _load_omron(tmp_path, {"email": "chris@example.com", "country": "JP", "server": "eu"})
    assert cfg.users[0].omron.server == "eu"


@pytest.mark.parametrize("server,expected", [("na", "na"), ("EU", "eu"),
                                             ("https://example.invalid/prd/", "https://example.invalid/prd")])
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_server_override_accepts_region_keys_and_https_urls(_keyring, tmp_path: Path, server, expected):
    cfg = _load_omron(tmp_path, {"email": "chris@example.com", "country": "QA", "server": server})
    assert cfg.users[0].omron.server == expected


@pytest.mark.parametrize("server", ["ftp://x", "asia", "https://a b", "http://example.invalid", 5])
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_server_override_rejects_garbage(_keyring, tmp_path: Path, server):
    with pytest.raises(ValueError, match="has an invalid omron.server"):
        _load_omron(tmp_path, {"email": "chris@example.com", "country": "QA", "server": server})


@pytest.mark.parametrize("value", [0, -1, 100, "x", "", True, 1.5, "2.0"])
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_user_number_must_be_a_whole_number_from_1(_keyring, tmp_path: Path, value):
    with pytest.raises(ValueError, match="has an invalid omron.user_number"):
        _load_omron(tmp_path, {"email": "chris@example.com", "country": "CA", "user_number": value})


@pytest.mark.parametrize("value,expected", [(1, 1), (2, 2), ("2", 2), (3, 3), (" 4 ", 4), (99, 99)])
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_user_number_accepts_any_monitor_user(_keyring, tmp_path: Path, value, expected):
    cfg = _load_omron(tmp_path, {"email": "chris@example.com", "country": "CA", "user_number": value})
    assert cfg.users[0].omron.user_number == expected


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_two_users_sharing_an_omron_account_are_rejected_case_insensitive(_keyring, tmp_path: Path):
    with pytest.raises(ValueError, match="both use the OMRON connect account"):
        _load_omron(tmp_path, {"email": "Shared@Example.com", "country": "CA"},
                    {"email": "shared@example.com ", "country": "CA"})


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_missing_password_names_user_and_service(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {"users": _omron_users({"email": "chris@example.com", "country": "CA"})})
    with pytest.raises(ValueError, match="No omron password found for user 'Chris'"):
        load_config(path)


def test_omron_validation_runs_before_password_lookup():
    from homevitals.config import validate_household_users
    users = _omron_users({"email": "chris@example.com", "country": "XYZ"})
    with patch("homevitals.credentials.get_password", side_effect=AssertionError("vault touched")):
        with pytest.raises(ValueError, match="has no valid omron.country"):
            validate_household_users(users, "config.yaml")


def test_omron_validation_also_applies_to_a_single_user():
    from homevitals.config import validate_household_users
    with pytest.raises(ValueError, match="has no valid omron.country"):
        validate_household_users([dict(_household_user("Chris", None), omron={"email": "c@example.com"})], "c.yaml")


# ---------------------------------------------------------------------------
# Partly set up people
# ---------------------------------------------------------------------------

def _store_for(names_services):
    from homevitals.credentials import store_password
    for name, services in names_services.items():
        for service in services:
            store_password(f"{name}:{service}", "pw")


_PARTIAL_PASSWORDS = {"Chris": ("eufy", "garmin", "omron"), "Jane": ("garmin",), "Sam": ("eufy",)}


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_user_without_eufy_section_loads_with_eufy_none(_keyring, fixtures):
    _store_for(_PARTIAL_PASSWORDS)
    cfg = load_config(fixtures.path("config_household_partial.yaml"))
    jane = cfg.users[1]
    assert jane.name == "Jane" and jane.eufy is None and jane.garmin.email == "adult-b@example.com"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_eufy_only_user_loads_with_garmin_none(_keyring, fixtures):
    _store_for(_PARTIAL_PASSWORDS)
    sam = load_config(fixtures.path("config_household_partial.yaml")).users[2]
    assert sam.garmin is None and sam.eufy.customer_id == "adult-b-0002"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_name_only_user_loads_with_everything_none(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {"users": [{"name": "Jane"}]})
    (user,) = load_config(path).users
    assert (user.eufy, user.garmin, user.omron, user.strava, user.zwift) == (None, None, None, None, None)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_two_users_customer_id_required_only_for_users_with_eufy(_keyring, fixtures, tmp_path: Path):
    _store_for(_PARTIAL_PASSWORDS)
    assert len(load_config(fixtures.path("config_household_partial.yaml")).users) == 3
    raw = yaml.safe_load(fixtures.text("config_household_partial.yaml"))
    del raw["users"][2]["eufy"]["customer_id"]
    path = tmp_path / "config.yaml"
    _write(path, raw)
    with pytest.raises(ValueError, match="User 'Sam' has no eufy.customer_id"):
        load_config(path)


@pytest.mark.parametrize("section", [None, {}, {"email": ""}, {"email": "  "}, "x"])
def test_eufy_section_without_email_rejected(section):
    from homevitals.config import validate_household_users
    with pytest.raises(ValueError, match="User 'Jane' has an eufy section without an email"):
        validate_household_users([{"name": "Jane", "eufy": section}], "c.yaml")


@pytest.mark.parametrize("section", [None, {}, {"email": ""}])
def test_garmin_section_without_email_rejected(section):
    from homevitals.config import validate_household_users
    with pytest.raises(ValueError, match="User 'Jane' has a garmin section without an email"):
        validate_household_users([{"name": "Jane", "garmin": section}], "c.yaml")


_E = {"email": "scale@example.com", "password": "pw", "customer_id": "a"}
_G = {"email": "g@example.com", "password": "pw"}
_O = {"email": "o@example.com", "country": "CA"}
_S = {"client_id": "1", "client_secret": "s"}


@pytest.mark.parametrize("sections,scale,bp,missing", [
    ({"eufy": _E, "garmin": _G}, True, False, []),
    ({"eufy": _E, "garmin": _G, "omron": _O}, True, True, []),
    ({"omron": _O, "garmin": _G}, False, True, []),
    ({"eufy": _E}, False, False, ["Garmin"]),
    ({"omron": _O}, False, False, ["Garmin"]),
    ({"eufy": _E, "omron": _O}, False, False, ["Garmin"]),
    ({"garmin": _G}, False, False, ["the scale or the blood pressure monitor"]),
    ({}, False, False, ["Garmin", "the scale or the blood pressure monitor"]),
    ({"eufy": _E, "strava": _S}, True, False, []),
    ({"omron": _O, "strava": _S}, False, False, ["Garmin"]),
])
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_readiness_table(_keyring, tmp_path: Path, sections, scale, bp, missing):
    from homevitals.config import bp_ready, missing_for_sync, nothing_connected, scale_ready
    from homevitals.credentials import store_password
    store_password("Jane:omron", "pw")
    path = tmp_path / "config.yaml"
    _write(path, {"users": [dict({"name": "Jane"}, **sections)]})
    (user,) = load_config(path).users
    assert (scale_ready(user), bp_ready(user), missing_for_sync(user)) == (scale, bp, missing)
    assert nothing_connected(user) is (sections == {})


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_legacy_single_user_without_customer_id_is_scale_ready(_keyring, fixtures):
    from homevitals.config import scale_ready
    _store_household_passwords(["default"])
    (user,) = load_config(fixtures.path("config_legacy_single_user.yaml")).users
    assert user.eufy.customer_id is None and scale_ready(user)


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_no_sync_targets_rule_is_gone(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {"users": [{"name": "Sam", "eufy": {"email": "scale@example.com", "password": "pw"}}]})
    assert load_config(path).users[0].garmin is None


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_omron_without_garmin_loads_but_is_not_bp_ready(_keyring, tmp_path: Path):
    from homevitals.config import bp_ready
    from homevitals.credentials import store_password
    path = tmp_path / "config.yaml"
    user = _household_user("Chris", "a")
    del user["garmin"]
    user["omron"] = {"email": "chris@example.com", "country": "CA"}
    _write(path, {"users": [user]})
    store_password("Chris:omron", "pw")
    cfg = load_config(path)
    assert cfg.users[0].garmin is None
    assert bp_ready(cfg.users[0]) is False
