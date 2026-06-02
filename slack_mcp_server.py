import os
from typing import Any, Literal
import httpx
from mcp.server.fastmcp import FastMCP
import re
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

SLACK_API_BASE = "https://slack.com/api"
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
LOGS_CHANNEL_ID = os.environ["LOGS_CHANNEL_ID"]
OUTPUT_FORMAT = os.environ.get("OUTPUT_FORMAT", "compact").lower()

# Cache file path (in same directory as script)
SCRIPT_DIR = Path(__file__).parent
USER_CACHE_FILE = SCRIPT_DIR / ".user_cache.json"
USER_NAME_CACHE_FILE = SCRIPT_DIR / ".user_name_cache.json"

# Cache for channel name to ID mapping
_channel_cache: dict[str, str] = {}

# Cache for user ID to handle mapping
_user_cache: dict[str, str] = {}

# Cache for user ID to username (name field) mapping for reverse lookups
_user_name_cache: dict[str, str] = {}

mcp = FastMCP(
    "slack", settings={"host": "127.0.0.1" if MCP_TRANSPORT == "stdio" else "0.0.0.0"}
)


async def make_request(
    url: str, method: str = "POST", payload: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    if MCP_TRANSPORT == "stdio":
        xoxc_token = os.environ["SLACK_XOXC_TOKEN"]
        xoxd_token = os.environ["SLACK_XOXD_TOKEN"]
        user_agent = "MCP-Server/1.0"
    else:
        request_headers = mcp.get_context().request_context.request.headers
        xoxc_token = request_headers["X-Slack-Web-Token"]
        xoxd_token = request_headers["X-Slack-Cookie-Token"]
        user_agent = request_headers.get("User-Agent", "MCP-Server/1.0")

    headers = {
        "Authorization": f"Bearer {xoxc_token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    }

    cookies = {"d": xoxd_token}

    async with httpx.AsyncClient(cookies=cookies) as client:
        try:
            if method.upper() == "GET":
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=payload,
                    timeout=30.0,
                )
            else:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                    timeout=30.0,
                )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(e)
            return None


async def log_to_slack(message: str):
    await post_message(LOGS_CHANNEL_ID, message, skip_log=True)


def parse_timestamp(date_str: str, is_end_of_range: bool = False) -> str:
    """Convert various date formats to Slack Unix timestamp.

    Args:
        date_str: Unix timestamp or ISO 8601 date string
        is_end_of_range: If True and date has no time, use end of day (23:59:59.999999)
                        If False and date has no time, use start of day (00:00:00)

    Returns:
        Unix timestamp as string with microsecond precision
    """
    if not date_str:
        return ""

    # Already a Unix timestamp (with or without microseconds)
    if re.match(r"^\d+(\.\d+)?$", date_str):
        return date_str

    # Parse ISO 8601 date
    try:
        # Check if it's a date-only format (no time component)
        is_date_only = re.match(r"^\d{4}-\d{2}-\d{2}$", date_str)

        if is_date_only:
            # Parse date and set time based on whether it's start or end of range
            dt = datetime.fromisoformat(date_str)
            if is_end_of_range:
                # End of day: 23:59:59.999999
                dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)
            else:
                # Start of day: 00:00:00
                dt = dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        else:
            # Has time component, parse as-is
            # Handle both with and without timezone
            if 'Z' in date_str:
                date_str = date_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(date_str)
            # If no timezone info, assume UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

        # Convert to Unix timestamp with microsecond precision
        return f"{dt.timestamp():.6f}"
    except ValueError as e:
        print(f"Error parsing date '{date_str}': {e}")
        return ""


def _load_user_cache() -> None:
    """Load user cache from disk."""
    global _user_cache, _user_name_cache

    try:
        if USER_CACHE_FILE.exists():
            with open(USER_CACHE_FILE, 'r') as f:
                _user_cache = json.load(f)
            print(f"Loaded {len(_user_cache)} user handles from cache")
    except Exception as e:
        print(f"Error loading user cache: {e}")
        _user_cache = {}

    try:
        if USER_NAME_CACHE_FILE.exists():
            with open(USER_NAME_CACHE_FILE, 'r') as f:
                _user_name_cache = json.load(f)
            print(f"Loaded {len(_user_name_cache)} usernames from cache")
    except Exception as e:
        print(f"Error loading user name cache: {e}")
        _user_name_cache = {}


def _save_user_cache() -> None:
    """Save user cache to disk."""
    try:
        with open(USER_CACHE_FILE, 'w') as f:
            json.dump(_user_cache, f, indent=2)
    except Exception as e:
        print(f"Error saving user cache: {e}")

    try:
        if _user_name_cache:
            with open(USER_NAME_CACHE_FILE, 'w') as f:
                json.dump(_user_name_cache, f, indent=2)
    except Exception as e:
        print(f"Error saving user name cache: {e}")


async def get_user_handle(user_id: str) -> str:
    """Get user handle by ID with caching. Returns user_id if lookup fails."""
    global _user_cache

    if not user_id:
        return ""

    # Check cache first
    if user_id in _user_cache:
        return _user_cache[user_id]

    # Cache miss - fetch user info
    url = f"{SLACK_API_BASE}/users.info"
    payload = {"user": user_id}
    data = await make_request(url, method="GET", payload=payload)

    if data and data.get("ok"):
        user = data.get("user", {})
        profile = user.get("profile", {})
        # Prefer display_name, fall back to real_name, then name
        handle = (
            profile.get("display_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
        _user_cache[user_id] = handle
        _save_user_cache()  # Persist to disk
        return handle

    # If lookup fails, cache the user_id itself to avoid repeated failed lookups
    _user_cache[user_id] = user_id
    _save_user_cache()  # Persist to disk
    return user_id


async def replace_user_mentions(text: str) -> str:
    """Replace user ID mentions (<@USERID>) with handles (@handle)."""
    if not text:
        return text

    # Find all user mentions in the format <@USERID>
    mention_pattern = r'<@([A-Z0-9]+)>'
    matches = re.finditer(mention_pattern, text)

    # Process each mention
    replacements = {}
    for match in matches:
        user_id = match.group(1)
        if user_id not in replacements:
            handle = await get_user_handle(user_id)
            replacements[user_id] = handle

    # Replace all mentions with handles
    for user_id, handle in replacements.items():
        text = text.replace(f'<@{user_id}>', f'@{handle}')

    return text


async def _load_users_to_cache() -> bool:
    """Load all users into the cache via users.list. Returns True if successful."""
    global _user_cache

    url = f"{SLACK_API_BASE}/users.list"
    cursor = None

    while True:
        payload = {"limit": 200}
        if cursor:
            payload["cursor"] = cursor

        data = await make_request(url, method="GET", payload=payload)

        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
            print(f"Error loading users to cache: {error_msg}")
            return False

        members = data.get("members", [])
        for member in members:
            if member.get("deleted"):
                continue
            uid = member.get("id", "")
            profile = member.get("profile", {})
            handle = (
                profile.get("display_name")
                or member.get("real_name")
                or member.get("name")
                or uid
            )
            if uid:
                _user_cache[uid] = handle
                # Also store username (name field) for reverse lookup
                name = member.get("name", "")
                if name:
                    _user_name_cache[uid] = name

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    _save_user_cache()
    print(f"Loaded {len(_user_cache)} users into cache")
    return True


async def resolve_user_id(user: str) -> str:
    """Resolve a username/handle to a Slack user ID. Accepts a user ID, handle, or display name."""
    # Already a user ID (starts with U or W followed by alphanumeric)
    if re.match(r'^[UW][A-Z0-9]+$', user):
        return user

    # Search the user cache for a matching handle or username
    for uid, handle in _user_cache.items():
        if handle.lower() == user.lower():
            return uid
    for uid, name in _user_name_cache.items():
        if name.lower() == user.lower():
            return uid

    # Cache miss - load all users and try again
    print(f"User '{user}' not found in cache, loading all users...")
    if await _load_users_to_cache():
        for uid, handle in _user_cache.items():
            if handle.lower() == user.lower():
                return uid
        for uid, name in _user_name_cache.items():
            if name.lower() == user.lower():
                return uid

    # Still not found - try users.search as last resort
    url = f"{SLACK_API_BASE}/users.search"
    payload = {"query": user}
    data = await make_request(url, method="GET", payload=payload)

    if data and data.get("ok"):
        members = data.get("members", [])
        for member in members:
            profile = member.get("profile", {})
            display_name = profile.get("display_name", "").lower()
            real_name = member.get("real_name", "").lower()
            name = member.get("name", "").lower()
            if user.lower() in (display_name, real_name, name):
                uid = member["id"]
                handle = profile.get("display_name") or member.get("real_name") or member.get("name") or uid
                _user_cache[uid] = handle
                _save_user_cache()
                return uid
        # If exact match not found but there are results, use the first one
        if members:
            uid = members[0]["id"]
            profile = members[0].get("profile", {})
            handle = profile.get("display_name") or members[0].get("real_name") or members[0].get("name") or uid
            _user_cache[uid] = handle
            _save_user_cache()
            return uid

    return user  # Return as-is if resolution fails


async def filter_message_fields(message: dict[str, Any]) -> dict[str, Any] | str:
    """Filter message to only essential fields to reduce token usage."""
    # Extract essential fields
    text = message.get("text", "")
    user_id = message.get("user", "")
    ts = message.get("ts", "")
    thread_ts = message.get("thread_ts", "")

    # Get user handle instead of ID
    user_handle = await get_user_handle(user_id) if user_id else ""

    # Replace user mentions in text with handles
    text = await replace_user_mentions(text)

    if OUTPUT_FORMAT == "json":
        # Return structured JSON format with handle instead of ID
        filtered = {
            "text": text,
            "user": user_handle,
            "ts": ts,
        }
        if thread_ts:
            filtered["thread_ts"] = thread_ts
        return filtered
    else:
        # Return compact text format (default)
        result = f"[{ts}] @{user_handle}: {text}"
        if thread_ts and thread_ts != ts:
            result += f" [thread:{thread_ts}]"
        return result


# Validate and convert thread_ts if needed
def convert_thread_ts(ts: str) -> str:
    # If ts is already in the correct format, return as is
    if re.match(r"^\d+\.\d+$", ts):
        return ts
    # If ts is a long integer string (from Slack URL), convert it
    if re.match(r"^\d{16}$", ts):
        return f"{ts[:10]}.{ts[10:]}"
    return ""


async def get_thread_replies(channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    """Get all replies in a thread."""
    url = f"{SLACK_API_BASE}/conversations.replies"
    payload = {"channel": channel_id, "ts": thread_ts}

    data = await make_request(url, method="GET", payload=payload)

    if not data or not data.get("ok"):
        error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
        print(f"Error getting thread replies: {error_msg}")
        return []

    # Returns all messages including the parent, so we skip the first one
    messages = data.get("messages", [])
    return messages[1:] if len(messages) > 1 else []


@mcp.tool()
async def get_channel_history(
    channel_id: str,
    limit: int = 1000,
    oldest: str = "",
    latest: str = "",
    include_threads: bool = False
) -> list[dict[str, Any] | str]:
    """Get the history of a channel with pagination support. Limit parameter controls max messages to fetch (default 1000).

    Optional date filtering (accepts ISO 8601 dates or Unix timestamps):
    - oldest: Only messages after this date (e.g., "2024-01-15" or "2024-01-15T10:30:00")
    - latest: Only messages before this date (e.g., "2024-01-20" or "2024-01-20T18:00:00")
    - include_threads: If True, also fetch all replies in threads (default False)

    Note: For date-only formats, 'oldest' defaults to start of day (00:00:00) and 'latest' to end of day (23:59:59).
    """
    await log_to_slack(f"Getting history of channel <#{channel_id}> (limit: {limit}, include_threads: {include_threads})")
    url = f"{SLACK_API_BASE}/conversations.history"

    # Parse timestamp parameters
    oldest_ts = parse_timestamp(oldest, is_end_of_range=False)
    latest_ts = parse_timestamp(latest, is_end_of_range=True)

    all_messages = []
    cursor = None

    while len(all_messages) < limit:
        payload = {"channel": channel_id, "limit": min(200, limit - len(all_messages))}
        if oldest_ts:
            payload["oldest"] = oldest_ts
        if latest_ts:
            payload["latest"] = latest_ts
        if cursor:
            payload["cursor"] = cursor

        data = await make_request(url, method="GET", payload=payload)

        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
            print(f"Error getting channel history: {error_msg}")
            break

        messages = data.get("messages", [])
        all_messages.extend(messages)

        # Check if there are more messages
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    print(f"Retrieved {len(all_messages)} messages from channel {channel_id}")

    # Fetch thread replies if requested
    if include_threads:
        thread_messages = []
        for msg in all_messages:
            # Check if message has replies (is a parent message)
            reply_count = msg.get("reply_count", 0)
            if reply_count > 0:
                thread_ts = msg.get("ts")
                if thread_ts:
                    replies = await get_thread_replies(channel_id, thread_ts)
                    thread_messages.extend(replies)

        all_messages.extend(thread_messages)
        print(f"Retrieved {len(thread_messages)} additional messages from threads")

    # Pre-fetch all unique user handles to avoid duplicate API calls
    # Include both message authors and users mentioned in text
    unique_users = {msg.get("user") for msg in all_messages if msg.get("user")}

    # Extract user IDs from mentions in message text
    mention_pattern = r'<@([A-Z0-9]+)>'
    for msg in all_messages:
        text = msg.get("text", "")
        if text:
            mentioned_users = re.findall(mention_pattern, text)
            unique_users.update(mentioned_users)

    # Fetch all unique users
    for user_id in unique_users:
        await get_user_handle(user_id)

    # Filter messages to reduce token usage (now all users are cached)
    return await asyncio.gather(*[filter_message_fields(msg) for msg in all_messages])


async def _search_channels(query: str, only_member: bool = True) -> list[dict[str, str]]:
    """Search for channels by keyword using search.modules (works on Enterprise Grid).
    By default only returns channels the user is a member of."""
    url = f"{SLACK_API_BASE}/search.modules"
    payload = {"query": query, "module": "channels", "count": 50}
    data = await make_request(url, payload=payload)

    if not data or not data.get("ok"):
        return []

    results = []
    for item in data.get("items", []):
        ch = item.get("channel", item)
        if only_member and not ch.get("is_member", False):
            continue
        name = ch.get("name", "")
        cid = ch.get("id", "")
        if name and cid:
            results.append({"id": cid, "name": name})
            _channel_cache[name] = cid

    return results


async def _load_channels_to_cache() -> bool:
    """Load channels into the cache. Uses conversations.list with cursor-based
    pagination, falling back to search.modules on Enterprise Grid workspaces
    where conversations.list is restricted."""
    global _channel_cache

    url = f"{SLACK_API_BASE}/conversations.list"
    _channel_cache.clear()
    cursor = None

    while True:
        payload: dict[str, str] = {
            "exclude_archived": "true",
            "types": "public_channel,private_channel",
            "limit": "1000",
        }
        if cursor:
            payload["cursor"] = cursor

        data = await make_request(url, method="GET", payload=payload)

        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
            print(f"conversations.list unavailable ({error_msg}), using search.modules fallback")
            return False

        for channel in data.get("channels", []):
            channel_name = channel.get("name", "")
            channel_id = channel.get("id", "")
            if channel_name and channel_id:
                _channel_cache[channel_name] = channel_id

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    print(f"Loaded {len(_channel_cache)} channels into cache")
    return True


@mcp.tool()
async def get_channel_id_by_name(channel_name: str) -> str:
    """Get the channel ID by channel name. The channel name can be with or without the # prefix."""
    # Remove # prefix if present
    clean_name = channel_name.lstrip("#")
    await log_to_slack(f"Looking up channel ID for channel name: {clean_name}")

    # Check cache first
    if clean_name in _channel_cache:
        print(f"Channel '{clean_name}' found in cache")
        return _channel_cache[clean_name]

    # Try loading full channel list
    print(f"Cache miss for '{clean_name}', loading channels...")
    if await _load_channels_to_cache():
        if clean_name in _channel_cache:
            return _channel_cache[clean_name]

    # Fallback: search for the channel by name (Enterprise Grid)
    results = await _search_channels(clean_name)
    for ch in results:
        if ch["name"] == clean_name:
            return ch["id"]

    print(f"Channel '{clean_name}' not found")
    return ""


@mcp.tool()
async def refresh_channel_cache() -> bool:
    """Refresh the channel cache. Use this when new channels are created or if channel lookups are failing."""
    await log_to_slack("Refreshing channel cache")
    return await _load_channels_to_cache()


@mcp.tool()
async def list_channels(query: str = "", only_member: bool = True) -> list[dict[str, str]]:
    """List channels in the workspace. Optionally filter by keyword (matches against channel name).
    Returns a list of channels with their ID and name. Use this to find channels when you don't know the exact name.
    Set only_member=False to include channels you haven't joined."""
    await log_to_slack(f"Listing channels (query: '{query}', only_member: {only_member})")

    # If a query is provided, use search.modules directly (works on Enterprise Grid)
    if query:
        results = await _search_channels(query, only_member=only_member)
        if results:
            return results

    # Fall back to cache-based lookup
    if not _channel_cache:
        await _load_channels_to_cache()

    results = []
    for name, channel_id in sorted(_channel_cache.items()):
        if not query or query.lower() in name.lower():
            results.append({"id": channel_id, "name": name})

    return results


@mcp.tool()
async def refresh_user_cache() -> int:
    """Clear the user cache. Use this when user handles are outdated or if user lookups are failing. Returns the number of cached entries cleared."""
    global _user_cache, _user_name_cache
    await log_to_slack("Clearing user cache")
    count = len(_user_cache)
    _user_cache.clear()
    _user_name_cache.clear()

    # Also remove cache files
    try:
        if USER_CACHE_FILE.exists():
            USER_CACHE_FILE.unlink()
        if USER_NAME_CACHE_FILE.exists():
            USER_NAME_CACHE_FILE.unlink()
        print(f"Cleared {count} user cache entries and deleted cache files")
    except Exception as e:
        print(f"Cleared {count} user cache entries but failed to delete cache files: {e}")

    return count


@mcp.tool()
async def search_users(query: str) -> list[dict[str, str]]:
    """Search for users by name, handle, or username. Returns matching users with their ID, handle, and username.
    Use this to find the correct user when a DM or mention fails, or when you're unsure of someone's exact Slack handle."""
    await log_to_slack(f"Searching for users: {query}")

    # Ensure caches are populated
    if not _user_name_cache:
        await _load_users_to_cache()

    query_lower = query.lower()
    results = []

    for uid, handle in _user_cache.items():
        username = _user_name_cache.get(uid, "")
        if query_lower in handle.lower() or query_lower in username.lower():
            results.append({
                "user_id": uid,
                "handle": handle,
                "username": username,
            })

    return results


@mcp.tool()
async def list_dms(limit: int = 50) -> list[dict[str, str]]:
    """List your direct message conversations, sorted by most recent activity.
    Returns DM channel IDs and the other user's handle. Use get_channel_history with the returned channel ID to read DM messages."""
    await log_to_slack(f"Listing DMs (limit: {limit})")
    url = f"{SLACK_API_BASE}/conversations.list"
    payload = {"types": "im", "limit": min(limit, 200)}

    data = await make_request(url, method="GET", payload=payload)

    if not data or not data.get("ok"):
        error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
        print(f"Error listing DMs: {error_msg}")
        return []

    conversations = data.get("channels", [])
    results = []
    for conv in conversations[:limit]:
        user_id = conv.get("user", "")
        handle = await get_user_handle(user_id) if user_id else "unknown"
        results.append({
            "channel_id": conv.get("id", ""),
            "user": handle,
            "user_id": user_id,
        })

    return results


@mcp.tool()
async def get_dm_channel(user: str) -> dict[str, str]:
    """Get the DM channel ID for a specific user. Accepts a Slack handle, display name, or user ID.
    Use the returned channel_id with get_channel_history to read the full DM thread."""
    resolved_id = await resolve_user_id(user)
    if resolved_id == user and not re.match(r'^[UW][A-Z0-9]+$', user):
        return {"error": f"Could not resolve user '{user}' to a Slack user ID"}

    url = f"{SLACK_API_BASE}/conversations.open"
    payload = {"users": resolved_id, "return_dm": True}
    data = await make_request(url, payload=payload)

    if data and data.get("ok"):
        channel = data.get("channel", {})
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        if channel_id:
            handle = await get_user_handle(resolved_id)
            return {"channel_id": channel_id, "user_id": resolved_id, "user": handle}

    error = data.get("error", "Unknown error") if data else "No response"
    return {"error": f"Could not open DM channel: {error}"}


@mcp.tool()
async def post_message(
    channel_id: str, message: str, thread_ts: str = "", skip_log: bool = False
) -> bool:
    """Post a message to a channel."""
    if not skip_log:
        await log_to_slack(f"Posting message to channel <#{channel_id}>: {message}")
    await join_channel(channel_id, skip_log=skip_log)
    url = f"{SLACK_API_BASE}/chat.postMessage"
    payload = {"channel": channel_id, "text": message}
    if thread_ts:
        payload["thread_ts"] = convert_thread_ts(thread_ts)
    data = await make_request(url, payload=payload)
    return data.get("ok")


@mcp.tool()
async def post_command(
    channel_id: str, command: str, text: str, skip_log: bool = False
) -> bool:
    """Post a command to a channel."""
    if not skip_log:
        await log_to_slack(
            f"Posting command to channel <#{channel_id}>: {command} {text}"
        )
    await join_channel(channel_id, skip_log=skip_log)
    url = f"{SLACK_API_BASE}/chat.command"
    payload = {"channel": channel_id, "command": command, "text": text}
    data = await make_request(url, payload=payload)
    return data.get("ok")


@mcp.tool()
async def add_reaction(channel_id: str, message_ts: str, reaction: str) -> bool:
    """Add a reaction to a message."""
    await log_to_slack(
        f"Adding reaction to message {message_ts} in channel <#{channel_id}>: :{reaction}:"
    )
    url = f"{SLACK_API_BASE}/reactions.add"
    payload = {
        "channel": channel_id,
        "name": reaction,
        "timestamp": convert_thread_ts(message_ts),
    }
    data = await make_request(url, payload=payload)
    return data.get("ok")


@mcp.tool()
async def whoami() -> str:
    """Checks authentication & identity."""
    await log_to_slack("Checking authentication & identity")
    url = f"{SLACK_API_BASE}/auth.test"
    data = await make_request(url)
    return data.get("user")


@mcp.tool()
async def join_channel(channel_id: str, skip_log: bool = False) -> bool:
    """Join a channel."""
    if not skip_log:
        await log_to_slack(f"Joining channel <#{channel_id}>")
    url = f"{SLACK_API_BASE}/conversations.join"
    payload = {"channel": channel_id}
    data = await make_request(url, payload=payload)
    return data.get("ok")


@mcp.tool()
async def send_dm(user_id: str, message: str) -> bool:
    """Send a direct message to a user. Accepts a Slack user ID, handle, or display name."""
    resolved_id = await resolve_user_id(user_id)
    print(f"[send_dm] Input: '{user_id}' -> Resolved to: '{resolved_id}'")
    await log_to_slack(f"Sending direct message to user <@{resolved_id}>: {message}")
    url = f"{SLACK_API_BASE}/conversations.open"
    payload = {"users": resolved_id, "return_dm": True}
    data = await make_request(url, payload=payload)
    print(f"[send_dm] conversations.open response: {data}")
    if data and data.get("ok"):
        channel = data.get("channel", {})
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        if not channel_id:
            print(f"[send_dm] No channel ID in response. Channel data: {channel}")
            return False
        print(f"[send_dm] Sending to channel: {channel_id}")
        return await post_message(channel_id, message)
    error = data.get("error", "Unknown error") if data else "No response"
    print(f"[send_dm] conversations.open failed: {error}")
    return False


@mcp.tool()
async def search_messages(
    query: str, sort: Literal["timestamp", "score"] = "timestamp", limit: int = 1000
) -> list[dict[str, Any] | str]:
    """Search for messages in the workspace with pagination support. Limit parameter controls max results to fetch (default 1000)."""
    await log_to_slack(f"Searching for messages: {query} (limit: {limit})")
    url = f"{SLACK_API_BASE}/search.messages"

    all_matches = []
    page = 1

    while len(all_matches) < limit:
        payload = {
            "query": query,
            "sort": sort,
            "count": min(100, limit - len(all_matches)),  # Slack max is 100 per page
            "page": page
        }

        data = await make_request(url, method="GET", payload=payload)

        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
            print(f"Error searching messages: {error_msg}")
            break

        messages_data = data.get("messages", {})
        matches = messages_data.get("matches", [])
        all_matches.extend(matches)

        # Check pagination info
        total_pages = messages_data.get("pagination", {}).get("page_count", 1)
        if page >= total_pages or len(matches) == 0:
            break

        page += 1

    print(f"Retrieved {len(all_matches)} search results for query: {query}")

    # Pre-fetch all unique user handles to avoid duplicate API calls
    # Include both message authors and users mentioned in text
    unique_users = {msg.get("user") for msg in all_matches if msg.get("user")}

    # Extract user IDs from mentions in message text
    mention_pattern = r'<@([A-Z0-9]+)>'
    for msg in all_matches:
        text = msg.get("text", "")
        if text:
            mentioned_users = re.findall(mention_pattern, text)
            unique_users.update(mentioned_users)

    # Fetch all unique users
    for user_id in unique_users:
        await get_user_handle(user_id)

    # Filter messages to reduce token usage (now all users are cached)
    return await asyncio.gather(*[filter_message_fields(msg) for msg in all_matches])


if __name__ == "__main__":
    # Load user cache from disk on startup
    _load_user_cache()
    mcp.run(transport=MCP_TRANSPORT)
