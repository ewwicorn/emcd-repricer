"""
Configuration models and YAML loader for the EMCD P2P Repricer.

Schema
------
The top-level YAML file contains global settings (``interval_min``,
``interval_max``, ``price_step``, ``dry_run``) and a list of ``accounts``.
Each account has its own credentials, optional proxy list, and a list of
offers to manage.

Offer-level ``price_step`` overrides the global default when present.
"""

from dataclasses import dataclass, field
from typing import List

import yaml


@dataclass
class OfferConfig:
    """Configuration for a single P2P offer to be managed by the repricer."""

    offer_id:      str
    """UUID of the offer on EMCD."""

    crypto:        str
    """Cryptocurrency code, lower-cased (e.g. ``"usdt"``, ``"btc"``)."""

    fiat:          str
    """Fiat currency code, lower-cased (e.g. ``"rub"``, ``"usd"``)."""

    direction:     str
    """Trade direction from the taker's perspective: ``"buy"`` or ``"sell"``."""

    ignored_users: List[str] = field(default_factory=list)
    """
    Usernames (lower-cased) whose offers are skipped when computing the
    top-of-book price.  Typically includes the account's own username so
    the bot does not compete with itself.
    """

    price_step:    float = 0.01
    """
    Amount added on top of the best market price when setting a new rate.
    Overrides the global ``price_step`` when specified at offer level.
    """

    interval_min:  int = None
    """
    Minimum sleep time (seconds) between reprice cycles for this offer.
    When not specified, falls back to the global ``interval_min`` value.
    """

    interval_max:  int = None
    """
    Maximum sleep time (seconds) between reprice cycles for this offer.
    When not specified, falls back to the global ``interval_max`` value.
    """
    
    round_to_zeros: bool = False
    """
    When ``True``, round the new price to the nearest "clean" number with
    trailing zeros.
    """


@dataclass
class AccountConfig:
    """Credentials and offers for one EMCD account."""

    name:     str
    """Logical account name used in logs and session file names."""

    email:    str
    """EMCD login email."""

    password: str
    """EMCD login password."""

    offers:   List[OfferConfig] = field(default_factory=list)
    """Offers managed by this account."""

    proxies:  List[str] = field(default_factory=list)
    """
    Optional proxy URLs for this account.  Supported formats:

    * ``http://ip:port``
    * ``http://user:pass@ip:port``
    * ``socks5://ip:port``
    """


@dataclass
class AppConfig:
    """Global application settings loaded from the YAML config file."""

    accounts:     List[AccountConfig]
    """All accounts to run concurrently."""

    interval_min: int
    """Minimum sleep time (seconds) between reprice cycles."""

    interval_max: int
    """Maximum sleep time (seconds) between reprice cycles."""

    price_step:   float
    """
    Global default price step.  Individual offers can override this value
    via their own ``price_step`` field.
    """

    dry_run:      bool
    """
    When ``True``, the repricer logs intended changes but does not send
    any price-update requests to the API.  Useful for testing.
    """


def load_config(path: str) -> AppConfig:
    """
    Load and validate the repricer configuration from a YAML file.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A fully populated ``AppConfig`` instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        KeyError:          If required fields are missing.
    """
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    global_step = raw.get("price_step", 0.01)

    accounts: List[AccountConfig] = []
    for acc_raw in raw["accounts"]:
        offers = [
            OfferConfig(
                offer_id=str(o["offer_id"]),
                crypto=o["crypto"].lower(),
                fiat=o["fiat"].lower(),
                direction=o["direction"].lower(),
                ignored_users=[u.lower() for u in (o.get("ignored_users") or [])],
                price_step=o.get("price_step", global_step),
                interval_min=o.get("interval_min"),
                interval_max=o.get("interval_max"),
                round_to_zeros=o.get("round_to_zeros", False),
            )
            for o in acc_raw.get("offers", [])
        ]

        raw_proxies = acc_raw.get("proxies") or []
        accounts.append(
            AccountConfig(
                name=acc_raw["name"],
                email=acc_raw["email"],
                password=acc_raw["password"],
                offers=offers,
                proxies=raw_proxies if isinstance(raw_proxies, list) else [],
            )
        )

    return AppConfig(
        accounts=accounts,
        interval_min=raw.get("interval_min", 10),
        interval_max=raw.get("interval_max", 20),
        price_step=global_step,
        dry_run=raw.get("dry_run", False),
    )
