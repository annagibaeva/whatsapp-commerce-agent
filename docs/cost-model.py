"""Modelled cost per booking. Published rates, estimated token counts."""
HAIKU = {"in": 1.00, "out": 5.00}      # $/MTok, claude-haiku-4-5
OPUS  = {"in": 5.00, "out": 25.00}     # $/MTok, claude-opus-5

def usd(p, tin, tout): return (tin*p["in"] + tout*p["out"]) / 1_000_000

EXTRACT_PROMPT = 652      # measured: prompts/extract-v0.1.md
AGENT_PROMPT   = 1213     # measured: prompts/agent-v0.1.md
TOOL_DEFS      = 400      # 4 tool schemas
HIST_PER_MSG   = 35       # avg tokens per prior turn carried
TOOL_RESULT    = 180      # a check_availability / search_catalogue result

def turn_cost(prior_msgs, agent_iters, escalate_extraction=False):
    hist = prior_msgs * HIST_PER_MSG
    # extraction: one call, small JSON out
    e_in, e_out = EXTRACT_PROMPT + hist + 35, 80
    c = usd(HAIKU, e_in, e_out)
    if escalate_extraction:                       # retry on opus
        c += usd(OPUS, e_in, e_out)
    # agent: one call per tool-loop iteration, context grows each time
    for i in range(agent_iters):
        a_in = AGENT_PROMPT + TOOL_DEFS + hist + i*(TOOL_RESULT + 90)
        c += usd(HAIKU, a_in, 130)
    return c

SCENARIOS = [
    ("Fast  — knows what they want, books in 2 turns", [(0,2),(2,3)], False),
    ("Typical — 5 turns, a question or two, books",   [(0,2),(2,2),(4,3),(6,2),(8,3)], False),
    ("Hard  — 8 turns, one garbled message, escalates", [(0,2),(2,2),(4,3),(6,2),(8,3),(10,2),(12,2),(14,1)], True),
]

print(f"{'scenario':50} {'turns':>6} {'model calls':>12} {'$/booking':>12}")
print("-"*84)
rows=[]
for name, turns, esc in SCENARIOS:
    total = 0.0; calls = 0
    for idx,(prior, iters) in enumerate(turns):
        first_esc = esc and idx == len(turns)//2
        total += turn_cost(prior, iters, first_esc)
        calls += 1 + iters + (1 if first_esc else 0)
    rows.append((name, len(turns), calls, total))
    print(f"{name:50} {len(turns):>6} {calls:>12} {'$'+format(total,'.4f'):>12}")

print()
print(f"{'monthly bookings':>18} {'fast':>12} {'typical':>12} {'hard':>12}")
print("-"*58)
for v in (100, 1_000, 10_000, 100_000):
    line = f"{v:>18,}"
    for _,_,_,c in rows:
        line += f" {'$'+format(c*v,',.2f'):>12}"
    print(line)
