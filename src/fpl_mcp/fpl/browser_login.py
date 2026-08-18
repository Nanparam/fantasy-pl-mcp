"""Browser-driven FPL login to obtain an OIDC refresh token.

FPL authenticates via PingOne (Ping Identity) "DaVinci" at
account.premierleague.com using OAuth 2.0 Authorization Code + PKCE. The
fantasy web app is a public client that requests the ``offline_access`` scope,
so after login the SPA (oidc-client-ts) persists a **refresh token** in
``localStorage`` under a key like
``oidc.user:https://account.premierleague.com/as:<client_id>``.

Normally the user has to open DevTools and copy that value by hand. This module
automates it: it drives the login form in a headless Chromium via Playwright
(so the Cloudflare / DataDome bot challenge is satisfied by a real browser),
then reads the refresh token — and the manager's team id — straight out of
localStorage.

Credentials are resolved from the system keyring first, then environment
variables, mirroring the rest of the credential handling in this project.
Playwright and keyring are **optional** dependencies; install them with
``pip install "fpl-mcp[login]"`` and ``playwright install chromium``.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from ..config import (
    FPL_APP_BASE_URL,
    FPL_CREDENTIAL_TARGET,
    FPL_ENV_PASSWORD,
    FPL_ENV_USERNAME,
    FPL_OIDC_STORAGE_KEY,
    FPL_USER_AGENT,
)

logger = logging.getLogger(__name__)


def _authorize_url() -> str:
    """Build the OIDC authorize URL (fallback when the app's Log in button
    doesn't render). Uses a fresh PKCE pair and state; the SPA normally does
    this itself, but hitting the endpoint directly still renders the login form.
    """
    import base64
    import hashlib
    import secrets
    from urllib.parse import urlencode

    from ..config import (
        FPL_OIDC_AUTHORITY,
        FPL_OIDC_CLIENT_ID,
        FPL_OIDC_REDIRECT_URI,
        FPL_OIDC_SCOPE,
    )

    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = {
        "client_id": FPL_OIDC_CLIENT_ID,
        "redirect_uri": FPL_OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": FPL_OIDC_SCOPE,
        "state": secrets.token_hex(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "language": "en",
    }
    return f"{FPL_OIDC_AUTHORITY}/authorize?{urlencode(params)}"


@dataclass
class LoginResult:
    """Outcome of a browser login."""

    refresh_token: str
    team_id: Optional[str]
    email: Optional[str]
    profile: dict


class LoginError(Exception):
    """Raised when the browser login cannot obtain a refresh token."""


def resolve_credentials(
    username: Optional[str] = None,
    password: Optional[str] = None,
    target: str = FPL_CREDENTIAL_TARGET,
) -> Tuple[str, str]:
    """Resolve (username, password) from args, then keyring, then env vars.

    Priority when a value is missing:
      1. System keyring under ``target`` (Windows Credential Manager, macOS
         Keychain, SecretService on Linux).
      2. ``FPL_USERNAME`` / ``FPL_PASSWORD`` environment variables.

    Raises LoginError if either value cannot be resolved.
    """
    if username and password:
        return username, password

    # Try keyring (optional dependency).
    try:
        import keyring  # type: ignore

        cred = keyring.get_credential(target, None)
        if cred and cred.username and cred.password:
            username = username or cred.username
            password = password or cred.password
    except ImportError:
        logger.debug("keyring not installed; skipping keyring credential lookup")
    except Exception as exc:  # backend locked/unavailable
        logger.debug("keyring lookup for %r failed: %s", target, exc)

    username = username or os.getenv(FPL_ENV_USERNAME)
    password = password or os.getenv(FPL_ENV_PASSWORD)

    if username and password:
        return username, password

    raise LoginError(
        f"No FPL username/password available. Store them in the keyring for "
        f"target '{target}', set {FPL_ENV_USERNAME}/{FPL_ENV_PASSWORD}, or pass "
        "them explicitly."
    )


def store_login_credentials(
    username: str,
    password: str,
    target: str = FPL_CREDENTIAL_TARGET,
) -> None:
    """Persist a username/password in the system keyring for later logins."""
    try:
        import keyring  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise LoginError(
            "keyring is not installed; run `pip install keyring` or set the "
            f"{FPL_ENV_USERNAME}/{FPL_ENV_PASSWORD} environment variables."
        ) from exc
    keyring.set_password(target, username, password)
    logger.info("Stored FPL login credentials in keyring (target=%s)", target)


def login(
    username: Optional[str] = None,
    password: Optional[str] = None,
    headless: bool = True,
    timeout_ms: int = 60000,
    credential_target: str = FPL_CREDENTIAL_TARGET,
) -> LoginResult:
    """Log in via a real browser and return the OIDC refresh token.

    Args:
        username: FPL account email. If None, resolved from keyring/env.
        password: FPL account password. If None, resolved from keyring/env.
        headless: run Chromium without a visible window (default True). Set
            False to watch the flow or solve a bot challenge interactively.
        timeout_ms: per-step navigation/selector timeout.
        credential_target: keyring target to read credentials from when omitted.

    Returns:
        LoginResult with the refresh_token and, when available, the team id and
        email parsed from the SPA's stored profile.

    Raises:
        LoginError: if Playwright is missing, credentials cannot be resolved, or
            the flow fails / is blocked by a challenge.
    """
    username, password = resolve_credentials(username, password, credential_target)

    try:
        from playwright.sync_api import (  # type: ignore  # noqa: F401
            TimeoutError as PWTimeout,
            sync_playwright,
        )
    except ImportError as exc:  # pragma: no cover
        raise LoginError(
            "Playwright is required for browser login. Install it with "
            "`pip install \"fpl-mcp[login]\"` (or `pip install playwright`) and "
            "then `playwright install chromium`."
        ) from exc

    # Playwright's sync API refuses to run inside a running asyncio loop. When
    # called from async code (the CLI wraps this in asyncio.run), execute the
    # blocking browser work in a dedicated thread that has no event loop.
    try:
        import asyncio

        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if in_loop:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                _login_blocking, username, password, headless, timeout_ms
            ).result()
    return _login_blocking(username, password, headless, timeout_ms)


def _login_blocking(
    username: str,
    password: str,
    headless: bool,
    timeout_ms: int,
) -> LoginResult:
    """Synchronous Playwright login. Must run in a thread with no asyncio loop."""
    from playwright.sync_api import (  # type: ignore
        TimeoutError as PWTimeout,
        sync_playwright,
    )

    app_root = FPL_APP_BASE_URL.rstrip("/") + "/"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=FPL_USER_AGENT)
        page = context.new_page()
        try:
            # Step 1: open the app so its SPA can start the OIDC flow, then click
            # Log in. Going through the app (rather than straight to /as/authorize)
            # lets the SPA generate the PKCE verifier it will need to store the
            # token afterwards.
            page.goto(app_root, wait_until="domcontentloaded", timeout=timeout_ms)
            if "account.premierleague.com" not in page.url:
                # Click Log in with the locator's own generous auto-wait (the SPA
                # renders it a beat after load). If it never appears, fall back to
                # navigating straight to the authorize endpoint.
                try:
                    page.get_by_role("button", name="Log in").click(timeout=30000)
                except PWTimeout:
                    logger.debug("no 'Log in' button; navigating to authorize URL")
                    page.goto(
                        _authorize_url(), wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )

            # Dismiss the OneTrust cookie banner (it can overlay the submit button).
            for label in ("Accept All Cookies", "Accept All", "I Accept"):
                try:
                    btn = page.get_by_role("button", name=label)
                    if btn.count():
                        btn.first.click(timeout=3000)
                        break
                except Exception:
                    pass

            # Detect whether the login form is shown. If the browser is already
            # authenticated (an existing PingOne SSO session), PingOne skips the
            # form and bounces straight back to the app — in that case there's no
            # email field and we go straight to reading the stored token.
            email_box = page.get_by_role("textbox", name="Email Address")
            try:
                email_box.wait_for(state="visible", timeout=20000)
                already_authenticated = False
            except PWTimeout:
                already_authenticated = "account.premierleague.com" not in page.url
                if not already_authenticated:
                    raise LoginError(
                        "Login form did not appear and we are still on the IdP; "
                        "a bot/JS challenge may be blocking it (retry with "
                        "headless=False)."
                    )

            if not already_authenticated:
                # Steps 3-4: fill the DaVinci-rendered credential form.
                email_box.fill(username, timeout=timeout_ms)
                page.get_by_role("textbox", name="Account password").fill(
                    password, timeout=timeout_ms
                )

                # #btnSignIn is the form submit; plain name="Sign In" also matches
                # the "Sign in with Google/Facebook/X/Apple" buttons, so use the id.
                submit = page.locator("#btnSignIn")
                if submit.count() == 0:
                    submit = page.get_by_role("button", name="Sign In", exact=True)
                try:
                    submit.first.click(timeout=15000)
                except PWTimeout:
                    try:
                        page.get_by_role(
                            "textbox", name="Account password"
                        ).press("Enter")
                    except Exception:
                        submit.first.click(timeout=15000, force=True)

            # Step 5: wait for the redirect back to the app after login (or the
            # immediate bounce-back when already authenticated). Match by host
            # via a predicate — glob patterns don't cross '/' and the post-login
            # URL carries a ?code=/#... fragment and then SPA-routes to /en/.
            page.wait_for_url(
                lambda url: url.startswith(app_root)
                and "account.premierleague.com" not in url,
                timeout=timeout_ms,
            )
            page.wait_for_load_state("networkidle", timeout=timeout_ms)

            # The SPA exchanges the code for tokens and writes its token blob to
            # localStorage asynchronously; poll until the oidc.user key with a
            # refresh_token appears.
            blob = _read_oidc_blob(page, FPL_OIDC_STORAGE_KEY, timeout_ms)
        except PWTimeout as exc:
            raise LoginError(
                f"Login timed out: {exc}. If a bot/JS challenge appeared, retry "
                "with headless=False to solve it interactively."
            ) from exc
        finally:
            browser.close()

    refresh_token = blob.get("refresh_token")
    if not refresh_token:
        raise LoginError(
            "Login completed but no refresh_token was found in the app's stored "
            "session. The account may need onboarding, or the storage key format "
            "changed."
        )

    profile = blob.get("profile") or {}
    # The manager's entry/team id isn't in the OIDC profile; callers look it up
    # from /api/me/ using the resulting token. Email is available though.
    email = profile.get("email") or profile.get("preferred_username")

    logger.info("Browser login succeeded (refresh_token length=%d)", len(refresh_token))
    return LoginResult(
        refresh_token=refresh_token,
        team_id=None,
        email=email,
        profile=profile,
    )


def _read_oidc_blob(page, storage_key: str, timeout_ms: int) -> dict:
    """Poll localStorage for the oidc.user blob and return it parsed.

    Falls back to scanning for any ``oidc.user:``-prefixed key if the exact
    configured key is absent (client_id / authority could differ per env).
    """
    deadline_polls = max(1, timeout_ms // 500)
    for _ in range(deadline_polls):
        raw = page.evaluate(
            """(key) => {
                let v = localStorage.getItem(key);
                if (v) return v;
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    if (k && k.startsWith('oidc.user:')) return localStorage.getItem(k);
                }
                return null;
            }""",
            storage_key,
        )
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("oidc.user value was not valid JSON; retrying")
        page.wait_for_timeout(500)
    raise LoginError(
        "Could not read the OIDC session from localStorage after login "
        f"(looked for '{storage_key}' and any 'oidc.user:' key)."
    )
