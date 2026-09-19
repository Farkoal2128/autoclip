"""Optional Twitch VOD chat replay retrieval.

Twitch does not expose VOD chat replay through the public Helix API. This module
uses the same first-party GraphQL query Twitch's web clients use, and is called
only from the review/layout editor after a user explicitly asks to load chat.

Nothing in the highlight pipeline imports this module, so Twitch chat never
affects highlight detection or users who do not opt in.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

GQL_URL = "https://gql.twitch.tv/gql"
# Public client identifier used by Twitch web clients and established VOD-chat tools.
TWITCH_WEB_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
COMMENTS_QUERY_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
MAX_PAGES = 150
MAX_MESSAGES = 5000

_TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
_VIDEO_ID_RE = re.compile(r"(?:^|/)(?:videos|v|video)/(\d+)(?:/|$)")


class TwitchChatError(RuntimeError):
    """Chat replay could not be loaded."""


@dataclass(frozen=True)
class TwitchChatMessage:
    id: str
    offset_s: float
    username: str
    message: str
    user_color: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TwitchChatMessage":
        return cls(
            id=str(data.get("id") or ""),
            offset_s=float(data.get("offset_s") or 0.0),
            username=str(data.get("username") or ""),
            message=str(data.get("message") or ""),
            user_color=(str(data["user_color"]) if data.get("user_color") else None),
        )


def vod_id_from_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in _TWITCH_HOSTS:
        return None
    match = _VIDEO_ID_RE.search(parsed.path or "")
    return match.group(1) if match else None


async def fetch_vod_chat(
    vod_id: str,
    *,
    start_s: float,
    end_s: float,
) -> list[TwitchChatMessage]:
    """Fetch chat replay messages intersecting one clip's source-time range."""
    if end_s <= start_s:
        return []

    messages: list[TwitchChatMessage] = []
    seen: set[str] = set()
    cursor: str | None = None
    latest_offset = max(0.0, start_s - 1.0)

    headers = {"Client-ID": TWITCH_WEB_CLIENT_ID}
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, headers=headers) as client:
        for _ in range(MAX_PAGES):
            variables: dict[str, Any] = {"videoID": vod_id}
            if cursor:
                variables["cursor"] = cursor
            else:
                variables["contentOffsetSeconds"] = max(0, int(start_s))

            payload = {
                "operationName": "VideoCommentsByOffsetOrCursor",
                "variables": variables,
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": COMMENTS_QUERY_HASH,
                    }
                },
            }

            try:
                response = await client.post(GQL_URL, json=payload)
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise TwitchChatError(
                    "Twitch chat replay could not be loaded. Twitch's VOD chat interface "
                    "is unavailable or changed."
                ) from exc

            if body.get("errors"):
                raise TwitchChatError("Twitch returned an error while loading VOD chat replay.")

            video = ((body.get("data") or {}).get("video") or {})
            comments = video.get("comments")
            if comments is None:
                raise TwitchChatError(
                    "This Twitch VOD has no available chat replay, or Twitch did not return it."
                )

            edges = comments.get("edges") or []
            if not edges:
                break

            for edge in edges:
                node = (edge or {}).get("node") or {}
                try:
                    offset = float(node.get("contentOffsetSeconds"))
                except (TypeError, ValueError):
                    continue
                latest_offset = max(latest_offset, offset)
                if offset < start_s:
                    continue
                if offset > end_s:
                    continue

                commenter = node.get("commenter") or {}
                username = str(commenter.get("displayName") or commenter.get("login") or "").strip()
                fragments = ((node.get("message") or {}).get("fragments") or [])
                text = "".join(str(fragment.get("text") or "") for fragment in fragments).strip()
                if not username or not text:
                    continue

                message_id = str(node.get("id") or f"{offset:.3f}:{username}:{text[:32]}")
                if message_id in seen:
                    continue
                seen.add(message_id)

                color = (node.get("message") or {}).get("userColor")
                messages.append(
                    TwitchChatMessage(
                        id=message_id,
                        offset_s=offset,
                        username=username,
                        message=text,
                        user_color=str(color) if color else None,
                    )
                )
                if len(messages) >= MAX_MESSAGES:
                    return sorted(messages, key=lambda item: item.offset_s)

            page_info = comments.get("pageInfo") or {}
            if latest_offset >= end_s or not page_info.get("hasNextPage"):
                break

            cursor = str((edges[-1] or {}).get("cursor") or "")
            if not cursor:
                break

    return sorted(messages, key=lambda item: item.offset_s)
