"""The tool loop: hand the model four tools and run one conversation turn.

The model reads the conversation and can call `search_catalogue`,
`check_availability`, `request_booking`, or `escalate`. It cannot commit
a booking by returning text, and `request_booking` itself only commits a
slot after `wca.gate.evaluate` passes a proposal built from the
conversation's own facts (see `wca.tools`). This module only wires the
model to those tools and runs the request/execute/respond loop; it makes
no policy decision of its own.

Never send `temperature`, `top_p` or `top_k`. All three were removed on
the current model family and a request that sends one gets a 400 back.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from wca.extract.base import load_prompt
from wca.tools import TOOL_SPECS, ToolContext, dispatch

#: Small and cheap: the model here only orchestrates tool calls, it never
#: decides policy, so there is nothing that needs a bigger model.
MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT_VERSION = "agent-v0.1"

#: Rendered into the system prompt's `{now}` placeholder so the model
#: knows the date without ever calling `datetime.now()` itself -- nothing
#: in this codebase does that, `now` is always the value the caller
#: passed in (see `wca.clock`). Includes the weekday because the model
#: reasons about "is this Sunday" in its replies and getting that wrong
#: is a worse failure than getting the calendar date wrong.
NOW_FORMAT = "%A, %d %B %Y, %H:%M UTC"

#: How many request/execute/respond rounds one turn is allowed. A
#: well-behaved conversation finishes in one or two: look something up,
#: propose a booking, answer. If the cap is hit while the model still
#: wants to call a tool, the loop stops and returns whatever text the
#: last response carried -- often none. That is an availability trade,
#: not a safety one: nothing in this loop commits a booking outside
#: `request_booking`'s own gate check, so a truncated loop can leave a
#: conversation without an answer, but it cannot leave behind a bad one.
MAX_ITERATIONS = 8


class _Messages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class _Client(Protocol):
    messages: _Messages


class Agent:
    """One conversation's worth of model plus tools.

    `client` is anything shaped like `anthropic.Anthropic()` -- in
    particular a stub with a `.messages.create(**kwargs)` method works
    for tests, no network and no real SDK object required.
    """

    def __init__(self, client: _Client, tool_context: ToolContext, model: str = MODEL) -> None:
        self._client = client
        self._tools = tool_context
        self.model = model
        # Formatted once, from the `now` this turn's ToolContext already
        # carries -- never `datetime.now()`. A fresh Agent is built every
        # turn (see wca.cli.run_job), so this is naturally turn-fresh too.
        self.system = load_prompt(SYSTEM_PROMPT_VERSION).format(
            now=tool_context.now.strftime(NOW_FORMAT)
        )

    def _call(self, history: list[dict[str, Any]]) -> Any:
        return self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=self.system,
            tools=list(TOOL_SPECS),
            messages=history,
        )

    def run_turn(self, messages: list[dict[str, Any]]) -> str:
        """Run one turn to completion and return the model's final text.

        Sends `messages` plus the tool definitions, executes any tool
        calls the model makes, feeds the results back as a single user
        message (per-call, not split across messages), and repeats until
        the model stops calling tools or `MAX_ITERATIONS` is reached.
        """
        history = list(messages)
        response = self._call(history)

        iterations = 0
        while response.stop_reason == "tool_use" and iterations < MAX_ITERATIONS:
            iterations += 1
            history.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                result = dispatch(self._tools, block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })
            history.append({"role": "user", "content": tool_results})

            response = self._call(history)

        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
