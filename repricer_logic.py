"""
Repricer business logic — account runner and per-offer price update cycle.

This module sits between the main entry-point (emcd_repricer.py) and the
low-level API client (client.py).  It is responsible for:

* Instantiating one EmcdP2PClient per configured account.
* Running the continuous reprice loop for all offers of that account.
* Delegating individual price decisions to ``reprice_offer()``.
"""

import asyncio
import logging
import random

import httpx

from client import EmcdP2PClient
from config import AccountConfig, AppConfig, OfferConfig


async def reprice_offer(
    client:  EmcdP2PClient,
    offer:   OfferConfig,
    step:    float,
    dry_run: bool,
) -> None:
    """
    Check and, if necessary, update the price of a single offer.

    Decision logic
    --------------
    1. Fetch the current top-of-book price for the offer's trading pair,
       excluding any ignored users.
    2. Fetch the offer's current price from the account.
    3. If the offer is already at or above ``top_price + step``, do nothing.
    4. Otherwise, set the new price to ``top_price + step`` and call the
       update API (or log only when ``dry_run`` is ``True``).

    Args:
        client:  Authenticated EMCD API client for the account.
        offer:   Offer configuration (ID, pair, ignored users, step).
        step:    Price increment above the market top to set.
        dry_run: When ``True``, log the intended change but skip the API call.
    """
    log = client.log

    top_price = await client.get_top_price(
        crypto=offer.crypto,
        fiat=offer.fiat,
        direction=offer.direction,
        ignored_users=offer.ignored_users,
    )
    
    if offer.direction == "sell":
        if top_price is None:
            return

        my_offer = await client.get_my_offer(offer.offer_id)
        if my_offer is None:
            return

        my_price = float(my_offer.get("price") or my_offer.get("rate") or 0)
        label    = f"{offer.crypto.upper()}/{offer.fiat.upper()}"

        if my_price > top_price:
            log.info("%s already at top: mine=%.8g market_top=%.8g", label, my_price, top_price)
            return

        new_price = round(top_price + step, 8)
        log.info(
            "%s repricing: %.8g → %.8g (market_top=%.8g)",
            label, my_price, new_price, top_price,
        )

        if dry_run:
            log.info("  [dry_run] update skipped.")
            return

        ok = await client.update_offer_price(offer.offer_id, new_price)
        if ok:
            log.info("  OK → %.8g", new_price)
        else:
            log.error("  FAILED to update offer %s", offer.offer_id)
            
    if offer.direction == "buy":
        if top_price is None:
            return

        my_offer = await client.get_my_offer(offer.offer_id)
        if my_offer is None:
            return

        my_price = float(my_offer.get("price") or my_offer.get("rate") or 0)
        label    = f"{offer.crypto.upper()}/{offer.fiat.upper()}"

        if my_price < top_price:
            log.info("%s already at top: mine=%.8g market_top=%.8g", label, my_price, top_price)
            return

        new_price = round(top_price - step, 8)
        log.info(
            "%s repricing: %.8g → %.8g (market_top=%.8g)",
            label, my_price, new_price, top_price,
        )

        if dry_run:
            log.info("  [dry_run] update skipped.")
            return

        ok = await client.update_offer_price(offer.offer_id, new_price)
        if ok:
            log.info("  OK → %.8g", new_price)
        else:
            log.error("  FAILED to update offer %s", offer.offer_id)


async def run_account(account: AccountConfig, cfg: AppConfig) -> None:
    """
    Entry point for a single account's reprice loop.

    Creates an API client, ensures a valid session (loading from disk or
    launching the browser if needed), then loops indefinitely — repricing
    every configured offer and sleeping for a random interval between cycles.

    The random sleep interval (between ``cfg.interval_min`` and
    ``cfg.interval_max`` seconds) helps avoid rate-limiting by spreading
    requests over time.

    Args:
        account: Account credentials and offer list.
        cfg:     Global application settings (intervals, dry_run flag, …).
    """
    log    = logging.getLogger(account.name)
    client = EmcdP2PClient(account)

    # Ensure we have a valid session before entering the main loop.
    # EmcdP2PClient._load_session() already ran in __init__; login() is a
    # no-op if the loaded session is still valid.
    try:
        await client.login()
    except Exception as exc:
        log.error("Authentication failed: %s", exc)
        await client.close()
        return

    log.info("Running. Offers configured: %d", len(account.offers))

    try:
        while True:
            for offer in account.offers:
                try:
                    await reprice_offer(client, offer, offer.price_step, cfg.dry_run)
                except httpx.HTTPStatusError as exc:
                    log.error("HTTP error for offer %s: %s", offer.offer_id, exc)
                except Exception as exc:
                    log.error(
                        "Unexpected error for offer %s: %s",
                        offer.offer_id, exc, exc_info=True,
                    )

            pause = random.uniform(cfg.interval_min, cfg.interval_max)
            log.debug("Sleeping %.1fs before next cycle.", pause)
            await asyncio.sleep(pause)

    finally:
        await client.close()
