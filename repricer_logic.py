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
import math
import random

import httpx

from client import EmcdP2PClient
from config import AccountConfig, AppConfig, OfferConfig

from decimal import Decimal, ROUND_DOWN, ROUND_UP


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
    def round_to_clean(price, step, direction):
        """Round the price down to the nearest multiple of step, ensuring "clean" numbers."""
        price_d = Decimal(str(price))
        step_d = Decimal(str(step))
        
        result = (price_d / step_d).quantize(Decimal('1'), rounding=ROUND_DOWN if direction=="sell" else ROUND_UP) * step_d
        return float(result)
    
    
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

        if math.isclose(my_price - top_price, step, rel_tol=1e-9, abs_tol=1e-9)  or (my_price - top_price < step and my_price - top_price > 0):
            log.info("%s already at target: mine=%.8g market_top=%.8g", label, my_price, top_price)
            return
        
        if my_price > top_price:
            log.info("%s already at top: mine=%.8g market_top=%.8g", label, my_price, top_price)

            new_price = round(top_price + step, 8)
            
            if offer.round_to_zeros:
                 new_price = round_to_clean(new_price, step, "sell")
            
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
            return

        new_price = round(top_price + step, 8)
        if offer.round_to_zeros:
            new_price = round_to_clean(new_price, step, "sell")
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

        if math.isclose(top_price - my_price, step, rel_tol=1e-9, abs_tol=1e-9) or (top_price - my_price < step and top_price - my_price > 0):
            log.info("%s already at target: mine=%.8g market_top=%.8g", label, my_price, top_price)
            return


        if my_price < top_price:
            log.info("%s already at top: mine=%.8g market_top=%.8g", label, my_price, top_price)
            new_price = round(top_price - step, 8)
            
            if offer.round_to_zeros:
                new_price = round_to_clean(new_price, step, "buy")
            
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
            return

        new_price = round(top_price - step, 8)
        if offer.round_to_zeros:
            new_price = round_to_clean(new_price, step, "buy")
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


async def run_offer_cycle(
    client:  EmcdP2PClient,
    offer:   OfferConfig,
    cfg:     AppConfig,
) -> None:
    """
    Run continuous reprice loop for a single offer, independently.

    Each offer gets its own async task that runs indefinitely, repricing
    according to its own intervals (or global defaults if not specified).

    Args:
        client: Authenticated EMCD API client for the account.
        offer:  Offer configuration.
        cfg:    Global application settings.
    """
    log = client.log
    label = f"{offer.crypto.upper()}/{offer.fiat.upper()}"

    try:
        while True:
            try:
                await reprice_offer(client, offer, offer.price_step, cfg.dry_run)
            except httpx.HTTPStatusError as exc:
                log.error("[%s] HTTP error: %s", label, exc)
            except Exception as exc:
                log.error("[%s] Unexpected error: %s", label, exc, exc_info=True)

            # Use per-offer intervals if configured, otherwise fall back to global
            interval_min = offer.interval_min if offer.interval_min is not None else cfg.interval_min
            interval_max = offer.interval_max if offer.interval_max is not None else cfg.interval_max
            pause = random.uniform(interval_min, interval_max)
            log.info("[%s] Sleeping %.1fs before next reprice.", label, pause)
            await asyncio.sleep(pause)
    except asyncio.CancelledError:
        log.info("[%s] Task cancelled.", label)
        raise


async def run_account(account: AccountConfig, cfg: AppConfig) -> None:
    """
    Entry point for a single account's reprice loop.

    Creates an API client, ensures a valid session (loading from disk or
    launching the browser if needed), then launches independent async tasks
    for each configured offer. Each offer reprices according to its own
    interval settings, allowing true parallelism.

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
        # Create independent concurrent tasks for each offer
        tasks = [run_offer_cycle(client, offer, cfg) for offer in account.offers]
        await asyncio.gather(*tasks)

    finally:
        await client.close()
