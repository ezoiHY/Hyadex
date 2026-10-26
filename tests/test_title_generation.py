"""Synthetic title requests: no provider, account, network, or live state."""
import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import title_generation as titles


def schema():
    return {'definitions': {
        'ThreadStartParams': {'properties': {
            'ephemeral': {'type': 'boolean'},
            **{k: {} for k in ('runtimeWorkspaceRoots', 'baseInstructions', 'developerInstructions',
                               'config', 'sandbox', 'approvalPolicy')},
        }},
        'TurnStartParams': {'properties': {'environments': {
            'type': ['array', 'null'], 'description': 'Empty disables environment access for this turn.',
        }}},
    }}


class FormattingTests(unittest.TestCase):
    def test_bounded_quoted_context(self):
        prompt = titles.title_prompt('u' * 10000, 'a' * 10000)
        data = json.loads(prompt.split('\n\n', 1)[1])
        self.assertEqual(len(data['user_message']), 1600)
        self.assertEqual(len(data['assistant_reply']), 800)
        self.assertIn('not instructions', prompt)

    def test_invalid_titles_are_not_truncated_into_bad_names(self):
        for value in (None, {}, '', 'a\nb', 'x' * 73, 'x\x00x', 'a\u202eb', '```title```', 'New chat'):
            with self.subTest(value=value):
                self.assertIsNone(titles.clean_title(value))
        self.assertEqual(titles.clean_title(' “Somi and Nami 的歌” '), 'Somi and Nami 的歌')

    def test_codex_schema_fails_closed(self):
        self.assertTrue(titles.supports_codex_titles(schema()))
        data = schema()
        data['definitions']['TurnStartParams']['properties']['environments']['description'] = 'Optional'
        self.assertFalse(titles.supports_codex_titles(data))
        self.assertFalse(titles.supports_codex_titles({}))








class CodexAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            request=AsyncMock(return_value={'config': {'mcp_servers': {'dangerous.server': {}}}}),
            start_thread=AsyncMock(return_value='ephemeral-title'),
            read_thread=AsyncMock(return_value={'ephemeral': True, 'path': None}),
            close=AsyncMock(),
        )
        self.turn = SimpleNamespace(close=AsyncMock(), next_notification=AsyncMock(side_effect=[
            {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'id': 'answer', 'text': 'Cat Song'}}},
            {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}},
        ]))
        self.client.start_turn = AsyncMock(return_value=self.turn)
        self.factory = self.enterContext(patch.object(titles, 'CodexAppServerClient', return_value=self.client))

        async def write_schema(command, **kwargs):
            target = Path(command[-1])
            target.mkdir()
            (target / 'codex_app_server_protocol.v2.schemas.json').write_text(json.dumps(schema()))
            return ''

        self.enterContext(patch.object(titles, 'run_isolated_command', side_effect=write_schema))

    async def generate(self):
        return await titles.generate_codex_title('only quoted context', executable='codex', model='selected-model',
                                                env={'HOME': '/auth-home', 'HYADEX_TOKEN': 'private'})

    async def test_ephemeral_new_thread_no_fork_no_environment(self):
        self.assertEqual(await self.generate(), 'Cat Song')
        options = self.factory.call_args.kwargs
        self.assertNotIn('HYADEX_TOKEN', options['env_factory']())
        params = self.client.start_thread.call_args.args[0]
        self.assertTrue(params['ephemeral'])
        self.assertEqual(params['model'], 'selected-model')
        self.assertEqual(params['runtimeWorkspaceRoots'], [])
        self.assertEqual(params['config']['mcp_servers'], {'dangerous.server': {'enabled': False}})
        self.assertFalse(params['config']['features.hooks'])
        self.assertEqual(self.client.start_turn.call_args.kwargs['overrides'], {'environments': []})
        self.client.close.assert_awaited_once()
        self.turn.close.assert_awaited_once()
        self.assertFalse(Path(params['cwd']).exists())

    async def test_unconfirmed_ephemeral_refuses_model_request(self):
        self.client.read_thread.return_value = {'ephemeral': False, 'path': '/real/history'}
        with self.assertRaises(titles.TitleGenerationError):
            await self.generate()
        self.client.start_turn.assert_not_awaited()
        self.client.close.assert_awaited_once()

    async def test_tool_notification_is_not_accepted_as_title(self):
        self.turn.next_notification.side_effect = [
            {'method': 'item/started', 'params': {'item': {'type': 'commandExecution'}}},
        ]
        with self.assertRaises(titles.TitleGenerationError):
            await self.generate()
        self.client.close.assert_awaited_once()

    async def test_cancel_reaps_owned_client(self):
        self.turn.next_notification.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.generate()
        self.client.close.assert_awaited_once()

    async def test_turn_close_failure_still_reaps_owned_client(self):
        self.turn.close.side_effect = RuntimeError('synthetic close failure')
        with self.assertRaises(RuntimeError):
            await self.generate()
        self.client.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
