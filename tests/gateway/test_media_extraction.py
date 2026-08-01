"""
Tests for MEDIA tag extraction from tool results.

Verifies that MEDIA tags (e.g., from TTS tool) are only extracted from
messages in the CURRENT turn, not from the full conversation history.
This prevents voice messages from accumulating and being sent multiple
times per reply. (Regression test for #160)

Also covers #34608: a stale MEDIA: path emitted by an execute_code /
make_image tool several turns earlier must not leak onto a later
text-only reply, even when the path-based dedup set fails to capture it.
"""

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def extract_media_tags_fixed(result_messages, history_len):
    """
    Extract MEDIA tags from tool results, but ONLY from new messages
    (those added after history_len). This is the fixed behavior.
    
    Args:
        result_messages: Full list of messages including history + new
        history_len: Length of history before this turn
        
    Returns:
        Tuple of (media_tags list, has_voice_directive bool)
    """
    media_tags = []
    has_voice_directive = False
    
    # Only process new messages from this turn
    new_messages = result_messages[history_len:] if len(result_messages) > history_len else []
    
    for msg in new_messages:
        if msg.get("role") == "tool" or msg.get("role") == "function":
            content = msg.get("content", "")
            if "MEDIA:" in content:
                for match in re.finditer(r'MEDIA:(\S+)', content):
                    path = match.group(1).strip().rstrip('",}')
                    if path:
                        media_tags.append(f"MEDIA:{path}")
                if "[[audio_as_voice]]" in content:
                    has_voice_directive = True
    
    return media_tags, has_voice_directive


def extract_media_tags_production(result_messages, history_len, history_media_paths):
    """Mirror of the production scan in gateway/run.py after the #34608 fix.

    Primary guard: scope the scan to the current turn via ``history_len``
    slicing (matching how ``agent_history`` is passed as
    ``conversation_history`` into ``run_conversation``). Secondary guard:
    path-based dedup against ``history_media_paths`` (the #160 compression-safe
    fallback, also used when compression shrinks the list below history_len).
    """
    media_tags = []
    has_voice_directive = False

    if len(result_messages) >= history_len and history_len:
        scan_msgs = result_messages[history_len:]
    else:
        scan_msgs = result_messages

    for msg in scan_msgs:
        if msg.get("role") == "tool" or msg.get("role") == "function":
            content = msg.get("content", "")
            if "MEDIA:" in content:
                for match in re.finditer(r'MEDIA:(\S+)', content):
                    path = match.group(1).strip().rstrip('",}')
                    if path and path not in history_media_paths:
                        media_tags.append(f"MEDIA:{path}")
                if "[[audio_as_voice]]" in content:
                    has_voice_directive = True

    return media_tags, has_voice_directive


def extract_media_tags_broken(result_messages):
    """
    The BROKEN behavior: extract MEDIA tags from ALL messages including history.
    This causes TTS voice messages to accumulate and be re-sent on every reply.
    """
    media_tags = []
    has_voice_directive = False
    
    for msg in result_messages:
        if msg.get("role") == "tool" or msg.get("role") == "function":
            content = msg.get("content", "")
            if "MEDIA:" in content:
                for match in re.finditer(r'MEDIA:(\S+)', content):
                    path = match.group(1).strip().rstrip('",}')
                    if path:
                        media_tags.append(f"MEDIA:{path}")
                if "[[audio_as_voice]]" in content:
                    has_voice_directive = True
    
    return media_tags, has_voice_directive


class TestMediaExtraction:
    """Tests for MEDIA tag extraction from tool results."""

    def test_gateway_auto_append_ignores_media_examples_in_skill_docs(self):
        """Skill/documentation examples must not be appended as real attachments."""
        from gateway.run import _collect_auto_append_media_tags

        messages = [
            {"role": "user", "content": "How should I format gateway media?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_skill", "function": {"name": "skill_view"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_skill",
                "content": """
Recommended pattern:
```text
MEDIA:/absolute/path/to/image.png
```
Second message:
```text
caption
```
""",
            },
            {"role": "assistant", "content": "Use a standalone media message."},
        ]

        tags, voice = _collect_auto_append_media_tags(messages, history_offset=0)
        assert tags == []
        assert voice is False

    def test_gateway_auto_append_keeps_real_tts_media_tag(self):
        """TTS tool media tags are still auto-appended when the model omits them."""
        from gateway.run import _collect_auto_append_media_tags

        messages = [
            {"role": "user", "content": "Say this as audio"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_tts", "function": {"name": "text_to_speech"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_tts",
                "content": '{"success": true, "media_tag": "[[audio_as_voice]]\\nMEDIA:/tmp/voice.ogg"}',
            },
            {"role": "assistant", "content": "Done."},
        ]

        tags, voice = _collect_auto_append_media_tags(messages, history_offset=0)
        assert tags == ["MEDIA:/tmp/voice.ogg"]
        assert voice is True

    def test_gateway_auto_append_image_generate_json_path(self):
        """image_generate returns a local path in JSON (no MEDIA: tag); it is
        auto-appended so delivery doesn't depend on the model restating it."""
        from gateway.run import _collect_auto_append_media_tags

        messages = [
            {"role": "user", "content": "Make me a cat"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_img", "function": {"name": "image_generate"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_img",
                "content": '{"success": true, "image": "/tmp/gen/cat.png", "agent_visible_image": "/tmp/gen/cat.png"}',
            },
            {"role": "assistant", "content": "Here's your cat."},
        ]

        tags, voice = _collect_auto_append_media_tags(messages, history_offset=0)
        assert tags == ["MEDIA:/tmp/gen/cat.png"]
        assert voice is False

    def test_gateway_auto_append_image_generate_prefers_host_path(self):
        """When host and sandbox paths differ, the host-deliverable path wins."""
        from gateway.run import _collect_auto_append_media_tags

        messages = [
            {"role": "user", "content": "Make me a dog"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_img", "function": {"name": "image_generate"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_img",
                "content": '{"success": true, "host_image": "/host/dog.jpg", "image": "/host/dog.jpg", "agent_visible_image": "/sandbox/dog.jpg"}',
            },
        ]

        tags, _ = _collect_auto_append_media_tags(messages, history_offset=0)
        assert tags == ["MEDIA:/host/dog.jpg"]

    def test_gateway_auto_append_image_generate_failure_and_url_ignored(self):
        """Failed generations and remote URLs are not auto-delivered."""
        from gateway.run import _collect_auto_append_media_tags

        def _img_msgs(content):
            return [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "c", "function": {"name": "image_generate"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "c", "content": content},
            ]

        # Failed generation
        tags, _ = _collect_auto_append_media_tags(
            _img_msgs('{"success": false, "image": null, "error": "boom"}'),
            history_offset=0,
        )
        assert tags == []

        # Remote URL is not a local file path
        tags, _ = _collect_auto_append_media_tags(
            _img_msgs('{"success": true, "image": "https://fal.media/x/cat.png"}'),
            history_offset=0,
        )
        assert tags == []

    def test_gateway_auto_append_image_generate_dedupes_history(self):
        """A generated image path already in history is not re-sent."""
        from gateway.run import _collect_auto_append_media_tags

        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c", "function": {"name": "image_generate"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c",
                "content": '{"success": true, "image": "/tmp/gen/cat.png"}',
            },
        ]

        tags, _ = _collect_auto_append_media_tags(
            messages, history_offset=0, history_media_paths={"/tmp/gen/cat.png"}
        )
        assert tags == []

    def test_gateway_auto_attaches_qa_screenshots_used_by_vision(self):
        """A QA manifest plus a matching vision call is explicit evidence.

        This mirrors a visual-QA script that writes screenshot paths to a JSON
        result, then asks ``vision_analyze`` to inspect each captured image.
        The final prose can omit the individual paths without losing Discord
        attachments.
        """
        from gateway.run import _collect_auto_append_media_tags

        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "manifest", "function": {"name": "read_file"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "manifest",
                "content": '13| "screenshot": "/tmp/qa/desktop.png"',
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "vision",
                        "function": {
                            "name": "vision_analyze",
                            "arguments": '{"image_url": "/tmp/qa/desktop.png", "question": "Check this QA screenshot"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "vision",
                "content": '{"success": true, "analysis": "looks good"}',
            },
            {"role": "assistant", "content": "QA finished; see the report directory."},
        ]

        tags, voice = _collect_auto_append_media_tags(messages, history_offset=0)

        assert tags == ["MEDIA:/tmp/qa/desktop.png"]
        assert voice is False

    def test_gateway_does_not_attach_an_unproven_vision_input(self):
        """A local image alone might be a user upload, not a new screenshot."""
        from gateway.run import _collect_auto_append_media_tags

        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "vision",
                        "function": {
                            "name": "vision_analyze",
                            "arguments": '{"image_url": "/tmp/user-image.png", "question": "Describe this"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "vision",
                "content": '{"success": true, "analysis": "a user image"}',
            },
        ]

        tags, _ = _collect_auto_append_media_tags(messages, history_offset=0)

        assert tags == []

    def test_gateway_auto_attaches_direct_browser_screenshot(self):
        """First-party browser captures are evidence without a QA manifest."""
        from gateway.run import _collect_auto_append_media_tags

        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "browser", "function": {"name": "browser_vision"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "browser",
                "content": '{"success": true, "screenshot_path": "/tmp/browser/shot.png"}',
            },
        ]

        tags, _ = _collect_auto_append_media_tags(messages, history_offset=0)

        assert tags == ["MEDIA:/tmp/browser/shot.png"]

    def test_gateway_auto_attaches_bounded_visual_qa_artifacts_with_dedupe_and_cap(self):
        """Visual-QA screenshots are first-party evidence even on uncertain receipts."""
        from gateway.run import _collect_auto_append_media_tags

        artifacts = [
            {"kind": "focused", "screenshot_path": "/tmp/qa/focused.png"},
            {"kind": "context", "screenshot_path": "/tmp/qa/context.png"},
            {"kind": "context", "screenshot_path": "/tmp/qa/focused.png"},
            {"kind": "responsive", "screenshot_path": "/tmp/qa/mobile.png"},
            {"kind": "responsive", "screenshot_path": "/tmp/qa/tablet.png"},
            {"kind": "responsive", "screenshot_path": "/tmp/qa/overflow.png"},
        ]
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "qa", "function": {"name": "visual_qa"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "qa",
                "content": json.dumps(
                    {
                        "status": "uncertain",
                        "visual_qa_receipt": {"status": "uncertain"},
                        "screenshot_artifacts": artifacts,
                    }
                ),
            },
        ]

        tags, voice = _collect_auto_append_media_tags(messages, history_offset=0)

        assert tags == [
            "MEDIA:/tmp/qa/focused.png",
            "MEDIA:/tmp/qa/context.png",
            "MEDIA:/tmp/qa/mobile.png",
            "MEDIA:/tmp/qa/tablet.png",
        ]
        assert voice is False

    def test_gateway_dedupes_visual_qa_artifacts_against_history(self):
        from gateway.run import _collect_auto_append_media_tags

        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "qa", "function": {"name": "visual_qa"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "qa",
                "content": json.dumps(
                    {
                        "status": "passed",
                        "screenshot_artifacts": [
                            {
                                "kind": "focused",
                                "screenshot_path": "/tmp/qa/focused.png",
                            },
                            {
                                "kind": "context",
                                "screenshot_path": "/tmp/qa/context.png",
                            },
                        ],
                    }
                ),
            },
        ]

        tags, _ = _collect_auto_append_media_tags(
            messages,
            history_offset=0,
            history_media_paths={"/tmp/qa/focused.png"},
        )

        assert tags == ["MEDIA:/tmp/qa/context.png"]

    def test_gateway_only_attaches_latest_visual_qa_retry_artifacts(self):
        from gateway.run import _collect_auto_append_media_tags

        messages = []
        attempts = [
            ("qa1", ["/tmp/qa/old-focused.png", "/tmp/qa/old-context.png"]),
            ("qa2", ["/tmp/qa/retry-focused.png", "/tmp/qa/retry-context.png"]),
            ("qa3", ["/tmp/qa/latest-focused.png"]),
        ]
        for call_id, paths in attempts:
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [{"id": call_id, "function": {"name": "visual_qa"}}],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(
                            {
                                "status": "uncertain",
                                "screenshot_artifacts": [
                                    {"kind": "focused" if index == 0 else "context", "screenshot_path": path}
                                    for index, path in enumerate(paths)
                                ],
                            }
                        ),
                    },
                ]
            )

        tags, _ = _collect_auto_append_media_tags(messages, history_offset=0)

        assert tags == ["MEDIA:/tmp/qa/latest-focused.png"]

    @pytest.mark.asyncio
    async def test_visual_qa_artifacts_batch_through_existing_media_delivery(
        self,
        tmp_path,
        monkeypatch,
    ):
        from gateway.config import Platform
        from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
        from gateway.run import GatewayRunner, _append_auto_media_tags
        from gateway.session import SessionSource

        root = tmp_path / "screenshots"
        root.mkdir()
        focused = root / "focused.png"
        context = root / "context.png"
        focused.write_bytes(b"focused")
        context.write_bytes(b"context")
        monkeypatch.setattr(
            "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
            (root,),
        )
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "qa", "function": {"name": "visual_qa"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "qa",
                "content": json.dumps(
                    {
                        "status": "passed",
                        "screenshot_artifacts": [
                            {"kind": "focused", "screenshot_path": str(focused)},
                            {"kind": "context", "screenshot_path": str(context)},
                        ],
                    }
                ),
            },
        ]
        response = _append_auto_media_tags(
            "Completed.\n\n"
            "What changed:\n- Implemented the requested dashboard update.\n\n"
            "Verification:\n- Focused checks passed.\n- Visual QA passed.\n\n"
            "Shipped:\n- Deployed to the requested environment.",
            messages,
            history_offset=0,
        )
        assert response.startswith(
            "Completed.\n\nWhat changed:\n- Implemented the requested dashboard update."
        )
        assert "Verification:\n- Focused checks passed.\n- Visual QA passed." in response
        assert "Shipped:\n- Deployed to the requested environment." in response
        assert response.count("MEDIA:") == 2
        event = MessageEvent(
            text="implement this",
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=Platform.DISCORD,
                chat_id="thread-1",
                chat_type="channel",
                thread_id="thread-1",
            ),
            message_id="message-1",
        )
        adapter = SimpleNamespace(
            name="discord",
            extract_media=BasePlatformAdapter.extract_media,
            extract_images=BasePlatformAdapter.extract_images,
            extract_local_files=BasePlatformAdapter.extract_local_files,
            send_multiple_images=AsyncMock(),
            send_voice=AsyncMock(),
            send_document=AsyncMock(),
            send_video=AsyncMock(),
        )
        runner = SimpleNamespace(
            _thread_metadata_for_source=lambda _source, _anchor=None: {
                "thread_id": "thread-1"
            },
            _reply_anchor_for_event=lambda _event: None,
        )

        await GatewayRunner._deliver_media_from_response(
            runner,
            response,
            event,
            adapter,
        )

        adapter.send_multiple_images.assert_awaited_once()
        assert [
            image[0] for image in adapter.send_multiple_images.await_args.kwargs["images"]
        ] == [f"file://{focused}", f"file://{context}"]

    @pytest.mark.asyncio
    async def test_post_stream_delivery_preserves_extracted_image_urls(self):
        from gateway.config import Platform
        from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
        from gateway.run import GatewayRunner
        from gateway.session import SessionSource

        event = MessageEvent(
            text="show the chart",
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=Platform.DISCORD,
                chat_id="thread-1",
                chat_type="channel",
                thread_id="thread-1",
            ),
        )
        adapter = SimpleNamespace(
            name="discord",
            extract_media=BasePlatformAdapter.extract_media,
            extract_images=BasePlatformAdapter.extract_images,
            extract_local_files=BasePlatformAdapter.extract_local_files,
            send_multiple_images=AsyncMock(),
            send_voice=AsyncMock(),
            send_document=AsyncMock(),
            send_video=AsyncMock(),
        )
        runner = SimpleNamespace(
            _thread_metadata_for_source=lambda _source, _anchor=None: {
                "thread_id": "thread-1"
            },
            _reply_anchor_for_event=lambda _event: None,
        )

        await GatewayRunner._deliver_media_from_response(
            runner,
            "Chart: ![confidence](https://example.com/confidence.png)",
            event,
            adapter,
        )

        adapter.send_multiple_images.assert_awaited_once_with(
            chat_id="thread-1",
            images=[("https://example.com/confidence.png", "confidence")],
            metadata={"thread_id": "thread-1"},
        )

    def test_gateway_appends_missing_screenshot_when_response_has_media(self):
        """One explicit attachment must not suppress other QA screenshots."""
        from gateway.run import _append_auto_media_tags

        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "manifest", "function": {"name": "read_file"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "manifest",
                "content": (
                    '"screenshot": "/tmp/qa/desktop.png", '
                    '"panelScreenshot": "/tmp/qa/mobile.png"'
                ),
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "desktop",
                        "function": {
                            "name": "vision_analyze",
                            "arguments": '{"image_url": "/tmp/qa/desktop.png", "question": "QA"}',
                        },
                    },
                    {
                        "id": "mobile",
                        "function": {
                            "name": "vision_analyze",
                            "arguments": '{"image_url": "/tmp/qa/mobile.png", "question": "QA"}',
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "desktop", "content": '{"success": true}'},
            {"role": "tool", "tool_call_id": "mobile", "content": '{"success": true}'},
        ]

        response = _append_auto_media_tags(
            "Done.\nMEDIA:/tmp/qa/desktop.png",
            messages,
            history_offset=0,
        )

        assert response.count("MEDIA:/tmp/qa/desktop.png") == 1
        assert response.endswith("MEDIA:/tmp/qa/mobile.png")

    def test_gateway_does_not_reattach_screenshot_after_compression(self):
        """Delivered QA evidence must not recur when history is rescanned."""
        from gateway.run import (
            _collect_auto_append_media_tags,
            _collect_history_media_paths,
        )

        history = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "manifest", "function": {"name": "read_file"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "manifest",
                "content": '"screenshot_path": "/tmp/qa/desktop.png"',
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "vision",
                        "function": {
                            "name": "vision_analyze",
                            "arguments": '{"image_url": "/tmp/qa/desktop.png", "question": "QA"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "vision", "content": '{"success": true}'},
        ]

        history_paths = _collect_history_media_paths(history)
        tags, _ = _collect_auto_append_media_tags(
            history,
            history_offset=9999,
            history_media_paths=history_paths,
        )

        assert history_paths == {"/tmp/qa/desktop.png"}
        assert tags == []

    def test_collect_history_media_paths_includes_image_generate_json(self):
        """Regression for #46627: the history media-path collector must pick up
        image_generate JSON-payload paths (no MEDIA: tag), not just MEDIA:
        text tags. Otherwise, after a compression boundary the auto-append
        fallback rescans full history, finds the generated path absent from
        the dedup set, and re-emits the same MEDIA tag every turn.
        """
        from gateway.run import _collect_history_media_paths

        history = [
            {"role": "user", "content": "make a cat"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "c", "function": {"name": "image_generate"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c",
                "content": '{"success": true, "image": "/tmp/gen/cat.png"}',
            },
            # A separate MEDIA: text tag from another tool, to confirm both shapes.
            {
                "role": "tool",
                "tool_call_id": "d",
                "content": "Saved MEDIA:/tmp/voice/note.ogg done",
            },
        ]
        paths = _collect_history_media_paths(history)
        assert "/tmp/gen/cat.png" in paths  # JSON-payload path (the bug)
        assert "/tmp/voice/note.ogg" in paths  # MEDIA: text path (already worked)

    def test_image_generate_not_reemitted_after_compression(self):
        """End-to-end of the #46627 fix: collect history paths, then the
        compression-fallback rescan (history_offset stale) must dedup the
        generated image against them — no re-emission."""
        from gateway.run import (
            _collect_auto_append_media_tags,
            _collect_history_media_paths,
        )

        history = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "c", "function": {"name": "image_generate"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c",
                "content": '{"success": true, "image": "/tmp/gen/dog.png"}',
            },
        ]
        history_paths = _collect_history_media_paths(history)

        # Simulate the post-compression fallback: history_offset is stale
        # (larger than the shrunken message list), so the collector rescans
        # the full list. With the dedup set populated, the already-delivered
        # image must NOT be re-emitted.
        tags, _ = _collect_auto_append_media_tags(
            history, history_offset=9999, history_media_paths=history_paths
        )
        assert tags == [], f"generated image re-emitted after compression: {tags}"


    def test_media_tags_not_extracted_from_history(self):
        """MEDIA tags from previous turns should NOT be extracted again."""
        # Simulate conversation history with a TTS call from a previous turn
        history = [
            {"role": "user", "content": "Say hello as audio"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "text_to_speech"}}]},
            {"role": "tool", "tool_call_id": "1", "content": '{"success": true, "media_tag": "[[audio_as_voice]]\\nMEDIA:/path/to/audio1.ogg"}'},
            {"role": "assistant", "content": "I've said hello for you!"},
        ]
        
        # New turn: user asks a simple question
        new_messages = [
            {"role": "user", "content": "What time is it?"},
            {"role": "assistant", "content": "It's 3:30 AM."},
        ]
        
        all_messages = history + new_messages
        history_len = len(history)
        
        # Fixed behavior: should extract NO media tags (none in new messages)
        tags, voice_directive = extract_media_tags_fixed(all_messages, history_len)
        assert tags == [], "Fixed extraction should not find tags in history"
        assert voice_directive is False
        
        # Broken behavior: would incorrectly extract the old media tag
        broken_tags, broken_voice = extract_media_tags_broken(all_messages)
        assert len(broken_tags) == 1, "Broken extraction finds tags in history"
        assert "audio1.ogg" in broken_tags[0]
    
    def test_media_tags_extracted_from_current_turn(self):
        """MEDIA tags from the current turn SHOULD be extracted."""
        # History without TTS
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        
        # New turn with TTS call
        new_messages = [
            {"role": "user", "content": "Say goodbye as audio"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2", "function": {"name": "text_to_speech"}}]},
            {"role": "tool", "tool_call_id": "2", "content": '{"success": true, "media_tag": "[[audio_as_voice]]\\nMEDIA:/path/to/audio2.ogg"}'},
            {"role": "assistant", "content": "I've said goodbye!"},
        ]
        
        all_messages = history + new_messages
        history_len = len(history)
        
        # Fixed behavior: should extract the new media tag
        tags, voice_directive = extract_media_tags_fixed(all_messages, history_len)
        assert len(tags) == 1, "Should extract media tag from current turn"
        assert "audio2.ogg" in tags[0]
        assert voice_directive is True
    
    def test_multiple_tts_calls_in_history_not_accumulated(self):
        """Multiple TTS calls in history should NOT accumulate in new responses."""
        # History with multiple TTS calls
        history = [
            {"role": "user", "content": "Say hello"},
            {"role": "tool", "tool_call_id": "1", "content": 'MEDIA:/audio/hello.ogg'},
            {"role": "assistant", "content": "Done!"},
            {"role": "user", "content": "Say goodbye"},
            {"role": "tool", "tool_call_id": "2", "content": 'MEDIA:/audio/goodbye.ogg'},
            {"role": "assistant", "content": "Done!"},
            {"role": "user", "content": "Say thanks"},
            {"role": "tool", "tool_call_id": "3", "content": 'MEDIA:/audio/thanks.ogg'},
            {"role": "assistant", "content": "Done!"},
        ]
        
        # New turn: no TTS
        new_messages = [
            {"role": "user", "content": "What time is it?"},
            {"role": "assistant", "content": "3 PM"},
        ]
        
        all_messages = history + new_messages
        history_len = len(history)
        
        # Fixed: no tags
        tags, _ = extract_media_tags_fixed(all_messages, history_len)
        assert tags == [], "Should not accumulate tags from history"
        
        # Broken: would have 3 tags (all the old ones)
        broken_tags, _ = extract_media_tags_broken(all_messages)
        assert len(broken_tags) == 3, "Broken version accumulates all history tags"
    
    def test_deduplication_within_current_turn(self):
        """Multiple MEDIA tags in current turn should be deduplicated."""
        history = []
        
        # Current turn with multiple tool calls producing same media
        new_messages = [
            {"role": "user", "content": "Multiple TTS"},
            {"role": "tool", "tool_call_id": "1", "content": 'MEDIA:/audio/same.ogg'},
            {"role": "tool", "tool_call_id": "2", "content": 'MEDIA:/audio/same.ogg'},  # duplicate
            {"role": "tool", "tool_call_id": "3", "content": 'MEDIA:/audio/different.ogg'},
            {"role": "assistant", "content": "Done!"},
        ]
        
        all_messages = history + new_messages
        
        tags, _ = extract_media_tags_fixed(all_messages, 0)
        # Even though same.ogg appears twice, deduplication happens after extraction
        # The extraction itself should get both, then caller deduplicates
        assert len(tags) == 3  # Raw extraction gets all
        
        # Deduplication as done in the actual code:
        seen = set()
        unique = [t for t in tags if t not in seen and not seen.add(t)]
        assert len(unique) == 2  # After dedup: same.ogg and different.ogg


class TestStaleToolMediaLeak:
    """Regression tests for #34608.

    A MEDIA: path emitted by an execute_code / make_image tool several turns
    earlier remains in the full conversation message list. A later text-only
    reply (zero MEDIA directives) must NOT attach that stale image.

    The production code previously relied solely on path-based dedup against
    paths reconstructed from the replayable transcript. When that
    reconstruction does not byte-match the in-memory tool content (timestamp
    stripping, observed-context withholding, compression rewrites), the stale
    path is absent from the dedup set and leaks. Turn-scoped slicing closes
    this class of bug deterministically.
    """

    def test_stale_execute_code_media_not_attached_to_text_only_reply(self):
        """The exact #34608 scenario: make_image cover from an earlier turn."""
        # Prior turn generated an image via execute_code stdout.
        history = [
            {"role": "user", "content": "Make a cover image"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "1", "function": {"name": "execute_code"}}]},
            {"role": "tool", "tool_call_id": "1",
             "content": "Generating cover...\nMEDIA:/tmp/seosmi_cover.png\nDone."},
            {"role": "assistant", "content": "Here is your cover."},
        ]
        # Current turn: plain text status update, zero MEDIA directives.
        new_messages = [
            {"role": "user", "content": "What skill version am I on?"},
            {"role": "assistant", "content": "You're on v0.15.1."},
        ]
        all_messages = history + new_messages
        history_len = len(history)

        # Simulate the dedup set FAILING to capture the stale path (the real
        # #34608 condition: replayable-history reconstruction diverged from
        # the in-memory tool content, so the path is not in the set).
        history_media_paths = set()

        tags, voice = extract_media_tags_production(
            all_messages, history_len, history_media_paths
        )
        assert tags == [], (
            "Stale tool MEDIA from a prior turn must not leak onto a "
            f"later text-only reply, got {tags}"
        )
        assert voice is False

        # The pre-fix production behaviour (scan everything, dedup only) would
        # have leaked the stale path when the dedup set missed it.
        broken_tags, _ = extract_media_tags_broken(all_messages)
        assert any("seosmi_cover.png" in t for t in broken_tags), (
            "Sanity: the unscoped scan does surface the stale path"
        )

    def test_current_turn_media_still_attached_when_dedup_set_empty(self):
        """Turn-scoping must not suppress genuinely new media."""
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        new_messages = [
            {"role": "user", "content": "Make me a cover image"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "9", "function": {"name": "execute_code"}}]},
            {"role": "tool", "tool_call_id": "9",
             "content": "MEDIA:/tmp/fresh_cover.png"},
            {"role": "assistant", "content": "Here it is."},
        ]
        all_messages = history + new_messages
        tags, _ = extract_media_tags_production(
            all_messages, len(history), set()
        )
        assert len(tags) == 1 and "fresh_cover.png" in tags[0]

    def test_compression_shrink_falls_back_to_path_dedup(self):
        """When the list is shorter than history_len (mid-run compression),
        fall back to scanning everything with path-based dedup so the #160
        compression-safe guarantee is preserved."""
        # Post-compression list is shorter than the original history length.
        compressed_messages = [
            {"role": "user", "content": "summary so far..."},
            {"role": "tool", "tool_call_id": "7",
             "content": "MEDIA:/tmp/old_from_history.png"},
            {"role": "assistant", "content": "ok"},
        ]
        original_history_len = 12  # larger than the compressed list
        # The old path IS captured in the dedup set here (history scan ran
        # before compression), so it must still be excluded.
        history_media_paths = {"/tmp/old_from_history.png"}
        tags, _ = extract_media_tags_production(
            compressed_messages, original_history_len, history_media_paths
        )
        assert tags == [], (
            "On the compression fallback path, path-dedup must still exclude "
            f"known-old media, got {tags}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
