"""Bind the production ledger to explicit synthetic test dependencies."""
from pathlib import Path
import re

from hyadex.store.cross_chat import CrossChatBindings, CrossChatStore


def ledger_type(namespace):
    """Retain fixture-owned clocks/validators while using real ledger methods."""
    bindings = CrossChatBindings(
        now=lambda: namespace['now_iso'](),
        join_task=lambda task: namespace['join_task_despite_caller_cancellation'](task),
        route_actions=lambda value: namespace['canonical_provider_cross_chat_route_actions'](value),
        source_instruction=lambda value: namespace['validated_cross_chat_source_user_instruction'](value),
        instance_id=lambda: namespace.get('SERVER_INSTANCE_ID', 'fixture'),
        retention_seconds=lambda: namespace.get('PROVIDER_CROSS_CHAT_LEGACY_RATE_RETENTION_SECONDS', 3600),
        route_id_pattern=namespace.get('PROVIDER_CROSS_CHAT_ROUTE_ID_RE', re.compile(r'^route_[0-9a-f]{32}$')),
        pair_id_pattern=namespace.get('PROVIDER_CROSS_CHAT_ROUTE_PAIR_ID_RE', re.compile(r'^pair_[0-9a-f]{32}$')),
        effect_id_pattern=namespace.get('PROVIDER_CROSS_CHAT_RECIPROCAL_EFFECT_ID_RE', re.compile(r'^exchange_[0-9a-f]{32}$')),
        exchange_terminal_statuses=frozenset(namespace.get('CROSS_CHAT_EXCHANGE_TERMINAL_STATUSES',
                                                         {'completed', 'failed', 'cancelled', 'expired'})),
        leg_terminal_statuses=frozenset(namespace.get('CROSS_CHAT_EXCHANGE_LEG_TERMINAL_STATUSES',
                                                    {'delivered', 'failed', 'cancelled', 'expired'})),
    )

    class FixtureLedger(CrossChatStore):
        def __init__(self, path=Path(':memory:')):
            super().__init__(path, bindings)

    return FixtureLedger
