import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import slack_mcp_server as slack


READ_ONLY_TOOLS = (
    slack.get_channel_history,
    slack.get_channel_id_by_name,
    slack.refresh_channel_cache,
    slack.list_channels,
    slack.refresh_user_cache,
    slack.search_users,
    slack.list_dms,
    slack.get_dm_channel,
    slack.whoami,
    slack.search_messages,
)

WRITE_TOOLS = (
    slack.post_message,
    slack.post_command,
    slack.add_reaction,
    slack.join_channel,
    slack.send_dm,
)


class ReadOnlyToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        slack._channel_cache.clear()
        slack._user_cache.clear()
        slack._user_name_cache.clear()

    def test_all_read_tools_avoid_slack_audit_logger(self) -> None:
        for tool in READ_ONLY_TOOLS:
            with self.subTest(tool=tool.__name__):
                self.assertNotIn("log_to_slack", inspect.getsource(tool))

    def test_write_tools_retain_slack_audit_logger(self) -> None:
        for tool in WRITE_TOOLS:
            with self.subTest(tool=tool.__name__):
                self.assertIn("log_to_slack", inspect.getsource(tool))

    async def test_read_tools_do_not_use_slack_audit_logger(self) -> None:
        slack._channel_cache["team-uxd"] = "C123"
        slack._user_cache["U123"] = "tiffany"
        slack._user_name_cache["U123"] = "Tiffany Nolan"

        empty_history = {
            "ok": True,
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        }
        empty_search = {
            "ok": True,
            "messages": {"matches": [], "pagination": {"page_count": 1}},
        }

        with (
            patch.object(slack, "log_to_slack", new_callable=AsyncMock) as slack_log,
            patch.object(slack, "_load_channels_to_cache", new_callable=AsyncMock, return_value=True),
            patch.object(slack, "_search_channels", new_callable=AsyncMock, return_value=[]),
            patch.object(slack, "make_request", new_callable=AsyncMock) as request,
            tempfile.TemporaryDirectory() as cache_dir,
            patch.object(slack, "USER_CACHE_FILE", Path(cache_dir) / "users.json"),
            patch.object(slack, "USER_NAME_CACHE_FILE", Path(cache_dir) / "names.json"),
        ):
            request.side_effect = [
                empty_history,
                {"ok": True, "channels": []},
                {"ok": True, "user": "tiffany"},
                empty_search,
            ]

            await slack.get_channel_history("C123", limit=1)
            await slack.get_channel_id_by_name("team-uxd")
            await slack.refresh_channel_cache()
            await slack.list_channels(query="hummingbird")
            await slack.search_users("Tiffany")
            await slack.refresh_user_cache()
            await slack.list_dms(limit=1)
            await slack.whoami()
            await slack.search_messages(query="after:yesterday", limit=1)

            slack_log.assert_not_awaited()
            requested_urls = [call.args[0] for call in request.await_args_list]
            self.assertFalse(any(url.endswith("/conversations.join") for url in requested_urls))
            self.assertFalse(any(url.endswith("/chat.postMessage") for url in requested_urls))

    async def test_get_dm_channel_prevents_conversation_creation(self) -> None:
        with (
            patch.object(slack, "resolve_user_id", new_callable=AsyncMock, return_value="U123"),
            patch.object(slack, "get_user_handle", new_callable=AsyncMock, return_value="tiffany"),
            patch.object(
                slack,
                "make_request",
                new_callable=AsyncMock,
                return_value={"ok": True, "channel": {"id": "D123"}},
            ) as request,
            patch.object(slack, "log_to_slack", new_callable=AsyncMock) as slack_log,
        ):
            result = await slack.get_dm_channel("tiffany")

            self.assertEqual(result["channel_id"], "D123")
            request.assert_awaited_once()
            self.assertTrue(request.await_args.kwargs["payload"]["prevent_creation"])
            slack_log.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
