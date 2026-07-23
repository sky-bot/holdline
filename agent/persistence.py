"""Postgres persistence for call state, transitions, and recovery (TRD §5, §8).

Every FSM transition is written **synchronously** — the `call_state` upsert and the
`state_transitions` insert happen in one transaction that the agent awaits before
doing anything else. So a crash can never leave persisted state behind the actual
conversation, which is what makes exact recovery possible (TRD §8.1). Recovery
reads the latest non-terminal `call_state` for a room and resumes the FSM there.

A `generation` fencing token guards writes: on attaching to a call a process bumps
it and stamps every write with its value. A write that updates zero rows means a
newer process has taken over — this one has been fenced out and must stop writing
(and speaking) (TRD §8.2).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg

from agent.models import Branch, CallState, State

_TERMINAL = ("PAYMENT_SCHEDULED", "ESCALATED", "CALL_ENDED")


async def _init_conn(conn: asyncpg.Connection) -> None:
    # Map jsonb <-> Python dict transparently, so offer/notes are plain dicts.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


@dataclass
class ActiveCall:
    call_id: Any
    call_state: CallState
    generation: int


class Persistence:
    """Thin async data-access layer over the `calls` / `call_state` / `state_transitions` tables."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Persistence":
        # statement_cache_size=0: managed Postgres poolers (Neon/Supabase PgBouncer
        # in transaction mode) reuse connections and drop server-side prepared
        # statements, which makes asyncpg's cache misfire. Disabling it is the
        # supported way to run asyncpg through those poolers.
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, init=_init_conn, statement_cache_size=0
        )
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    @staticmethod
    def _row_to_state(row: asyncpg.Record) -> CallState:
        return CallState(
            current_state=State(row["current_state"]),
            branch=Branch(row["branch"]) if row["branch"] else None,
            offer=row["offer"],
            notes=row["notes"],
            turn_count=row["turn_count"],
        )

    async def find_active_call(self, room_name: str) -> Optional[ActiveCall]:
        """The most recent non-terminal call for a room, or None (TRD §8.2 step 3)."""
        row = await self._pool.fetchrow(
            """
            select c.id, s.current_state, s.branch, s.offer, s.notes, s.turn_count, s.generation
            from calls c
            join call_state s on s.call_id = c.id
            where c.room_name = $1 and s.current_state <> all($2::text[])
            order by c.started_at desc
            limit 1
            """,
            room_name, list(_TERMINAL),
        )
        if row is None:
            return None
        return ActiveCall(row["id"], self._row_to_state(row), row["generation"])

    async def create_call(self, room_name: str) -> ActiveCall:
        """Create a fresh call and its initial OPENING state (generation 1)."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                call_id = await con.fetchval(
                    "insert into calls (room_name) values ($1) returning id", room_name
                )
                await con.execute(
                    "insert into call_state (call_id, current_state, generation) "
                    "values ($1, 'OPENING', 1)",
                    call_id,
                )
        return ActiveCall(call_id, CallState(), 1)

    async def bump_generation(self, call_id: Any) -> int:
        """Fence: claim this call for the current process; returns the new token (TRD §8.2 step 4)."""
        return await self._pool.fetchval(
            "update call_state set generation = generation + 1, updated_at = now() "
            "where call_id = $1 returning generation",
            call_id,
        )

    async def persist_transition(self, call_id: Any, generation: int, transition: Any) -> bool:
        """Synchronously write the new state and append the transition, fenced by generation.

        Returns False if a newer process has taken over (generation moved on); the
        caller must then stop writing and speaking (TRD §8.2).
        """
        cs: CallState = transition.call_state
        async with self._pool.acquire() as con:
            async with con.transaction():
                claimed = await con.fetchval(
                    """
                    update call_state
                    set current_state = $2, branch = $3, offer = $4, notes = $5,
                        turn_count = $6, updated_at = now()
                    where call_id = $1 and generation = $7
                    returning call_id
                    """,
                    call_id,
                    cs.current_state.value,
                    cs.branch.value if cs.branch else None,
                    cs.offer,
                    cs.notes,
                    cs.turn_count,
                    generation,
                )
                if claimed is None:
                    return False  # fenced out
                await con.execute(
                    "insert into state_transitions (call_id, from_state, to_state, event) "
                    "values ($1, $2, $3, $4)",
                    call_id,
                    transition.from_state.value,
                    transition.to_state.value,
                    transition.event,
                )
        return True

    async def log_resume(self, call_id: Any, state: State) -> None:
        """Audit marker that a process resumed this call at `state` (TRD §8.2 step 5)."""
        await self._pool.execute(
            "insert into state_transitions (call_id, from_state, to_state, event) "
            "values ($1, $2, $2, 'resumed')",
            call_id, state.value,
        )

    async def set_outcome(self, call_id: Any, outcome: str) -> None:
        """Record the terminal outcome on the call ('payment_scheduled' | 'escalated')."""
        await self._pool.execute(
            "update calls set outcome = $2, ended_at = now() where id = $1", call_id, outcome
        )
