"""Test runner: manages Direct Line conversations, Entra ID sign-in, the
fixed-interval send loop, assertions, and report generation."""
from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import assertions
from .auth import EntraIDAuthenticator
from .config import AppConfig
from .directline import DirectLineClient, DirectLineError, activities_to_text
from .models import TestCase, TestResult
from .report import generate_reports

log = logging.getLogger(__name__)


class SessionAuthRequired(RuntimeError):
    pass


class ConversationSession:
    """One authenticated Direct Line conversation."""

    def __init__(self, client: DirectLineClient, authenticator: EntraIDAuthenticator,
                 config: AppConfig) -> None:
        self.client = client
        self.authenticator = authenticator
        self.cfg = config
        self.conversation_id: str = ""
        self.token: str = ""
        self.watermark: Optional[str] = None
        self.authenticated = False
        self._token_obtained_at = 0.0
        self._token_expires_in = 1800
        self.last_all_activities: list[dict] = []

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        conv = self.client.start_conversation()
        self.conversation_id = conv["conversationId"]
        self.token = conv["token"]
        self._token_obtained_at = conv.get("_obtained_at", time.time())
        self._token_expires_in = conv.get("expires_in", 1800)

        # Some bots send the OAuthCard proactively on conversationUpdate.
        # Sign in to any cards (Entra, MCP, ...) before the first query.
        # The deadline resets after each sign-in round, so interactive MFA time
        # does not eat into this window.
        self._collect_turn(sent_id=None, timeout=10, quiet_seconds=2)

    def close(self) -> None:
        # Direct Line has no explicit "end conversation" REST call; discard token.
        self.token = ""

    # ------------------------------------------------------------------ send
    # Recovery after a turn that needed interactive sign-in / connection
    # consent, or whose reply is only an auth placeholder (e.g. "Test Reply" or
    # "please open the connection manager and retry"):
    #
    #   Tier 1 - SAME conversation. The human just authorised the connection in
    #     THIS conversation; the connector binding needs a moment to propagate,
    #     and the bot itself says "after the connection is established, retry
    #     the request". So we settle briefly and RE-ASK IN THE SAME
    #     conversation. This reuses the authorisation the human just completed
    #     and does NOT pop a second connection window. SAME_CONV_RESENDS is how
    #     many times we re-ask here.
    #
    #   Tier 2 - FRESH conversation (last resort). Only if re-asking in the same
    #     conversation still returns a placeholder (e.g. a dialog stack broken
    #     by the magic-code message / "Test Reply") do we discard it and open a
    #     brand-new, now-warm conversation. SSO cookies and the user-level
    #     connection persist across conversations, so this one answers cleanly.
    #     FRESH_CONV_MAX bounds those restarts.
    SAME_CONV_RESENDS = 2
    FRESH_CONV_MAX = 2
    # Settle time after interactive auth before re-asking, so the connector
    # consent can propagate to the current conversation.
    POST_AUTH_SETTLE_SECONDS = 2.5

    def _send_and_collect(
        self, text: str
    ) -> tuple[list[dict], list[dict], Optional[float], float]:
        """Send one user message and collect one bot turn.

        Returns (answer_activities, all_bot_activities, auth_done_at,
        send_start). ``auth_done_at`` is non-None when a sign-in / connection
        auth round happened during the turn.
        """
        self._ensure_token()
        sent_id = self.client.send_message(self.conversation_id, self.token, text)
        send_start = time.time()
        answer_acts, all_acts, _, auth_done_at = self._collect_turn(
            sent_id=sent_id,
            timeout=self.cfg.runner.max_wait_seconds,
            quiet_seconds=self.cfg.runner.quiet_seconds,
        )
        return answer_acts, all_acts, auth_done_at, send_start

    def _restart_conversation(self) -> None:
        """Discard the current (post-auth, possibly polluted) conversation and
        open a brand-new one. Entra SSO cookies live in the persistent browser
        profile and the connector consent is bound to the user at the service
        level, so the new conversation is already warm and should not need any
        interactive sign-in."""
        try:
            self.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        self.watermark = None
        self.start()

    @classmethod
    def _answer_is_auth_artifact(cls, answer_acts: list[dict]) -> bool:
        """True when the whole captured "answer" is really an auth/connection
        placeholder rather than a real reply (e.g. the literal Copilot Studio
        ``Test Reply`` sent by a Retry action, the bot commenting on the magic
        code, or a "please connect" prompt). Length-guarded so a genuine long
        data answer is never discarded just for mentioning a word."""
        texts = [(a.get("text") or "").strip()
                 for a in answer_acts if (a.get("text") or "").strip()]
        if not texts:
            return False
        joined = "\n".join(texts)
        low = joined.lower()
        if cls._is_auth_prompt(joined):
            return True
        # Copilot Studio connection Retry placeholder.
        if low == "test reply" or (len(low) <= 24 and "test reply" in low):
            return True
        # Bot talking about the 6-digit magic code it received as a message.
        if "numeric code" in low and len(low) <= 400:
            return True
        return False

    def _needs_recovery(
        self, answer_acts: list[dict], auth_done_at: Optional[float]
    ) -> bool:
        """Whether the captured turn must be retried rather than accepted:
        an interactive auth/connection round just happened in it, or its only
        content is an auth/connection placeholder."""
        return (auth_done_at is not None) \
            or self._answer_is_auth_artifact(answer_acts)

    def send_query(self, text: str) -> tuple[list[dict], int, int, list]:
        """Send a message and wait for the bot turn.

        Returns (answer_activities, first_token_latency_ms, total_latency_ms,
        steps). Steps are structured telemetry for every intermediate activity
        (typing, trace, event, MCP/knowledge/SQL, etc.).

        Sign-in / connection-auth time is excluded from the reported latency.
        After interactive auth -- or when the reply is only an auth placeholder
        -- the query is re-asked: first in the SAME conversation (reusing the
        authorisation just completed, so no second browser/connection prompt),
        and only as a last resort in a fresh now-warm conversation. The auth
        exchange itself is never treated as the query answer.
        """
        merged_all: list[dict] = []
        answer_acts, all_acts, auth_done_at, start = self._send_and_collect(text)
        merged_all.extend(all_acts)

        same_resends = 0
        fresh_convs = 0
        while self._needs_recovery(answer_acts, auth_done_at):
            if same_resends < self.SAME_CONV_RESENDS:
                # Tier 1: settle so the consent binds, then re-ask HERE.
                same_resends += 1
                log.info(
                    "Sign-in/connection finished (or reply is a placeholder); "
                    "waiting %.1fs for it to bind, then re-asking in the SAME "
                    "conversation (same-conversation resend %d/%d).",
                    self.POST_AUTH_SETTLE_SECONDS, same_resends,
                    self.SAME_CONV_RESENDS,
                )
                time.sleep(self.POST_AUTH_SETTLE_SECONDS)
                answer_acts, all_acts, auth_done_at, start = \
                    self._send_and_collect(text)
                merged_all.extend(all_acts)
                continue
            if fresh_convs < self.FRESH_CONV_MAX:
                # Tier 2 (last resort): same conversation won't recover.
                fresh_convs += 1
                same_resends = 0
                log.info(
                    "Same-conversation retry is still not a clean answer; "
                    "opening a fresh, now-warm conversation (%d/%d) and "
                    "re-asking.", fresh_convs, self.FRESH_CONV_MAX,
                )
                self._restart_conversation()
                answer_acts, all_acts, auth_done_at, start = \
                    self._send_and_collect(text)
                merged_all.extend(all_acts)
                continue
            # Bounded: accept the last reply; assertions FAIL honestly rather
            # than false-passing.
            log.warning("Post-auth recovery exhausted; accepting last reply.")
            break

        # Keep the full cross-conversation trace for raw-activity debugging,
        # but build the reported steps from the final (clean) turn only.
        final_all = all_acts
        self.last_all_activities = merged_all

        # Latency is measured on the final accepted turn. It normally has no
        # auth, so effective_start is simply when that turn's message was sent;
        # interactive MFA/consent time never counts against query latency.
        effective_start = auth_done_at if auth_done_at else start
        end = time.time()

        first_token_ts: Optional[float] = None
        if answer_acts:
            first_token_ts = answer_acts[0].get("_received_at", start)

        # Build structured steps from the final clean activity stream.
        from .telemetry import activity_to_step, Step
        steps: list[Step] = []
        for act in final_all:
            step = activity_to_step(act, start)
            if step is not None:
                steps.append(step)

        # Log a compact step trace in verbose mode.
        for s in steps:
            if s.activity_type in ("trace", "event") or s.category in (
                "mcp_action", "knowledge_query", "sql_query",
                "dataverse_query", "planning", "oauth_card",
                "connection_card",
            ):
                log.info("  step [%s] %s%s",
                         s.category, s.summary,
                         f" ({s.activity_name})" if s.activity_name else "")

        first_ms = int((first_token_ts - effective_start) * 1000) if first_token_ts else 0
        total_ms = int((end - effective_start) * 1000)
        return answer_acts, first_ms, total_ms, steps

    # ----------------------------------------------------- turn collection
    def _collect_turn(
        self,
        sent_id: Optional[str],
        timeout: float,
        quiet_seconds: float,
    ) -> tuple[list[dict], list[dict], Optional[str], Optional[float]]:
        """Poll activities until the bot finishes the turn, signing in to any
        OAuthCards encountered along the way.

        Returns (answer_activities, all_bot_activities, watermark,
        auth_done_at).  auth_done_at is the wall-clock time when the last
        sign-in / connection-auth round finished (or None if no auth happened);
        the caller uses it to exclude interactive auth time from latency.

        all_bot_activities includes typing/trace/event activities for telemetry;
        answer_activities contains only the non-card, non-typing messages.

        Multiple cards are supported and processed in order (e.g. first the
        Copilot Studio Entra ID card, then an MCP connection's own OAuth card).
        A persistent Playwright profile means the human only completes MFA the
        first time; later rounds reuse cached SSO cookies.
        """
        deadline = time.time() + timeout
        answer: list[dict] = []
        all_bot: list[dict] = []
        seen_ids: set[str] = set()
        last_bot_at: Optional[float] = None
        auth_done_at: Optional[float] = None

        # Track the most recently completed sign-in so we can tell a new card
        # apart from a failed one that got re-sent.
        last_signin_url: Optional[str] = None
        last_code: Optional[str] = None
        fallback_used = False
        rounds = 0

        # Track Copilot Studio connection-manager cards (MCP/connector auth).
        conn_rounds = 0
        last_conn_url: Optional[str] = None

        while time.time() < deadline:
            activities, self.watermark = self.client.poll_activities(
                self.conversation_id, self.token, self.watermark
            )

            for act in activities:
                act_id = act.get("id") or ""
                if act_id and act_id in seen_ids:
                    continue
                if sent_id and act_id == sent_id:
                    continue
                if self.client._is_from_self(act):  # noqa: SLF001
                    continue
                if not self.client._is_bot_activity(act):  # noqa: SLF001
                    continue
                if act_id:
                    seen_ids.add(act_id)
                act["_received_at"] = time.time()

                card = self.client.find_oauth_card([act])
                if card:
                    signin_url = self.client.extract_signin_url(card)
                    same_as_before = signin_url and signin_url == last_signin_url

                    if same_as_before and not fallback_used and last_code \
                            and self.cfg.auth.code_submit_mode == "auto":
                        # The card we just completed came back: submitting the
                        # code as a chat message wasn't accepted. Retry via
                        # signin/verifyState invoke (the other mechanism).
                        log.warning("OAuthCard returned after sign-in; retrying "
                                    "magic code via signin/verifyState invoke.")
                        self.client.submit_magic_code(
                            self.conversation_id, self.token, last_code,
                            mode="invoke",
                        )
                        fallback_used = True
                    elif same_as_before and fallback_used:
                        raise SessionAuthRequired(
                            "Sign-in failed: OAuthCard persists after fallback."
                        )
                    else:
                        # A new (or first) OAuthCard -> run the browser sign-in.
                        rounds += 1
                        if rounds > self.cfg.auth.max_signin_rounds:
                            raise SessionAuthRequired(
                                f"Exceeded {self.cfg.auth.max_signin_rounds} "
                                f"sign-in rounds; possible auth loop."
                            )
                        # Any messages collected before this card are auth
                        # prompts (e.g. "I'll need you to sign in"), not the
                        # real answer. Drop them so they don't leak into the
                        # captured response or trigger error-phrase checks.
                        answer.clear()
                        log.info("OAuthCard #%d (%s); starting browser sign-in...",
                                 rounds,
                                 self._card_label(card, signin_url))
                        last_code = self._perform_signin(signin_url)
                        last_signin_url = signin_url
                        fallback_used = False
                        self.authenticated = True
                        # Record when auth finished so the caller can exclude
                        # interactive MFA time from query latency.
                        auth_done_at = time.time()
                        # Reset the deadline so the time spent on interactive
                        # MFA doesn't consume the response-waiting window. Each
                        # sign-in round gets a fresh timeout for what comes
                        # after it (next card or the bot's answer).
                        deadline = time.time() + timeout

                    # Auth activity resets the quiet window.
                    last_bot_at = None
                    continue

                # Copilot Studio connection-manager card (MCP/connector auth).
                conn_card = self.client.find_connection_card([act])
                if conn_card:
                    conn_url = self.client.extract_connection_url(conn_card)
                    # Allow up to max_signin_rounds attempts at the SAME
                    # connection URL (the first attempt may race with SSO
                    # cookie propagation); only give up after repeated
                    # failures to avoid an infinite loop.
                    if conn_url == last_conn_url:
                        conn_rounds += 1
                    else:
                        conn_rounds = 1
                    if conn_rounds > self.cfg.auth.max_signin_rounds:
                        raise SessionAuthRequired(
                            f"Exceeded {self.cfg.auth.max_signin_rounds} "
                            f"connection-auth rounds; the connection may "
                            f"still be unauthorized."
                        )
                    # Drop any "let's get you connected" text captured so far.
                    answer.clear()
                    log.info(
                        "Connection card #%d: opening connection manager...",
                        conn_rounds,
                    )
                    self.authenticator.authenticate_connection(conn_url)
                    last_conn_url = conn_url
                    self.authenticated = True
                    auth_done_at = time.time()
                    # Give the connection a moment to propagate before
                    # clicking Retry, then send the card's Retry action and
                    # wait for the real answer.
                    log.info("Connection authenticated; sending Retry action.")
                    time.sleep(2)
                    self.client.send_action_submit(
                        self.conversation_id, self.token, {"action": "Retry"},
                    )
                    deadline = time.time() + timeout
                    last_bot_at = None
                    continue

                # A non-card bot activity.
                all_bot.append(act)
                if act.get("type") == "typing":
                    # Typing indicates the bot is still working; keep waiting
                    # but don't add it to the answer.
                    last_bot_at = time.time()
                    continue

                # Some bots send the connection-manager link as a plain text
                # message (with an inline user-connections URL) instead of an
                # adaptive card.  Treat it the same as a connection card.
                text_conn_url = self._extract_connection_url_from_text(
                    act.get("text", "")
                )
                if text_conn_url:
                    if text_conn_url == last_conn_url:
                        conn_rounds += 1
                    else:
                        conn_rounds = 1
                    if conn_rounds > self.cfg.auth.max_signin_rounds:
                        raise SessionAuthRequired(
                            f"Exceeded {self.cfg.auth.max_signin_rounds} "
                            f"connection-auth rounds; possible loop."
                        )
                    answer.clear()
                    log.info(
                        "Connection URL in text message #%d; opening "
                        "connection manager...", conn_rounds,
                    )
                    self.authenticator.authenticate_connection(text_conn_url)
                    last_conn_url = text_conn_url
                    self.authenticated = True
                    auth_done_at = time.time()
                    time.sleep(2)
                    self.client.send_action_submit(
                        self.conversation_id, self.token, {"action": "Retry"},
                    )
                    deadline = time.time() + timeout
                    last_bot_at = None
                    continue

                # Skip auth-prompt text messages ("please sign in", etc.) that
                # arrive alongside or just after an OAuthCard. The real answer
                # comes after sign-in completes.
                if not answer and self._is_auth_prompt(act.get("text", "")):
                    log.debug("Skipping auth-prompt message: %s",
                              (act.get("text") or "")[:100])
                    last_bot_at = time.time()
                    continue
                answer.append(act)
                last_bot_at = time.time()

            if answer and last_bot_at is not None \
                    and time.time() - last_bot_at >= quiet_seconds:
                break
            # Floor at 100ms to avoid a CPU-spinning tight loop if someone
            # sets polling_interval_seconds to 0.
            time.sleep(max(self.cfg.runner.polling_interval_seconds, 0.1))

        return (self._strip_oauth_cards(answer),
                self._strip_oauth_cards(all_bot),
                self.watermark,
                auth_done_at)

    @staticmethod
    def _card_label(card: dict, signin_url: Optional[str]) -> str:
        """Best-effort short label for an OAuthCard (connection name)."""
        for key in ("connectionName",):
            if card.get(key):
                return str(card[key])
        if signin_url:
            # The Bot Framework token service URL carries the state; the host
            # at least tells us which token service is in use.
            from urllib.parse import urlparse
            return urlparse(signin_url).netloc
        return "unknown"

    @staticmethod
    def _extract_connection_url_from_text(text: str) -> Optional[str]:
        """Extract a Copilot Studio user-connections URL from a plain text
        message (some bots send the link as text instead of an adaptive
        card)."""
        if not text:
            return None
        import re
        m = re.search(r"https?://[^\s)]*user-connections[^\s)]*", text)
        if m:
            return m.group(0)
        return None

    # Phrases that indicate a message is the OAuthPrompt's "please sign in"
    # text or a connection-manager prompt rather than the real answer.
    # Only matched on short messages that arrive before any real answer has
    # been collected.
    _AUTH_PROMPT_PATTERNS = (
        "need you to sign in",
        "please sign in",
        "please sign-in",
        "to sign in",
        "sign in to",
        "you'll need to sign",
        "you will need to sign",
        "authentication required",
        "please log in",
        "please login",
        # connection-manager prompts
        "let's get you connected",
        "lets get you connected",
        "get you connected first",
        "open connection manager",
        "connection is not available",
        "connection needed",
        "connect the data source",
        "verify your credentials",
        "once the connection is ready",
        "connection is not available in this session",
    )

    @classmethod
    def _is_auth_prompt(cls, text: str) -> bool:
        if not text:
            return False
        # Connection prompts can be longer (they include URLs); allow up to
        # 600 chars for those, but keep the 300-char cap for plain sign-in
        # prompts to avoid swallowing real answers.
        low = text.lower()
        if len(text) <= 300:
            return any(p in low for p in cls._AUTH_PROMPT_PATTERNS)
        # For longer messages, only match the connection-specific phrases.
        _CONN_PROMPTS = (
            "let's get you connected",
            "lets get you connected",
            "get you connected first",
            "open connection manager",
            "connect the data source",
            "verify your credentials",
            "once the connection is ready",
            "connection is not available in this session",
        )
        return any(p in low for p in _CONN_PROMPTS)

    @staticmethod
    def _strip_oauth_cards(activities: list[dict]) -> list[dict]:
        """Remove OAuthCard attachments / activities so sign-in UI text does
        not leak into the captured reply."""
        clean: list[dict] = []
        for act in activities:
            if DirectLineClient.find_oauth_card([act]):
                continue
            atts = [
                a for a in (act.get("attachments", []) or [])
                if a.get("contentType") != "application/vnd.microsoft.card.oauth"
            ]
            if atts != (act.get("attachments", []) or []):
                act = dict(act)
                act["attachments"] = atts
            clean.append(act)
        return clean

    # ------------------------------------------------------------------- auth
    def _perform_signin(self, signin_url: str) -> Optional[str]:
        """Run the Playwright Entra/IDP login for one OAuthCard and submit the
        resulting magic code. Returns the code (or None if no code was shown,
        e.g. enhanced-auth direct token post)."""
        if not self.cfg.auth.enabled:
            raise SessionAuthRequired(
                "Bot returned an OAuthCard but auth.enabled is false."
            )
        if not signin_url:
            raise SessionAuthRequired("Could not extract a sign-in URL from OAuthCard.")

        log.info("Sign-in URL: %s", signin_url)
        code = self.authenticator.acquire_magic_code(signin_url)

        if code:
            self._submit_code(code)
        else:
            # No magic code: enhanced auth likely posted the token directly.
            log.info("No magic code; assuming direct token post.")
        log.info("Sign-in round complete for conversation %s", self.conversation_id)
        return code

    def _submit_code(self, code: str) -> None:
        """Submit the magic code back to the bot.

        Copilot Studio's OAuthPrompt (and its built-in test chat) expects the
        6-digit code as a plain chat message -- that is what a human types in
        the Copilot Studio test panel. Some other Bot Framework bots expect a
        signin/verifyState invoke instead. "auto" tries the message first and
        falls back to invoke on HTTP error; the per-turn fallback in
        _collect_turn also tries the other way if the same OAuthCard returns.
        """
        mode = self.cfg.auth.code_submit_mode
        if mode == "message":
            self.client.submit_magic_code(
                self.conversation_id, self.token, code, mode="message"
            )
        elif mode == "invoke":
            self.client.submit_magic_code(
                self.conversation_id, self.token, code, mode="invoke"
            )
        else:  # auto
            try:
                self.client.submit_magic_code(
                    self.conversation_id, self.token, code, mode="message"
                )
            except DirectLineError as e:
                log.warning(
                    "Message-mode code submit failed (%s); trying "
                    "signin/verifyState invoke.", e,
                )
                self.client.submit_magic_code(
                    self.conversation_id, self.token, code, mode="invoke"
                )

    # ----------------------------------------------------------------- tokens
    def _ensure_token(self) -> None:
        age = time.time() - self._token_obtained_at
        if age >= self._token_expires_in - self.cfg.runner.token_refresh_margin_seconds:
            log.info("Refreshing Direct Line token...")
            data = self.client.refresh_token(self.token)
            self.token = data["token"]
            self._token_obtained_at = data.get("_obtained_at", time.time())
            self._token_expires_in = data.get("expires_in", 1800)


class TestRunner:
    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self.authenticator = EntraIDAuthenticator(config.auth)

    def run(self, cases: list[TestCase]) -> list[TestResult]:
        start_all = time.time()
        log.info("Loaded %d test case(s).", len(cases))

        if self.cfg.runner.session_mode == "isolated":
            results = self._run_isolated(cases)
        else:
            results = self._run_continuous(cases)

        results.sort(key=lambda r: r.timestamp)
        elapsed = time.time() - start_all
        passed = sum(1 for r in results if r.status == "PASS")
        log.info("Finished %d cases in %.1fs - %d passed.",
                 len(results), elapsed, passed)

        raw = {}
        raw_dir = Path(self.cfg.report.output_dir) / "raw_activities"
        save_raw = self.cfg.report.save_raw_activities
        if save_raw:
            raw_dir.mkdir(parents=True, exist_ok=True)

        for r in results:
            if save_raw and hasattr(r, "_raw_activities"):
                # Save the full activity stream (typing/trace/event + answer),
                # not just the final answer messages.
                full_acts = getattr(r, "_all_activities", None) or r._raw_activities
                raw_path = raw_dir / f"{r.case_id}_{int(time.time()*1000)}.json"
                raw_path.write_text(
                    json.dumps(full_acts, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                r.raw_activity_file = str(raw_path)
                raw[r.case_id] = full_acts

        generate_reports(results, self.cfg.report.output_dir,
                         self.cfg.report.formats, raw)
        return results

    # ------------------------------------------------------------ dispatchers
    def _run_continuous(self, cases: list[TestCase]) -> list[TestResult]:
        """Group cases by session_group; one authenticated conversation per group."""
        groups: "OrderedDict[str, list[TestCase]]" = OrderedDict()
        for c in cases:
            groups.setdefault(c.session_group, []).append(c)

        results: list[TestResult] = []
        for group_id, group_cases in groups.items():
            log.info("=== Session group %s: %d case(s) ===", group_id, len(group_cases))
            session = self._new_session()
            try:
                session.start()
                for i, case in enumerate(group_cases):
                    results.append(self._run_one(session, case))
                    if i < len(group_cases) - 1:
                        time.sleep(self.cfg.runner.interval_seconds)
            finally:
                session.close()
        return results

    def _run_isolated(self, cases: list[TestCase]) -> list[TestResult]:
        """Each case gets its own conversation + sign-in."""
        results: list[TestResult] = []
        concurrency = max(1, self.cfg.runner.concurrency)
        if concurrency == 1:
            for i, case in enumerate(cases):
                results.append(self._run_isolated_one(case))
                if i < len(cases) - 1:
                    time.sleep(self.cfg.runner.interval_seconds)
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {pool.submit(self._run_isolated_one, c): c for c in cases}
                for fut in as_completed(futures):
                    results.append(fut.result())
        return results

    def _run_isolated_one(self, case: TestCase) -> TestResult:
        session = self._new_session()
        try:
            session.start()
            return self._run_one(session, case)
        finally:
            session.close()

    # -------------------------------------------------------------- one case
    def _new_session(self) -> ConversationSession:
        client = DirectLineClient(
            secret=self.cfg.directline.secret,
            endpoint=self.cfg.directline.endpoint,
            user=dict(self.cfg.directline.user),
            locale=self.cfg.directline.locale,
            channel_data=dict(self.cfg.directline.channel_data),
        )
        return ConversationSession(client, self.authenticator, self.cfg)

    @staticmethod
    def _summarize_steps(steps: list) -> str:
        """One-line summary of the non-heartbeat steps for the console log."""
        interesting = [
            s for s in steps
            if s.category not in ("heartbeat", "other", "message", "trace")
            or s.activity_type in ("event",)
        ]
        if not interesting:
            # Fall back to trace activities with names.
            interesting = [s for s in steps if s.activity_name]
        parts = []
        for s in interesting[:12]:
            label = s.activity_name or s.category
            parts.append(f"{label}@{s.offset_ms}ms")
        return " → ".join(parts)

    def _run_one(self, session: ConversationSession, case: TestCase) -> TestResult:
        result = TestResult(case_id=case.case_id, query=case.query,
                            conversation_id=session.conversation_id,
                            timestamp=TestResult.now_stamp())
        log.info("[%s] >>> %s", case.case_id, case.query)
        try:
            activities, first_ms, total_ms, steps = session.send_query(case.query)
            result.first_token_latency_ms = first_ms
            result.total_latency_ms = total_ms
            result.steps = steps
            result.response_text = activities_to_text(activities)
            result._raw_activities = activities  # type: ignore[attr-defined]
            result._all_activities = session.last_all_activities  # type: ignore[attr-defined]

            outcome = assertions.evaluate(case, result, self.cfg.assertions)
            result.keyword_hits = outcome.hits
            result.keyword_misses = outcome.misses
            result.error_phrases_hit = outcome.error_phrases
            result.status = "PASS" if outcome.passed else "FAIL"
            if outcome.reasons:
                result.error = "; ".join(outcome.reasons)

            # Compact step summary in the log.
            step_summary = self._summarize_steps(steps)
            log.info("[%s] %s (%dms) %s",
                     case.case_id, result.status, total_ms,
                     result.response_text[:120].replace("\n", " "))
            if step_summary:
                log.info("[%s] steps: %s", case.case_id, step_summary)
        except Exception as e:  # noqa: BLE001
            result.status = "ERROR"
            result.error = str(e)
            log.exception("[%s] ERROR: %s", case.case_id, e)
        return result
