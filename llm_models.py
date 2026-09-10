"""Which OpenAI model each call site uses.

One model for every call was the wrong default. The five call sites have very
different economics and very different blast radii, and picking per site is
worth the three names:

- POLLER runs on every single email, meeting and file the pollers see, and only
  ever answers "is there a todo here, and why". Volume is the cost driver and
  the judgement is easy, so this wants the cheapest current model.

- ACTIONS writes the instruction that becomes an agent's marching orders. It is
  one cached call per todo, so it is nearly free however good the model is —
  and its output is the highest-leverage text in the system. An instruction
  that quietly asserts something about the user ("reflects a positive
  experience") reaches the executor as the user's own words and gets acted on;
  see SUGGESTED_ROUTE_TAIL in agent/input_builder.py for why the downstream
  defence against that is thin. Pay for judgement here.

- AGENT is the Agents-SDK resolution loop: multi-turn, tool-calling, and the
  only executor that runs where there is no local Hermes binary, which means it
  is what the deployed container actually uses. Its failures send real mail and
  submit real forms. It is also the one place where token spend can run away,
  since every tool result re-enters the context — so this is the knob to turn
  down first if the bill surprises you.

Read at import time, so `load_dotenv` must already have run — it does, at the
top of both entrypoints. Override any of them in `.env` to trade cost against
judgement without touching code.
"""

import os

POLLER = os.environ.get("OPENAI_MODEL_POLLER", "gpt-5.6-luna")
ACTIONS = os.environ.get("OPENAI_MODEL_ACTIONS", "gpt-5.6-terra")
AGENT = os.environ.get("OPENAI_MODEL_AGENT", "gpt-6-astra")
