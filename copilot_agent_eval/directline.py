"""Minimal Direct Line API 3.0 client.

Implements:
  * POST /conversations            (start conversation, returns token + streamUrl)
  * POST /conversations/{id}/activities   (send an activity)
  * GET  /conversations/{id}/activities   (poll with watermark)
  * POST /tokens/refresh           (refresh a conversation token)

OAuthCard sign-in is handled by the auth helper; this client exposes helpers
to detect an OAuthCard and submit the magic code as a plain chat message
(Copilot Studio style) or via signin/verifyState invoke.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

OAUTH_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.oauth"


class DirectLineError(RuntimeError):
    pass


class DirectLineClient:
    def __init__(
        self,
        secret: str,
        endpoint: str = "https://directline.botframework.com/v3/directline",
        user: Optional[dict] = None,
        locale: str = "en-US",
        channel_data: Optional[dict] = None,
    ) -> None:
        self.secret = secret
        self.endpoint = endpoint.rstrip("/")
        self.user = user or {"id": "dl_test_user", "name": "Test User"}
        self.locale = locale
        self.channel_data = channel_data or {}
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------ auth
    def _headers(self, token: Optional[str]) -> dict:
        return {"Authorization": f"Bearer {token or self.secret}"}

    def start_conversation(self) -> dict:
        """Start a new conversation. Returns {conversationId, token, expires_in, streamUrl}."""
        payload = {"user": self.user}
        resp = self._session.post(
            f"{self.endpoint}/conversations",
            headers=self._headers(None),
            json=payload,
            timeout=30,
        )
        self._raise_for_status(resp, "start conversation")
        data = resp.json()
        # expires_in is documented in seconds; default to 1800 if absent.
        data.setdefault("expires_in", 1800)
        data["_obtained_at"] = time.time()
        log.info("Started conversation %s", data.get("conversationId"))
        return data

    def refresh_token(self, token: str) -> dict:
        resp = self._session.post(
            f"{self.endpoint}/tokens/refresh",
            headers=self._headers(token),
            timeout=30,
        )
        self._raise_for_status(resp, "refresh token")
        data = resp.json()
        data["_obtained_at"] = time.time()
        return data

    # -------------------------------------------------------------- activities
    def send_activity(self, conversation_id: str, token: str, activity: dict) -> str:
        """Send an activity; returns the server-assigned activity id."""
        activity.setdefault("type", "message")
        activity.setdefault("from", self.user)
        activity.setdefault("locale", self.locale)
        resp = self._session.post(
            f"{self.endpoint}/conversations/{conversation_id}/activities",
            headers=self._headers(token),
            json=activity,
            timeout=30,
        )
        self._raise_for_status(resp, "send activity")
        return resp.json().get("id", "")

    def send_message(
        self,
        conversation_id: str,
        token: str,
        text: str,
        extra_channel_data: Optional[dict] = None,
    ) -> str:
        channel_data = dict(self.channel_data)
        if extra_channel_data:
            channel_data.update(extra_channel_data)
        activity: dict[str, Any] = {
            "type": "message",
            "text": text,
            "textFormat": "plain",
        }
        if channel_data:
            activity["channelData"] = channel_data
        return self.send_activity(conversation_id, token, activity)

    def poll_activities(
        self, conversation_id: str, token: str, watermark: Optional[str] = None
    ) -> tuple[list[dict], Optional[str]]:
        """Poll once. Returns (activities, new_watermark)."""
        params = {"watermark": watermark} if watermark else None
        resp = self._session.get(
            f"{self.endpoint}/conversations/{conversation_id}/activities",
            headers=self._headers(token),
            params=params,
            timeout=30,
        )
        self._raise_for_status(resp, "poll activities")
        data = resp.json()
        return data.get("activities", []), data.get("watermark")

    # ------------------------------------------------------------- OAuthCard
    @staticmethod
    def find_oauth_card(activities: list[dict]) -> Optional[dict]:
        """Return the first OAuthCard attachment content found, or None."""
        for act in activities:
            for att in act.get("attachments", []) or []:
                if att.get("contentType") == OAUTH_CARD_CONTENT_TYPE:
                    return att.get("content", {})
        return None

    @staticmethod
    def extract_signin_url(oauth_card: dict) -> Optional[str]:
        """Extract the sign-in URL from an OAuthCard."""
        # Newer cards expose a top-level signinLink / tokenPostResource.
        for key in ("signinLink",):
            if oauth_card.get(key):
                return oauth_card[key]
        for btn in oauth_card.get("buttons", []) or []:
            if btn.get("value"):
                return btn["value"]
        return None

    # ------------------------------------------------- connection manager card
    @staticmethod
    def find_connection_card(activities: list[dict]) -> Optional[dict]:
        """Detect a Copilot Studio 'Open connection manager' Adaptive Card.

        This card is sent when a connector/MCP connection needs user-level
        authentication. It contains a link to .../user-connections and a
        Retry button (Action.Submit with {"action": "Retry"}).
        """
        for act in activities:
            for att in act.get("attachments", []) or []:
                ct = att.get("contentType", "") or ""
                if "adaptive" not in ct:
                    continue
                content = att.get("content", {}) or {}
                body = content.get("body", []) or []
                for item in body:
                    text = str(item.get("text", ""))
                    if "user-connections" in text or "connection manager" in text.lower():
                        return content
                    # Check nested ColumnSet/ActionSet for Retry action.
                    for col in item.get("columns", []) or []:
                        for sub in col.get("items", []) or []:
                            for act_def in sub.get("actions", []) or []:
                                data = act_def.get("data", {}) or {}
                                if data.get("action") == "Retry":
                                    return content
        return None

    @staticmethod
    def extract_connection_url(card: dict) -> Optional[str]:
        """Extract the user-connections URL from a connection card."""
        import re
        for item in card.get("body", []) or []:
            text = str(item.get("text", ""))
            m = re.search(r"https?://[^\s)]*user-connections[^\s)]*", text)
            if m:
                return m.group(0)
        return None

    def send_action_submit(
        self,
        conversation_id: str,
        token: str,
        data: dict,
    ) -> str:
        """Send an Action.Submit (like clicking a card button).

        For the connection-manager Retry button, data is {"action": "Retry"}.
        Returns the sent activity id.
        """
        activity = {
            "type": "message",
            "text": "",
            "value": data,
            "from": self.user,
        }
        return self.send_activity(conversation_id, token, activity)

    def submit_magic_code(
        self,
        conversation_id: str,
        token: str,
        code: str,
        mode: str = "auto",
    ) -> None:
        """Submit the magic code back to the conversation.

        mode:
          "invoke"  -> signin/verifyState invoke (modern Web Chat behaviour)
          "message" -> plain text message containing the code (OAuthPrompt
                       recognises a 6-digit code in a message)
          "auto"    -> invoke first; caller may fall back to message
        """
        if mode in ("invoke", "auto"):
            self.send_activity(
                conversation_id,
                token,
                {
                    "type": "invoke",
                    "name": "signin/verifyState",
                    "value": {"state": code},
                    "from": self.user,
                },
            )
        elif mode == "message":
            self.send_message(conversation_id, token, code)
        else:
            raise ValueError(f"Unknown code_submit_mode: {mode}")

    # ------------------------------------------------------------- turn loop
    def wait_for_bot_turn(
        self,
        conversation_id: str,
        token: str,
        sent_activity_id: Optional[str] = None,
        timeout: float = 60.0,
        quiet_seconds: float = 3.0,
        poll_interval: float = 1.0,
        stop_on_oauth_card: bool = True,
        initial_watermark: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """Poll until the bot finishes a turn.

        Collects bot-originated message / event / invokeResponse activities.
        A turn is considered complete when no new bot activity arrives for
        `quiet_seconds`, or when an OAuthCard is seen (if stop_on_oauth_card),
        or when `timeout` elapses.

        Returns (collected_activities, last_watermark).
        """
        deadline = time.time() + timeout
        watermark = initial_watermark
        collected: list[dict] = []
        seen_ids: set[str] = set()
        last_bot_at: Optional[float] = None
        first_bot_at: Optional[float] = None
        started = time.time()

        while time.time() < deadline:
            activities, watermark = self.poll_activities(conversation_id, token, watermark)
            for act in activities:
                act_id = act.get("id") or ""
                if act_id and act_id in seen_ids:
                    continue
                # Skip our own echoed message.
                if sent_activity_id and act_id == sent_activity_id:
                    continue
                if self._is_from_self(act):
                    continue
                if self._is_bot_activity(act):
                    if act_id:
                        seen_ids.add(act_id)
                    act["_received_at"] = time.time()
                    collected.append(act)
                    if first_bot_at is None:
                        first_bot_at = time.time()
                    last_bot_at = time.time()
                    if stop_on_oauth_card and self.find_oauth_card([act]):
                        log.info("OAuthCard received; pausing turn for sign-in.")
                        return collected, watermark

            if collected and last_bot_at is not None:
                if time.time() - last_bot_at >= quiet_seconds:
                    break
            time.sleep(poll_interval)

        log.debug(
            "Turn done: %d activities in %.1fs", len(collected), time.time() - started
        )
        return collected, watermark

    # --------------------------------------------------------------- helpers
    def _is_from_self(self, act: dict) -> bool:
        frm = act.get("from", {}) or {}
        return frm.get("id") == self.user.get("id")

    @staticmethod
    def _is_bot_activity(act: dict) -> bool:
        frm = act.get("from", {}) or {}
        if frm.get("role") == "bot":
            return True
        # Some channels don't set role; treat non-self message/event as bot.
        return act.get("type") in ("message", "event", "invokeResponse", "endOfConversation")

    @staticmethod
    def _raise_for_status(resp: requests.Response, action: str) -> None:
        if resp.status_code >= 400:
            raise DirectLineError(
                f"Direct Line {action} failed: {resp.status_code} {resp.text[:500]}"
            )


def activities_to_text(activities: list[dict]) -> str:
    """Flatten bot message activities (incl. adaptive card text) into a string."""
    parts: list[str] = []
    for act in activities:
        if act.get("type") != "message":
            continue
        if act.get("text"):
            parts.append(act["text"])
        for att in act.get("attachments", []) or []:
            content = att.get("content", {}) or {}
            if isinstance(content, dict):
                if content.get("type") == "AdaptiveCards" or att.get("contentType", "").startswith(
                    "application/vnd.microsoft.card.adaptive"
                ):
                    parts.extend(_adaptive_card_text(content))
                elif content.get("text"):
                    parts.append(content["text"])
    return "\n".join(p for p in parts if p)


def _adaptive_card_text(card: dict) -> list[str]:
    out: list[str] = []
    for item in card.get("body", []) or []:
        if isinstance(item, dict):
            if item.get("text"):
                out.append(item["text"])
            if item.get("type") == "Container":
                out.extend(_adaptive_card_text(item))
    return out
