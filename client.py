"""
EMCD P2P API Client — async HTTP client with automatic session management.

Authentication flow:
  1. On startup, load saved tokens and cookies from disk.
  2. If access_token is missing or expired, attempt a silent refresh via
     the EMCD refresh-token endpoint (no browser needed).
  3. Only if the silent refresh fails, launch a headless Chromium browser
     via Playwright, perform a full login, and capture the resulting
     cookie jar.
  4. The full cookie jar (not just the two auth cookies) is persisted to
     disk so the next run can skip the browser entirely.
  5. Before every outgoing request, proactively refresh the access_token
     when fewer than TOKEN_REFRESH_THRESHOLD seconds remain.
  6. On an unexpected 401, retry once after a silent refresh and fall
     back to the browser only as a last resort.

Proxy support:
  Multiple proxies can be provided per account.  The client rotates to
  the next proxy after MAX_PROXY_ERRORS consecutive connection failures.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import jwt
from playwright.async_api import async_playwright, expect

from config import AccountConfig

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

BASE_URL = "https://endpoint.emcd.io"
AUTH_BASE_URL = "https://emcd.io"          # Auth endpoints live on the main domain
TOKENS_DIR = Path(".tokens")

# Number of milliseconds between simulated key-presses in Playwright
_TYPING_DELAY_MS = 100

# Proactively refresh the access_token this many seconds before expiry
TOKEN_REFRESH_THRESHOLD = 0            # 1 day (tokens last ~7 days, so refresh only on day 6-7)

# Rotate to the next proxy after this many consecutive connection errors
MAX_PROXY_ERRORS = 3

# Default browser login timeout (milliseconds)
_LOGIN_TIMEOUT_MS = 120_000               # 2 minutes (enough for 2-FA)

_DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "origin": "https://emcd.io",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_proxy_url(proxy_url: str) -> Tuple[str, Optional[Dict]]:
    """
    Parse a proxy URL into formats understood by httpx and Playwright.

    Accepted schemes: ``http``, ``https``, ``socks5``.
    Credentials (``user:pass@``) are optional.

    Args:
        proxy_url: Raw proxy URL, e.g. ``socks5://user:pass@1.2.3.4:1080``.

    Returns:
        A 2-tuple ``(httpx_proxy_url, playwright_proxy_dict)``.

    Raises:
        ValueError: If the URL cannot be parsed or is missing host/port.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(proxy_url)
        scheme   = parsed.scheme.lower()
        hostname = parsed.hostname
        port     = parsed.port
        username = parsed.username
        password = parsed.password

        if not hostname or not port:
            raise ValueError(f"Missing host or port in proxy URL: {proxy_url!r}")

        auth_prefix = f"{username}:{password}@" if username else ""
        httpx_proxy = f"{scheme}://{auth_prefix}{hostname}:{port}"

        playwright_proxy: Dict = {"server": f"{scheme}://{hostname}:{port}"}
        if username:
            playwright_proxy["username"] = username
        if password:
            playwright_proxy["password"] = password

        return httpx_proxy, playwright_proxy

    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to parse proxy URL {proxy_url!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class EmcdP2PClient:
    """
    Async API client for the EMCD P2P exchange.

    Manages authentication, token refresh, proxy rotation, and all API
    calls needed by the repricer.
    """

    def __init__(self, account: AccountConfig) -> None:
        """
        Initialise the client from an account config.

        Sets up the httpx session, parses proxy list, and attempts to
        load persisted tokens/cookies from disk.

        Args:
            account: Validated account configuration (email, password,
                     offer list, proxy list).
        """
        self._email          = account.email
        self._password       = account.password
        self._account_name   = account.name

        self.log = logging.getLogger(account.name)

        # --- Token state ---
        self._token:         Optional[str]   = None
        self._refresh_token: Optional[str]   = None
        self._token_expiry:  Optional[float] = None

        # Serialised cookies captured from the browser, keyed by name.
        # Each entry is a dict with keys: name, value, domain, path.
        self._saved_cookies: List[Dict] = []

        # Lock prevents two coroutines from launching the browser at once.
        self._login_lock = asyncio.Lock()

        # --- Proxy state ---
        self._raw_proxies:     List[str]              = account.proxies or []
        self._parsed_proxies:  List[Tuple[str, Dict]] = []
        self._current_proxy_index = 0
        self._proxy_error_count   = 0

        for raw in self._raw_proxies:
            try:
                self._parsed_proxies.append(_parse_proxy_url(raw))
            except ValueError as exc:
                self.log.error("Proxy parse error: %s", exc)

        TOKENS_DIR.mkdir(exist_ok=True)

        # --- httpx session ---
        self._client = self._build_http_client()

        # Restore session from disk (no network call).
        self._load_session()

    # ------------------------------------------------------------------
    # httpx client construction
    # ------------------------------------------------------------------

    def _build_http_client(self) -> httpx.AsyncClient:
        """
        Create a fresh httpx.AsyncClient with the current proxy and headers.

        Returns:
            A configured, ready-to-use async HTTP client.
        """
        kwargs: Dict = {
            "base_url": BASE_URL,
            "headers":  dict(_DEFAULT_HEADERS),
            "timeout":  15.0,
        }

        if self._parsed_proxies:
            httpx_proxy, _ = self._parsed_proxies[self._current_proxy_index]
            kwargs["proxy"] = httpx_proxy
            self.log.info(
                "Using proxy [%d/%d]: %s",
                self._current_proxy_index + 1,
                len(self._parsed_proxies),
                self._raw_proxies[self._current_proxy_index],
            )

        return httpx.AsyncClient(**kwargs)

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def _session_file(self) -> Path:
        """Return the path to this account's persisted session file."""
        return TOKENS_DIR / f"{self._account_name}.json"

    def _load_session(self) -> bool:
        """
        Load cookies from disk.

        Restores the full cookie jar to the httpx client so the session
        behaves identically to a regular browser session.

        Returns:
            ``True`` if cookies were found and loaded; ``False``
            otherwise (a re-login will be needed before the first request).
        """
        path = self._session_file()
        if not path.exists():
            self.log.debug("No saved session found at %s", path)
            return False

        try:
            with path.open() as fh:
                data = json.load(fh)

            self._saved_cookies = data.get("cookies", [])

            if self._saved_cookies:
                # Extract token from cookies for use in headers
                for cookie in self._saved_cookies:
                    if cookie["name"] == "auth__access_token":
                        self._token = cookie["value"]
                        break
                
                self._apply_session_to_client()
                self.log.info(
                    "Session loaded — %d cookies restored, token extracted", len(self._saved_cookies)
                )
                return True

            self.log.info("No cookies found in saved session; will login on first request.")
            return False

        except Exception as exc:
            self.log.debug("Failed to load session: %s", exc)
            return False

    def _save_session(self) -> None:
        """
        Persist cookies to disk.

        The saved file is account-scoped (filename = account name).
        """
        try:
            data = {
                "cookies":  self._saved_cookies,
                "saved_at": time.time(),
            }
            with self._session_file().open("w") as fh:
                json.dump(data, fh, indent=2)
            self.log.debug("Session saved to %s (cookies=%d)", self._session_file(), len(self._saved_cookies))
        except Exception as exc:
            self.log.error("Failed to save session: %s", exc)

    def _apply_session_to_client(self) -> None:
        """
        Push the full cookie jar into the httpx client and set auth headers.

        This must be called every time ``_saved_cookies`` changes so the
        next HTTP request carries fresh credentials.

        Key detail: cookies are set with the *exact* domain/path values
        captured from the browser (e.g. ``.emcd.io``), which ensures they
        are forwarded to ``endpoint.emcd.io`` just as a real browser would.
        We also extract the token and send it as a header for API auth.
        """
        # Clear any old cookies first
        self._client.cookies.clear()
        
        # Restore the complete cookie jar from the saved list and extract token
        for cookie in self._saved_cookies:
            self._client.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
            # Extract token from auth cookie
            if cookie["name"] == "auth__access_token":
                self._token = cookie["value"]
        
        # Set token as header for API authentication
        if self._token:
            self._client.headers["x-access-token"] = self._token

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    def _parse_token_expiry(self) -> None:
        """
        Decode the access_token JWT and store its expiry timestamp.

        Signature verification is intentionally skipped — we only need
        the ``exp`` claim to schedule proactive refreshes.
        """
        if not self._token:
            return
        try:
            payload = jwt.decode(
                self._token, options={"verify_signature": False}
            )
            self._token_expiry = payload.get("exp")
            if self._token_expiry:
                self.log.debug(
                    "Access token expires in %.0fs",
                    self._token_expiry - time.time(),
                )
        except Exception as exc:
            self.log.debug("Could not decode JWT: %s", exc)
            self._token_expiry = None

    def _session_valid(self) -> bool:
        """
        Return ``True`` when cookies are present and session is ready.
        """
        return bool(self._saved_cookies)

    # ------------------------------------------------------------------
    # Browser-based login
    # ------------------------------------------------------------------

    async def login(self) -> None:
        """
        Authenticate via a real Chromium browser (Playwright).

        Called automatically when:
        * no persisted session exists, or
        * silent token refresh has failed.

        The asyncio Lock prevents multiple concurrent coroutines from each
        opening their own browser window.

        After a successful login the *complete* cookie jar is saved to disk
        so subsequent runs can reuse the full browser session and avoid
        triggering 2-FA or CAPTCHA.
        """
        async with self._login_lock:
            # Another coroutine may have already logged in by the time we
            # acquire the lock — re-check before opening a browser.
            if self._session_valid():
                return

            self.log.info("Launching browser for login...")

            async with async_playwright() as pw:
                launch_kwargs: Dict = {"headless": False}
                if self._parsed_proxies:
                    _, playwright_proxy = self._parsed_proxies[self._current_proxy_index]
                    launch_kwargs["proxy"] = playwright_proxy
                    self.log.info(
                        "Browser using proxy: %s",
                        self._raw_proxies[self._current_proxy_index],
                    )

                browser = await pw.chromium.launch(**launch_kwargs)
                context = await browser.new_context(
                    user_agent=_DEFAULT_HEADERS["user-agent"]
                )
                page = await context.new_page()

                # ---- Navigate to login page ----
                await page.goto("https://emcd.io/auth/login")

                btn_email = page.locator(
                    '//*[@id="app"]/div/div/div[2]/div/div[2]/button'
                )
                await expect(btn_email).to_be_visible()
                await btn_email.click()

                inp_email = page.locator('//*[@id="login-input-email"]')
                await expect(inp_email).to_be_visible()
                await inp_email.type(self._email, delay=_TYPING_DELAY_MS)

                inp_pass = page.locator('//*[@id="login-input-password"]')
                await expect(inp_pass).to_be_visible()
                await inp_pass.type(self._password, delay=_TYPING_DELAY_MS)

                btn_login = page.locator(
                    '//*[@id="app"]/div/div/div[2]/div/div[1]/div[2]/form/div[4]/button'
                )
                await expect(btn_login).to_be_enabled()
                await btn_login.click()

                # ---- Wait for post-login redirect ----
                def _left_login(url: str) -> bool:
                    return "/auth/login" not in url

                try:
                    await page.wait_for_url(_left_login, timeout=_LOGIN_TIMEOUT_MS)
                    self.log.info("Login successful.")
                except Exception:
                    self.log.warning(
                        "Login timeout — complete 2-FA or CAPTCHA manually in the "
                        "browser window. Waiting another 2 minutes..."
                    )
                    try:
                        await page.wait_for_url(_left_login, timeout=_LOGIN_TIMEOUT_MS)
                        self.log.info("Login successful after manual input.")
                    except Exception:
                        await browser.close()
                        raise RuntimeError(
                            "Login stuck on /auth/login after 4 minutes. "
                            "Please log in manually at https://emcd.io first."
                        )

                # ---- Navigate to API endpoint to ensure auth cookies are set ----
                # The API endpoint might set domain-specific auth cookies
                self.log.debug("Navigating to API endpoint to capture auth cookies...")
                try:
                    await page.goto(
                        "https://endpoint.emcd.io/p2p/v1/offer/list?"
                        "crypto_currency=usdt&fiat_currency=rub&direction=buy&limit=1",
                        wait_until="networkidle",
                        timeout=10000,
                    )
                except Exception as e:
                    self.log.debug("API endpoint navigation timeout (expected): %s", e)

                # ---- Extract and validate cookies ----
                # Save the full cookie jar; we work only with cookies, not tokens.
                raw_cookies = await context.cookies()
                self._saved_cookies = [
                    {
                        "name":   c["name"],
                        "value":  c["value"],
                        "domain": c["domain"],
                        "path":   c["path"],
                    }
                    for c in raw_cookies
                ]
                
                # Extract auth token from cookies
                for c in raw_cookies:
                    if c["name"] == "auth__access_token":
                        self._token = c["value"]
                        break

                # Verify that critical auth token was found
                has_auth_token = self._token is not None

                await browser.close()

                if not has_auth_token:
                    raise RuntimeError(
                        "No auth__access_token found after login. "
                        "Check the token at https://emcd.io."
                    )

                self._apply_session_to_client()
                self._save_session()

                self.log.info(
                    "Authenticated successfully. Saved %d cookies.",
                    len(self._saved_cookies),
                )

    # ------------------------------------------------------------------
    # Proxy rotation
    # ------------------------------------------------------------------

    def _rotate_proxy(self) -> bool:
        """
        Switch to the next proxy in the configured list.

        Returns:
            ``True`` if a new proxy was selected; ``False`` if only one
            proxy (or none) is configured.
        """
        if len(self._parsed_proxies) <= 1:
            self.log.warning("No alternative proxy available for rotation.")
            return False

        self._current_proxy_index = (
            (self._current_proxy_index + 1) % len(self._parsed_proxies)
        )
        self._proxy_error_count = 0
        self.log.warning(
            "Rotated to proxy [%d/%d]: %s",
            self._current_proxy_index + 1,
            len(self._parsed_proxies),
            self._raw_proxies[self._current_proxy_index],
        )
        return True

    async def _rebuild_http_client(self) -> None:
        """
        Close the current httpx client and build a new one using the
        currently selected proxy, then restore auth credentials.
        """
        await self._client.aclose()
        self._client = self._build_http_client()
        self._apply_session_to_client()

    # ------------------------------------------------------------------
    # Core request executor
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """
        Execute an authenticated HTTP request with automatic login and proxy rotation on failure.

        Strategy
        --------
        1. Ensure session is valid (cookies are loaded), otherwise login via browser.
        2. Send the request. The full cookie jar is already attached to the httpx client.
        3. After every response, capture any server-side cookie updates.
        4. On HTTP 401: re-login via browser and retry once.
        5. On connection/proxy errors: rotate the proxy and retry.

        Args:
            method: HTTP verb (``"GET"``, ``"POST"``, …).
            path:   API path relative to BASE_URL.
            **kwargs: Forwarded to ``httpx.AsyncClient.request()``.

        Returns:
            A successful ``httpx.Response`` (2xx).

        Raises:
            httpx.HTTPStatusError: For unrecoverable non-2xx responses.
            httpx.ConnectError:    When all proxy attempts are exhausted.
        """
        # Step 1 — ensure we have a valid session (cookies loaded).
        if not self._session_valid():
            await self.login()

        max_attempts = max(len(self._parsed_proxies), 1)
        auth_retried = False

        for attempt in range(max_attempts):
            try:
                resp = await self._client.request(method, path, **kwargs)
                self._proxy_error_count = 0

                # Step 3 — capture any server-side cookie updates.
                self._absorb_response_cookies(resp)

                # Step 4 — handle 401 Unauthorized.
                if resp.status_code == 401 and not auth_retried:
                    auth_retried = True
                    self.log.warning("Received 401 — re-logging in via browser.")
                    await self.login()
                    await self._rebuild_http_client()

                    resp = await self._client.request(method, path, **kwargs)
                    self._absorb_response_cookies(resp)

                resp.raise_for_status()
                return resp

            except (
                httpx.ConnectError,
                httpx.ProxyError,
                httpx.TimeoutException,
            ) as exc:
                self._proxy_error_count += 1
                self.log.warning(
                    "Connection error (%d/%d): %s",
                    self._proxy_error_count, MAX_PROXY_ERRORS, type(exc).__name__,
                )

                if (
                    attempt < max_attempts - 1
                    and self._proxy_error_count >= MAX_PROXY_ERRORS
                ):
                    if self._rotate_proxy():
                        await self._rebuild_http_client()
                        continue

                if attempt == max_attempts - 1:
                    self.log.error("All connection attempts exhausted.")
                    raise

        raise httpx.ConnectError("All proxies exhausted.")

    def _absorb_response_cookies(self, resp: httpx.Response) -> None:
        """
        After each response, capture any updated cookies and sync to session.

        EMCD may rotate cookies via Set-Cookie headers. httpx updates its
        internal cookie jar automatically; this method reads the updated jar
        and syncs our saved session when cookies change.

        Args:
            resp: The HTTP response to inspect (used indirectly via the
                  client's cookie jar, which httpx updates in-place).
        """
        # Check if any critical auth cookies have changed in the jar
        for cookie_name in ("auth__access_token", "auth__refresh_token"):
            current_value = self._client.cookies.get(cookie_name)
            if not current_value:
                continue

            # Find this cookie in our saved list
            found = False
            for saved in self._saved_cookies:
                if saved["name"] == cookie_name:
                    if saved["value"] != current_value:
                        self.log.info("Server rotated %s — updating session.", cookie_name)
                        saved["value"] = current_value
                        self._save_session()
                    found = True
                    break

            # If cookie is present in jar but not in our saved list, add it
            if not found:
                self.log.info("Server issued new %s — updating session.", cookie_name)
                self._saved_cookies.append({
                    "name": cookie_name,
                    "value": current_value,
                    "domain": ".emcd.io",
                    "path": "/",
                })
                self._save_session()

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP client and release all resources."""
        await self._client.aclose()

    async def get_top_price(
        self,
        crypto:        str,
        fiat:          str,
        direction:     str,
        ignored_users: Optional[List[str]] = None,
        limit:         int = 15,
    ) -> Optional[float]:
        """
        Return the best market price for a trading pair, skipping offers
        from ignored users.

        The EMCD offer-list response includes a ``profile_map`` dict that
        maps each maker's UUID to their username, so no additional API
        call is required for username resolution.

        Args:
            crypto:        Cryptocurrency code (``"usdt"``, ``"btc"`` …).
            fiat:          Fiat currency code (``"rub"``, ``"usd"`` …).
            direction:     ``"buy"`` or ``"sell"`` from the taker's perspective.
            ignored_users: Usernames whose offers should be skipped.
            limit:         Number of top offers to fetch; increase when many
                           ignored users are expected near the top of book.

        Returns:
            Rate of the first eligible offer as ``float``, or ``None`` if
            no eligible offers were found.
        """
        ignored_lower = {u.lower() for u in (ignored_users or [])}

        resp = await self._request(
            "GET",
            "/p2p/v1/offer/list",
            params={
                "crypto_currency": crypto,
                "fiat_currency":   fiat,
                "direction":       direction,
                "limit":           limit,
                "offset":          0,
                "kyc_required":    "true",
                "favourite_users": "false",
                "provider_codes":  "",
            },
        )
        data = resp.json()

        offers      = data.get("offers") or data.get("items") or data.get("data") or []
        profile_map = data.get("profile_map") or {}

        if not offers:
            self.log.warning(
                "Empty offer list for %s/%s %s", crypto, fiat, direction
            )
            return None

        self.log.debug("Received %d offers.", len(offers))

        for idx, offer in enumerate(offers):
            maker_id = offer.get("maker_id", "")
            profile  = profile_map.get(maker_id, {}) if profile_map else {}
            username = (profile.get("username") or "").lower()

            if username in ignored_lower:
                self.log.info(
                    "Skipping offer #%d from %r (ignored_users).", idx + 1, username
                )
                continue

            price_raw = offer.get("rate") or offer.get("price") or 0
            self.log.info(
                "Top offer %s/%s %s: price=%.8g, user=%r, rank=%d/%d",
                crypto.upper(), fiat.upper(), direction,
                float(price_raw), username, idx + 1, len(offers),
            )
            return float(price_raw) if price_raw else None

        self.log.warning(
            "All %d fetched offers belong to ignored users. "
            "Consider raising `limit` or narrowing `ignored_users`.",
            len(offers),
        )
        return None

    async def get_my_offer(
        self, offer_id: str, include_inactive: bool = True
    ) -> Optional[dict]:
        """
        Fetch the caller's own offer by ID.

        Checks active (published) offers first.  If not found there and
        ``include_inactive`` is ``True``, also checks unpublished offers.

        Args:
            offer_id:         UUID of the target offer.
            include_inactive: Whether to fall back to unpublished offers
                              when the offer is not found in the active list.

        Returns:
            Raw offer dict from the API, or ``None`` if not found.
        """
        states = [True, False] if include_inactive else [True]

        for published in states:
            try:
                resp = await self._request(
                    "GET",
                    "/p2p/v1/offer/list/my",
                    params={"published": "true" if published else "false"},
                )
                data   = resp.json()
                offers = data.get("offers") or data.get("items") or []

                self.log.debug(
                    "Fetched %d %s offers.",
                    len(offers), "active" if published else "inactive",
                )

                for offer in offers:
                    oid = str(offer.get("id") or offer.get("offer_id") or "")
                    if oid == offer_id:
                        self.log.info(
                            "Offer %s found in %s list.",
                            offer_id, "active" if published else "inactive",
                        )
                        return offer

            except Exception as exc:
                self.log.error("Error fetching my offers: %s", exc)
                return None

        self.log.warning("Offer %s not found.", offer_id)
        return None

    async def update_offer_price(self, offer_id: str, new_price: float) -> bool:
        """
        Update the fixed rate of an existing offer.

        Fetches the full current offer first so that all other fields
        (limits, payment options, description, …) are preserved unchanged.

        Args:
            offer_id:  UUID of the offer to update.
            new_price: New fixed rate to set.

        Returns:
            ``True`` on HTTP 200/201; ``False`` on any failure.
        """
        current = await self.get_my_offer(offer_id)
        if not current:
            self.log.error("Cannot update %s: offer not found.", offer_id)
            return False

        payload = {
            "offer_id":        offer_id,
            "amount":          current.get("amount"),
            "auto_message":    current.get("auto_message", ""),
            "coin_crypto":     current.get("coin_crypto"),
            "coin_fiat":       current.get("coin_fiat"),
            "description":     current.get("description", ""),
            "direction":       current.get("direction"),
            "kyc_only":        current.get("kyc_only", True),
            "max_amount":      current.get("max_amount"),
            "min_amount":      current.get("min_amount"),
            "max_fiat_amount": current.get("max_fiat_amount"),
            "min_fiat_amount": current.get("min_fiat_amount"),
            "payment_options": current.get("payment_options", []),
            "publish":         current.get("publish", False),
            "rate_rule":       {"fixed_rate": new_price},
            "rate_rule_type":  "FIXED_RATE",
            "use_fiat_limits": current.get("use_fiat_limits", True),
        }

        try:
            resp = await self._request(
                "POST", "/p2p/v1/offer/update", json=payload
            )
            if resp.status_code in (200, 201):
                self.log.debug("Offer %s updated to %.8g.", offer_id, new_price)
                return True
            self.log.error(
                "Failed to update offer %s: HTTP %d", offer_id, resp.status_code
            )
            return False
        except Exception as exc:
            self.log.error("Error updating offer %s: %s", offer_id, exc)
            return False
