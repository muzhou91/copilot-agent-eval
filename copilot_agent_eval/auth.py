"""Entra ID sign-in helper for Direct Line OAuthCard flow.

When Copilot Studio is configured with "Authenticate manually" + "Require
users to sign in", every new Direct Line conversation receives an OAuthCard.
This helper opens the card's sign-in URL in a real (Chromium) browser via
Playwright, lets the user complete Entra ID login (including MFA), and then
extracts the 6-digit magic code shown on the token.botframework.com redirect
page so the runner can submit it back to the conversation.

A persistent browser profile is used so that after the first interactive
login (including MFA), subsequent runs reuse the Entra ID SSO cookies and
complete near-instantly. For CI, supply a service account without MFA via
config.auth.username / password and set headless: true, manual: false.

The connection-manager flow (connector/MCP consent) is handled by
:meth:`authenticate_connection`. Unlike an OAuthCard there is no magic code:
the page binds the connection to the conversation; we auto-click consent
buttons, wait for the page to report the connection ready, and only then let
the caller send the card's Retry action.
"""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

from .config import AuthConfig

log = logging.getLogger(__name__)

_CODE_PATTERN = re.compile(r"\b(\d{6})\b")
_REDIRECT_URL_MARKER = "token.botframework.com/.auth/web/redirect"

# Hosts/paths that indicate we are still inside an auth/consent UI. Once the
# browser leaves all of these (and no magic code is shown), we treat the sign-in
# as completed via direct token post.
_AUTH_HOST_MARKERS = (
    "login.microsoftonline.com",
    "login.live.com",
    "token.botframework.com",
    "authorize",
    "/oauth2/",
    "/common/oauth",
    "consent",
    "signin",
)

# Page-body phrases that indicate the sign-in completed without a magic code.
_SUCCESS_MARKERS = (
    "signed in",
    "sign-in complete",
    "sign in complete",
    "authentication complete",
    "authenticated",
    "you can close",
    "verification successful",
    "successfully signed",
    "authentication was successful",
)

# Page-body phrases that strongly indicate a Copilot Studio connection consent
# page has finished. These are specific phrases (not the generic word
# "connected", which can appear in navigation/chrome while the connection is
# still being authorised).
_CONNECTION_READY_MARKERS = (
    # English — specific phrases
    "connection is ready",
    "connection ready",
    "successfully connected",
    "connection successful",
    "connection was successful",
    "you're all set",
    "you are all set",
    "consent granted",
    "credentials valid",
    "no action needed",
    "no pending",
    "verification successful",
    # 中文
    "已连接",
    "连接成功",
    "已就绪",
    "连接正常",
)

# Consent / allow / connect buttons that are safe to auto-click on the
# connection-manager page itself (low-risk, idempotent).
_CONSENT_BUTTON_SELECTORS = (
    'button:has-text("Allow")',
    'button:has-text("Accept")',
    'button:has-text("Authorize")',
    'button:has-text("Connect")',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Submit")',
    'button:has-text("同意")',
    'button:has-text("接受")',
    'button:has-text("授权")',
    'button:has-text("连接")',
    'button:has-text("继续")',
    'button:has-text("提交")',
    'input[value="Allow"]',
    'input[value="Accept"]',
    'input[value="Authorize"]',
    'input[value="Connect"]',
    'input[value="Next"]',
    '[role="button"]:has-text("Allow")',
    '[role="button"]:has-text("Accept")',
    '[role="button"]:has-text("Authorize")',
)

# Sign-in / login buttons that may appear inside a popup opened by the
# connection manager (e.g. an Entra ID re-auth inside a popup).  We do NOT
# auto-click these on a loop: clicking "Sign in" on an MFA page can submit a
# half-filled form or make the popup vanish.  They are clicked at most once,
# and then the popup is left for the user / monitored for completion.
_SIGNIN_BUTTON_SELECTORS = (
    'button:has-text("Sign in")',
    'button:has-text("登录")',
    'input[type="submit"]',
)

# Specific sign-in button selectors for the connection-manager *main* page
# (avoids generic input[type=submit] which could clash with consent forms).
_MAIN_PAGE_SIGNIN_SELECTORS = (
    'button:has-text("Sign in")',
    'button:has-text("登录")',
)

# Buttons whose presence means a connection on the manager page still needs
# the user to act. Used to distinguish a real "Connected" status from a page
# that merely mentions the word "connected" in its chrome.
_PENDING_ACTION_SELECTORS = (
    'button:has-text("Connect")',
    'button:has-text("Sign in")',
    'button:has-text("Authorize")',
    'button:has-text("连接")',
    'button:has-text("登录")',
    'button:has-text("授权")',
)


class AuthError(RuntimeError):
    pass


class EntraIDAuthenticator:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self.profile_dir = str(Path(config.user_data_dir).expanduser().resolve())

    # ------------------------------------------------------------ launch
    def _launch_context(self, p):
        """Launch the persistent Chromium context (profile, headless).

        NOTE: keep the launch args identical to the original working version
        (no forced locale / --lang). Forcing ``locale="en-US"`` / ``--lang=en-US``
        changed how the Microsoft sign-in pages render and broke the flow where
        the user clicks through to the magic-code page.
        """
        return p.chromium.launch_persistent_context(
            self.profile_dir,
            headless=self.config.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

    @staticmethod
    def _live_pages(pages) -> list:
        """Pages still open (filter out closed windows / popups)."""
        return [pg for pg in pages if not pg.is_closed()]

    # Set once stdin reaches EOF / is not interactive, so we stop polling it.
    _stdin_eof: bool = False

    @classmethod
    def _enter_pressed(cls) -> bool:
        """Non-blocking check whether the user pressed Enter in the terminal.

        Works while the poll loop keeps running (no blocking readline), so the
        browser can stay open and the user resumes the run by pressing Enter.
        Unix/macOS uses :mod:`select`; Windows falls back to :mod:`msvcrt`.
        Returns False if stdin is not interactive.
        """
        if cls._stdin_eof:
            return False
        # Unix / macOS
        try:
            import select
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready:
                line = sys.stdin.readline()
                if line:
                    # Real input (Enter yields "\n"); consume it and resume.
                    return True
                # Empty read == EOF: stdin is not an interactive terminal.
                cls._stdin_eof = True
            return False
        except (OSError, ValueError):
            # Not a selectable fd (e.g. redirected/closed stdin on some setups)
            cls._stdin_eof = True
        except ImportError:
            pass
        # Windows
        try:
            import msvcrt
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    return True
        except Exception:
            pass
        return False

    # -------------------------------------------------------------- magic
    def acquire_magic_code(self, signin_url: str) -> str | None:
        """Open the sign-in URL, complete login, return the magic code.

        Returns None if the redirect page did not show a code (this can happen
        when enhanced authentication posts the token directly to the bot; in
        that case the caller should just poll the conversation).

        All open pages (main page + any MFA popup) are monitored for the
        magic code.  If the original page closes during MFA (some Entra flows
        open a popup and close the opener), we keep watching the popup instead
        of giving up immediately.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise AuthError(
                "playwright is not installed. Run:\n"
                "  pip install -r requirements.txt\n"
                "  playwright install chromium"
            ) from e

        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        log.info("Opening browser for Entra ID sign-in (profile: %s)", self.profile_dir)

        with sync_playwright() as p:
            context = self._launch_context(p)
            pages: list = []

            def _on_popup(popup) -> None:
                """Track popups opened during MFA (Authenticator, FIDO, etc.)."""
                if any(popup is pg for pg in pages):
                    return
                pages.append(popup)
                log.info("New browser page opened during sign-in: %s",
                         (popup.url or "")[:120])

            context.on("page", _on_popup)

            page = context.new_page()
            pages.append(page)
            try:
                page.goto(signin_url, wait_until="domcontentloaded", timeout=60000)
                if not self.config.manual and self.config.username and self.config.password:
                    self._auto_fill(page)

                # Poll until the sign-in completes. Different OAuthCards finish
                # differently:
                #   - Bot Framework Entra card -> token.botframework.com redirect
                #     page showing a 6-digit magic code.
                #   - Connector / Dataverse card -> may show a success page, may
                #     close the popup, or may post the token directly with no
                #     code at all.
                # We detect all of these instead of hard-waiting on one URL.
                deadline = time.time() + self.config.login_timeout_seconds
                logged_waiting = False
                seen_auth_page = self._is_auth_url(page)
                # When we first land on a page that looks like the post-login
                # code/result page, start a timer; if auto-extraction fails for
                # this long, fall back to asking the user to type the code.
                code_page_since: Optional[float] = None
                manual_prompted = False
                while time.time() < deadline:
                    live = self._live_pages(pages)
                    if not live:
                        # Every page (main + popups) has closed.  If we already
                        # saw an auth page, this usually means the token was
                        # posted directly (enhanced auth).  If we never reached
                        # an auth page something went wrong.
                        if seen_auth_page:
                            log.info(
                                "All sign-in pages closed; assuming direct "
                                "token post."
                            )
                            return None
                        log.warning(
                            "Browser closed before reaching an auth page."
                        )
                        return None

                    # If the original page closed but a popup is still alive,
                    # switch our attention to the popup.
                    if page.is_closed():
                        page = live[0]
                        log.info(
                            "Original sign-in page closed; watching popup: %s",
                            (page.url or "")[:120],
                        )

                    # Check every live page for the code / success.
                    code = None
                    for pg in live:
                        if self._is_auth_url(pg):
                            seen_auth_page = True
                        c = self._extract_code(pg)
                        if c:
                            code = c
                            break
                    if code:
                        log.info("Magic code obtained: %s", code)
                        return code

                    for pg in live:
                        if self._is_success_page(pg):
                            log.info(
                                "Sign-in success page detected (no magic code); "
                                "the token was posted directly."
                            )
                            return None

                    if seen_auth_page:
                        # Only consider ourselves "off auth" if EVERY live
                        # page has left the auth hosts (a popup might still
                        # be on login.microsoftonline.com while the main page
                        # has redirected).
                        any_on_auth = any(
                            self._is_auth_url(pg) for pg in live
                        )
                        if not any_on_auth:
                            # Left the auth/consent hosts without a code or
                            # success message -> give the page a moment to
                            # render, then re-check before concluding.
                            page.wait_for_timeout(1500)
                            code = self._extract_code(page)
                            if code:
                                log.info("Magic code obtained: %s", code)
                                return code
                            if not any(self._is_auth_url(pg) for pg in self._live_pages(pages)):
                                log.info(
                                    "Redirected away from the auth host "
                                    "without a magic code; assuming direct "
                                    "token post."
                                )
                                return None

                    # Track time on a potential code/result page for the
                    # manual-input fallback.  Check every live page.
                    code_page = page
                    on_code_page = self._looks_like_code_page(page)
                    if not on_code_page:
                        for pg in live:
                            if pg is page:
                                continue
                            if self._looks_like_code_page(pg):
                                code_page = pg
                                on_code_page = True
                                break
                    if on_code_page:
                        if code_page_since is None:
                            code_page_since = time.time()
                            log.debug(
                                "On possible code page: %s",
                                (code_page.url or "")[:200],
                            )
                        elif (not manual_prompted
                              and time.time() - code_page_since > 15):
                            manual_prompted = True
                            code = self._prompt_manual_code(code_page)
                            if code:
                                log.info("Magic code entered manually: %s", code)
                                return code
                    else:
                        code_page_since = None

                    if not logged_waiting:
                        log.info(
                            "Waiting for %s login to complete in the browser...",
                            "manual" if self.config.manual else "automated",
                        )
                        logged_waiting = True
                    page.wait_for_timeout(1000)

                raise AuthError(
                    f"Timed out after {self.config.login_timeout_seconds}s "
                    "waiting for sign-in to complete."
                )
            finally:
                for pg in list(pages):
                    if not pg.is_closed():
                        try:
                            pg.close()
                        except Exception:
                            pass
                try:
                    context.close()
                except Exception:
                    pass

    # ------------------------------------------------- connection manager auth
    def authenticate_connection(self, connection_url: str) -> bool:
        """Open the Copilot Studio connection-manager page and complete any
        pending connector/MCP authentication.

        Two modes, selected by ``config.auth.auto_click_consent``:

        * Pure manual (default, ``auto_click_consent=False``): the browser is
          opened and NOTHING is clicked automatically. The human clicks
          Connect / Submit / MFA themselves, and when finished simply CLOSES
          the browser window — that is the signal to continue. A strong
          "connection ready" phrase also continues automatically. This avoids
          an auto-click closing a consent popup before the user submits it.

        * Auto (``auto_click_consent=True``, unattended warm-SSO runs only):
          auto-click safe consent/connect buttons on the main page and on any
          popup, each physical button at most once; "Sign in"/"登录" is clicked
          at most once per page so an MFA form is never re-submitted. Poll for
          a specific ready marker (the generic word "connected" is not trusted
          alone).

        On timeout, both modes offer an Enter-to-continue fallback.
        Returns True when the connection is ready (or the user confirms).
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise AuthError(
                "playwright is not installed. Run:\n"
                "  pip install -r requirements.txt\n"
                "  playwright install chromium"
            ) from e

        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        log.info("Opening connection manager: %s", connection_url)

        with sync_playwright() as p:
            context = self._launch_context(p)
            pages: list = []
            # Popups whose "Sign in" button we already clicked once, so we do
            # not re-click it on every poll.
            signin_clicked: set = set()

            def _on_popup(popup) -> None:
                """Track a consent/sign-in popup."""
                if any(popup is pg for pg in pages):
                    return
                pages.append(popup)
                log.info("New popup opened during connection auth: %s",
                         (popup.url or "")[:120])
                try:
                    popup.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass

            context.on("page", _on_popup)

            page = context.new_page()
            pages.append(page)
            try:
                page.goto(connection_url, wait_until="domcontentloaded", timeout=60000)

                deadline = time.time() + self.config.login_timeout_seconds
                auto_click = bool(self.config.auto_click_consent)
                silent_until = time.time() + 20  # quiet auto-SSO window
                prompted = False
                logged = False
                last_click_at = 0.0
                # Global de-dup of every physical consent button we click, so
                # the same "Connect"/"Submit" is never clicked twice (which
                # would re-open a consent popup after it was already done).
                clicked_sigs: set = set()
                # Once the page shows a Connected status, stop proactively
                # clicking NEW buttons — the user has connected at least one
                # thing and we don't want to fan out across other rows.
                stop_auto_click = False
                # When a Connected status first appears with no popup open,
                # remember it; if it stays for a few seconds we accept the
                # connection as ready even if unrelated rows still show a
                # Connect button (avoids waiting the whole timeout).
                connected_stable_since: Optional[float] = None

                if not auto_click:
                    sys.stdout.write(
                        "\n" + "=" * 70 + "\n"
                        "Connection manager opened (PURE MANUAL mode — the\n"
                        "tool will NOT click anything).\n"
                        "  1. Click Connect / Sign in yourself.\n"
                        "  2. Complete consent / Submit / MFA in the popup.\n"
                        "  3. When finished, come back to THIS TERMINAL and\n"
                        "     press Enter to continue.\n"
                        "     (Closing the browser window also works.)\n"
                        + "=" * 70 + "\n"
                    )
                    sys.stdout.flush()
                    log.info("Pure-manual connection auth: press Enter in the "
                             "terminal when you are done (or close the window).")

                while time.time() < deadline:
                    live = self._live_pages(pages)
                    if not live:
                        log.info("Connection manager window closed; assuming done.")
                        return True

                    # The user can always resume the run from the terminal by
                    # pressing Enter (primary signal in pure-manual mode, where
                    # closing the persistent-context window is not always
                    # detected reliably).
                    if self._enter_pressed():
                        log.info("Enter pressed in terminal; continuing.")
                        return True

                    now = time.time()
                    popups = [pg for pg in live if pg is not page]

                    if auto_click:
                        # Once any page shows a Connected status, freeze
                        # further auto-clicking (a still-open popup may still
                        # need its one consent click, handled below).
                        if not stop_auto_click:
                            for pg in live:
                                if self._page_shows_connected(pg):
                                    stop_auto_click = True
                                    log.info(
                                        "A Connected status is shown; stopping "
                                        "further auto-clicking and waiting for "
                                        "readiness."
                                    )
                                    break

                        # Fast acceptance: after we have stopped auto-clicking,
                        # the main page continuously shows Connected and no
                        # popup is open for a few seconds, treat the connection
                        # as ready (other rows may still list unrelated Connect
                        # buttons).
                        if stop_auto_click and not popups \
                                and self._page_shows_connected(page):
                            if connected_stable_since is None:
                                connected_stable_since = now
                            elif now - connected_stable_since >= 5:
                                log.info(
                                    "Connected status stable for 5s with no "
                                    "popup; accepting connection as ready."
                                )
                                return True
                        else:
                            connected_stable_since = None

                        # --- Button clicking (auto mode only) ---------------
                        # Critical: while a consent popup is open, ONLY interact
                        # with the popup. Re-clicking the main page's "Connect"
                        # button behind the popup is what re-opened it in a
                        # loop.
                        if popups:
                            for pg in popups:
                                self._try_click_consent_buttons(pg, clicked_sigs)
                                if id(pg) not in signin_clicked:
                                    if self._try_click_signin_button(pg):
                                        signin_clicked.add(id(pg))
                        else:
                            # No popup open: click the main page once per
                            # rate-limit window, de-duplicated by identity.
                            if not stop_auto_click and now - last_click_at >= 5.0:
                                self._try_click_consent_buttons(page, clicked_sigs)
                                last_click_at = now
                            if not stop_auto_click and id(page) not in signin_clicked:
                                if self._try_click_signin_button(
                                    page, _MAIN_PAGE_SIGNIN_SELECTORS
                                ):
                                    signin_clicked.add(id(page))

                    # Readiness auto-advance. In PURE-MANUAL mode we do NOT
                    # auto-advance on any page text — the human closes the
                    # browser window when done (handled at the top), which is
                    # fully predictable and can't fire while they are still
                    # clicking through a consent popup. Auto mode additionally
                    # trusts ready markers / structural signals.
                    if auto_click:
                        ready_check = self._is_connection_ready
                        if ready_check(page):
                            log.info("Connection manager reports ready.")
                            page.wait_for_timeout(2000)
                            if ready_check(page):
                                return True
                            log.info("Ready marker disappeared on re-check; continuing to wait.")

                        for pg in popups:
                            if ready_check(pg):
                                log.info("Connection ready (popup).")
                                page.wait_for_timeout(2000)
                                if ready_check(pg):
                                    return True

                    if auto_click and now < silent_until:
                        if not logged:
                            log.info(
                                "Waiting up to %ds for automatic SSO connection "
                                "authorization...", 20,
                            )
                            logged = True
                    elif not prompted:
                        # (Auto mode) silent window passed, or (manual mode)
                        # first iteration — keep the browser open; never block.
                        prompted = True
                        if auto_click:
                            log.info(
                                "Connection not ready yet; please complete "
                                "sign-in / consent / MFA in the Chromium window."
                            )
                            sys.stdout.write(
                                "\n" + "=" * 70 + "\n"
                                "Connection manager is not ready yet.\n"
                                "Please complete the connection authentication\n"
                                "in the browser window. The run continues\n"
                                "automatically once ready (or close the window).\n"
                                + "=" * 70 + "\n"
                            )
                            sys.stdout.flush()

                    time.sleep(1.0)

                # Timed out — offer the manual fallback from v3: if the user
                # completed auth in the browser, let them press Enter to
                # continue instead of hard-failing the whole run.
                sys.stdout.write(
                    "\n" + "=" * 70 + "\n"
                    "Connection manager did not auto-detect readiness.\n"
                    "If you have completed authentication in the browser,\n"
                    "press Enter to continue (or close the browser to abort):\n"
                    + "=" * 70 + "\n> "
                )
                sys.stdout.flush()
                try:
                    sys.stdin.readline()
                except Exception:
                    pass
                log.info("User confirmed connection manually; continuing.")
                return True
            finally:
                for pg in list(pages):
                    if not pg.is_closed():
                        try:
                            pg.close()
                        except Exception:
                            pass
                try:
                    context.close()
                except Exception:
                    pass

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _page_body(page) -> str:
        """Lower-cased visible body text, or '' if unavailable."""
        if page.is_closed():
            return ""
        try:
            return page.locator("body").inner_text(timeout=2000).lower()
        except Exception:
            return ""

    @staticmethod
    def _has_visible_button(page, selectors) -> bool:
        """True if any selector matches a currently-visible element."""
        if page.is_closed():
            return False
        for sel in selectors:
            try:
                loc = page.locator(sel)
                for i in range(loc.count()):
                    try:
                        if loc.nth(i).is_visible():
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    @classmethod
    def _page_shows_connected(cls, page) -> bool:
        """True if the page visibly shows a Connected / 已连接 status."""
        body = cls._page_body(page)
        return ("connected" in body) or ("已连接" in body) or ("连接成功" in body)

    @classmethod
    def _has_ready_phrase(cls, page) -> bool:
        """True only for an explicit ready phrase (strong signal).

        Safe to use in pure-manual mode: it never matches while the user is
        still clicking through Connect / Submit.
        """
        body = cls._page_body(page)
        return bool(body) and any(
            marker in body for marker in _CONNECTION_READY_MARKERS
        )

    @classmethod
    def _is_connection_ready(cls, page) -> bool:
        """Detect a 'connection ready / connected' page.

        Two signals:
          1. A specific ready phrase (connection is ready, successfully
             connected, consent granted, ...).
          2. Structural: the page shows a Connected status AND there is no
             visible Connect / Sign in / Authorize action button left to
             click. This matches the Copilot Studio manager, which shows a
             "Connected" badge per row with no remaining action button.
        """
        if page.is_closed():
            return False
        body = cls._page_body(page)
        if not body:
            return False
        if cls._has_ready_phrase(page):
            return True
        if cls._page_shows_connected(page) and not cls._has_visible_button(
            page, _PENDING_ACTION_SELECTORS
        ):
            return True
        return False

    @staticmethod
    def _button_sig(selector: str, btn) -> Optional[tuple]:
        """Stable identity for a button so it is never clicked twice.

        Based on its trimmed label and quantised on-screen position — NOT the
        selector, because the same physical button can be matched by several
        selectors (e.g. ``button:has-text("Connect")`` and
        ``input[value="Connect"]``). Survives re-renders as long as the button
        stays in roughly the same place with the same label.
        """
        try:
            txt = (btn.inner_text(timeout=300) or "").strip().lower()[:40]
        except Exception:
            txt = ""
        try:
            val = btn.get_attribute("value", timeout=300)
        except Exception:
            val = ""
        label = txt or (val or "").strip().lower()[:40]
        try:
            box = btn.bounding_box(timeout=300)
        except Exception:
            box = None
        if box:
            return (label, int(box["x"] // 15), int(box["y"] // 15))
        return (label,)

    def _try_click_consent_buttons(self, page, clicked_sigs=None) -> bool:
        """Best-effort click of the first visible, not-yet-clicked safe
        consent/allow/connect button. Each physical button is clicked at most
        once (tracked via ``clicked_sigs``). Does NOT click "Sign in"/"登录" —
        use :meth:`_try_click_signin_button` for those (once per page)."""
        if page.is_closed():
            return False
        if clicked_sigs is None:
            clicked_sigs = set()
        for sel in _CONSENT_BUTTON_SELECTORS:
            try:
                loc = page.locator(sel)
                count = loc.count()
            except Exception:
                continue
            for i in range(count):
                btn = loc.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    sig = self._button_sig(sel, btn)
                    if sig is not None and sig in clicked_sigs:
                        continue
                    btn.click(timeout=2000)
                    if sig is not None:
                        clicked_sigs.add(sig)
                    log.info("Clicked consent button: %s", sel)
                    page.wait_for_timeout(800)
                    return True
                except Exception:
                    continue
        return False

    def _try_click_signin_button(self, page, selectors=None) -> bool:
        """Click a Sign in / 登录 / submit button on a popup at most once.

        Returns True if a click was made.  This is deliberately separate from
        :meth:`_try_click_consent_buttons` so that we never repeatedly click
        "Sign in" on an MFA form (which can re-submit the page or make the
        popup vanish before the user enters their code).
        """
        if page.is_closed():
            return False
        sel_list = selectors or _SIGNIN_BUTTON_SELECTORS
        for sel in sel_list:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=2000)
                    log.info("Clicked sign-in button once: %s", sel)
                    page.wait_for_timeout(1500)
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------ auto
    def _auto_fill(self, page) -> None:
        """Best-effort credential fill for the standard Entra ID login pages.

        If anything unexpected appears (MFA, consent, account picker), we
        simply leave the browser open for manual completion.
        """
        try:
            # Email / account field.
            page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=20000)
            page.fill('input[type="email"], input[name="loginfmt"]', self.config.username)
            page.click('input[type="submit"], button[type="submit"]')

            # Password field.
            page.wait_for_selector('input[type="password"], input[name="passwd"]', timeout=20000)
            page.fill('input[type="password"], input[name="passwd"]', self.config.password)
            page.click('input[type="submit"], button[type="submit"]')

            # "Stay signed in?" / "Keep me signed in?" -> Yes.
            try:
                page.wait_for_selector('input[id="idSIButton9"]', timeout=8000)
                page.click('input[id="idSIButton9"]')
            except Exception:
                pass

            # Consent screen ("Accept") if it appears.
            try:
                page.wait_for_selector('input[value="Accept"], #idBtn_Accept', timeout=5000)
                page.click('input[value="Accept"], #idBtn_Accept')
            except Exception:
                pass
        except Exception as e:
            log.warning("Auto-fill did not complete (%s); waiting for manual login.", e)

    @staticmethod
    def _extract_code(page) -> str | None:
        """Try several ways to find the 6-digit magic code on the page."""
        # 1) Query string / hash may carry the code directly.
        try:
            url = page.url or ""
        except Exception:
            url = ""
        m = re.search(r"[?&#]code=([^&#]+)", url)
        if m:
            val = m.group(1)
            if val.isdigit() and len(val) == 6:
                return val

        # Collect text from the main frame and every same-origin iframe.
        texts: list[str] = []
        try:
            texts.append(page.evaluate(
                "() => (document.body && document.body.innerText) || ''"
            ))
        except Exception:
            pass
        try:
            texts.append(page.locator("body").inner_text(timeout=3000))
        except Exception:
            pass
        try:
            for frame in page.frames:
                try:
                    texts.append(frame.evaluate(
                        "() => (document.body && document.body.innerText) || ''"
                    ))
                except Exception:
                    continue
        except Exception:
            pass

        for txt in texts:
            if not txt:
                continue
            m = _CODE_PATTERN.search(txt)
            if m:
                return m.group(1)

        # 4) Dedicated element selectors.
        try:
            el = page.locator(
                ".code, .magic-code, [data-testid='magic-code'], "
                ".verification-code, #code, .auth-code"
            ).first
            if el.count() > 0:
                txt = el.inner_text(timeout=2000)
                m = _CODE_PATTERN.search(txt or "")
                if m:
                    return m.group(1)
        except Exception:
            pass

        # 5) Last resort: regex the raw HTML (code may be in a data attribute).
        try:
            html = page.content()
            m = re.search(rb'["\s>(](\d{6})["\s<]', html.encode("utf-8", "ignore"))
            if m:
                return m.group(1).decode()
        except Exception:
            pass
        return None

    @staticmethod
    def _looks_like_code_page(page) -> bool:
        """True when the page is a likely post-login code/result page.

        We use this to decide when to offer the manual-input fallback: the
        token.botframework.com redirect page, or any page that mentions a
        verification code.
        """
        try:
            url = (page.url or "").lower()
        except Exception:
            return False
        if "token.botframework.com/.auth/web/redirect" in url:
            return True
        if "token.botframework.com" in url and "signin" not in url:
            return True
        try:
            body = page.evaluate(
                "() => (document.body && document.body.innerText) || ''"
            ).lower()
        except Exception:
            body = ""
        return any(phrase in body for phrase in (
            "verification code", "magic code", "copy this code",
            "enter it in the chat", "enter this code",
        ))

    @staticmethod
    def _prompt_manual_code(page) -> str | None:
        """Fallback: ask the user to read the code from the browser and type
        it into the terminal."""
        url = ""
        try:
            url = page.url or ""
        except Exception:
            pass
        sys.stdout.write(
            "\n" + "=" * 70 + "\n"
            "Could not auto-read the verification code from the browser.\n"
            f"Browser URL: {url[:200]}\n"
            "Please look at the Chromium window, type the 6-digit code shown\n"
            "there, and press Enter (or just press Enter to skip):\n"
            + "=" * 70 + "\n> "
        )
        sys.stdout.flush()
        try:
            user_input = sys.stdin.readline()
        except Exception:
            return None
        code = (user_input or "").strip()
        if code and code.isdigit() and len(code) == 6:
            return code
        return None

    @staticmethod
    def _is_success_page(page) -> bool:
        """Detect a 'you are signed in / you can close this window' page."""
        if page.is_closed():
            return False
        try:
            body = page.locator("body").inner_text(timeout=2000).lower()
        except Exception:
            return False
        return any(marker in body for marker in _SUCCESS_MARKERS)

    @staticmethod
    def _is_auth_url(page) -> bool:
        """True if the current URL still looks like an auth/consent page."""
        try:
            url = (page.url or "").lower()
        except Exception:
            return True
        return any(marker in url for marker in _AUTH_HOST_MARKERS)

    def _left_auth_host(self, page) -> bool:
        """True if we have navigated away from every known auth/consent host."""
        return not self._is_auth_url(page)
