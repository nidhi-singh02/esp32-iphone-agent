import io
import math

import pytest
from PIL import Image

from hidagent.agent import circle, parse_actions, parse_command, parse_plan


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (498, 1080), "white").save(out, "JPEG")
    return out.getvalue()


def test_parse_single_action():
    acts = parse_actions("THINK: The Search pill is visible, so I tap it.\nDO: tap 500 814")
    assert acts == [{"thought": "The Search pill is visible, so I tap it.", "thought_zh": "",
                     "action": "tap", "x": 500, "y": 814}]


def test_parse_several_actions_keeps_thought_on_first():
    acts = parse_actions("THINK: Type and search.\nDO: type Whiteboard\nDO: key enter")
    assert [a["action"] for a in acts] == ["type", "key"]
    assert acts[0]["text"] == "Whiteboard" and acts[1]["combo"] == "enter"
    assert "thought" not in acts[1]


def test_parse_caps_at_six_actions():
    reply = "THINK: draw\n" + "\n".join(f"DO: tap {i} {i}" for i in range(10))
    assert len(parse_actions(reply)) == 6


def test_parse_path_and_circle():
    assert parse_command("path 100 200 300 400 500 600")["points"] == [[100, 200], [300, 400], [500, 600]]
    assert parse_command("circle 500 450 200") == {"action": "circle", "x": 500, "y": 450, "r": 200}


@pytest.mark.parametrize("line", ["fly 1 2", "tap 500", "path 1 2 3"])
def test_parse_rejects_bad_commands(line):
    with pytest.raises(ValueError):
        parse_command(line)


def test_parse_requires_a_do_line():
    with pytest.raises(ValueError):
        parse_actions("THINK: I am not sure what to do.")


def test_parse_plan_with_and_without_languages():
    assert parse_plan("EN:\n1. Tap Search\nZH:\n1. 点搜索") == ("1. Tap Search", "1. 点搜索")
    assert parse_plan("1. Tap Search") == ("1. Tap Search", "1. Tap Search")


def test_circle_is_closed_and_round_on_a_tall_screen():
    aspect = 0.46  # width / height of a portrait iPhone frame
    pts = circle(0.5, 0.5, 0.2, aspect)
    assert pts[0] == pytest.approx(pts[-1])
    for x, y in pts:  # equal radius in pixels: y offsets shrink by the aspect ratio
        assert math.hypot(x - 0.5, (y - 0.5) / aspect) == pytest.approx(0.2)


def test_actor_may_not_repeat_a_move_that_changed_nothing():
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        live, frame, aspect, size = True, _jpeg(), 0.46, (498, 1080)

    class Actor:
        model = "fake"
        def __init__(self):
            self.prompts = []
        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            self.prompts.append(text)
            return "THINK: again\nDO: swipe 382 253 382 150" if len(self.prompts) == 1 else "THINK: other\nDO: tap 1 2"

    actor = Actor()
    agent = Agent(phone=None, screen=Screen(), actor=actor, planner=actor, trace=Trace(None))
    agent.steps = [{"action": "swipe", "x1": 382, "y1": 253, "x2": 382, "y2": 150, "changed": False}]
    acts = asyncio.run(agent._next_actions())
    assert acts[0]["action"] == "tap"
    assert "changed nothing" in actor.prompts[1]
    assert "1000x1000 square" in actor.prompts[0]  # the actor sees a square, so pixel answers are grid answers


def test_repeating_a_move_that_keeps_changing_the_screen_is_not_stuck():
    from hidagent.agent import Agent
    from hidagent.debug import Trace

    agent = Agent(phone=None, screen=None, actor=None, planner=None, trace=Trace(None))
    swipe = {"action": "swipe", "x1": 500, "y1": 800, "x2": 500, "y2": 400}
    agent.steps = [{**swipe, "changed": True}, {**swipe, "changed": True}]
    assert not agent._repeating(dict(swipe))
    agent.steps[-1]["changed"] = False  # reached the end of the list
    assert agent._repeating(dict(swipe))


def test_precise_tap_flag():
    assert parse_command("tap 120 640 precise") == {"action": "tap", "x": 120, "y": 640, "precise": True}
    assert "precise" not in parse_command("tap 120 640")


def test_alternating_tap_and_home_is_a_loop():
    from hidagent.agent import Agent
    from hidagent.debug import Trace

    agent = Agent(phone=None, screen=None, actor=None, planner=None, trace=Trace(None))
    tap = {"action": "tap", "x": 250, "y": 887}
    agent.steps = [{**tap, "changed": True}, {"action": "home", "changed": True}, {**tap, "changed": True},
                   {"action": "home", "changed": True}]
    assert agent._tried(dict(tap)) == 2  # a third try is refused
    assert agent._tried({"action": "home"}) == 0
    assert agent._tried({"action": "tap", "x": 500, "y": 820}) == 0


def test_taps_a_few_units_apart_count_as_the_same_tap():
    from hidagent.agent import Agent
    from hidagent.debug import Trace

    agent = Agent(phone=None, screen=None, actor=None, planner=None, trace=Trace(None))
    agent.steps = [{"action": "tap", "x": 502, "y": 821, "changed": True},
                   {"action": "tap", "x": 498, "y": 821, "changed": True, "precise": True}]
    assert agent._tried({"action": "tap", "x": 500, "y": 821}) == 2
    assert agent._tried({"action": "tap", "x": 500, "y": 880}) == 0  # a different target
    swipe = {"action": "swipe", "x1": 500, "y1": 800, "x2": 500, "y2": 400}
    agent.steps = [{**swipe, "x2": 505, "changed": True}]
    assert agent._tried(swipe) == 0  # swipes stay exact


def test_actor_prompt_asks_to_verify_before_done_and_not_to_duplicate():
    from hidagent.agent import ACTOR_PROMPT

    assert "Before done: open the place where the result should appear" in ACTOR_PROMPT
    assert "Do not create the same thing twice" in ACTOR_PROMPT


def test_after_an_action_the_agent_waits_for_a_fresh_frame_then_for_quiet():
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    calls = []

    class Screen:
        async def fresh(self, since, timeout=0.8):
            calls.append(("fresh", since, timeout))

        async def settled(self, quiet=0.35, least=0.25, most=4):
            calls.append(("settled", quiet))

    agent = Agent(phone=None, screen=Screen(), actor=None, planner=None, trace=Trace(None))
    asyncio.run(agent._screen_after(123.0))
    assert calls == [("fresh", 123.0, 1.2), ("settled", 0.5)]
    calls.clear()
    asyncio.run(agent._screen_after(None))  # nothing was done (wait): no point waiting for a new frame
    assert calls == [("settled", 0.35)]


def test_a_repeated_tap_is_aimed_even_when_the_screen_changed():
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    taps = []

    class Phone:
        async def tap(self, x, y, aim=False):
            taps.append(aim)

    agent = Agent(phone=Phone(), screen=None, actor=None, planner=None, trace=Trace(None))
    asyncio.run(agent._do({"action": "tap", "x": 500, "y": 821}))
    agent.steps = [{"action": "tap", "x": 502, "y": 821, "changed": True}]  # same spot, a widget faded in
    asyncio.run(agent._do({"action": "tap", "x": 500, "y": 821}))
    asyncio.run(agent._do({"action": "tap", "x": 500, "y": 300}))  # a new target is not aimed
    assert taps == [False, True, False]


def test_refused_repeats_become_a_stuck_error_not_an_empty_reply_error():
    import asyncio

    from hidagent.agent import Agent, StuckError
    from hidagent.debug import Trace

    class Screen:
        size = (498, 1080)
        live, frame = True, _jpeg()

    class Actor:
        model = "fake"

        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            return "THINK: again\nDO: tap 500 821"

    agent = Agent(phone=None, screen=Screen(), actor=Actor(), planner=None, trace=Trace(None))
    agent.steps = [{"action": "tap", "x": 502, "y": 821, "changed": True},
                   {"action": "home", "changed": True},
                   {"action": "tap", "x": 498, "y": 821, "changed": True}]
    with pytest.raises(StuckError, match="kept proposing"):
        asyncio.run(agent._next_actions())


def test_actor_prompt_calls_the_change_signal_a_hint():
    from hidagent.agent import ACTOR_PROMPT

    assert "only a hint" in ACTOR_PROMPT


def test_actor_prompt_matches_how_the_phone_behaves():
    from hidagent.agent import ACTOR_PROMPT

    assert "keyboard may stay hidden" in ACTOR_PROMPT
    assert "only scrolls it one row" in ACTOR_PROMPT
    assert "visibly focused (the keyboard is showing)" not in ACTOR_PROMPT


def test_an_empty_actor_reply_is_asked_again_without_using_up_an_attempt():
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        size = (498, 1080)
        live, frame = True, _jpeg()

    class Actor:
        model = "fake"

        def __init__(self):
            self.calls = []

        async def chat(self, system, text, image=None, max_tokens=2000, temperature=0):
            self.calls.append(temperature)
            return "" if len(self.calls) == 1 else "THINK: ok\nDO: wait"

    actor = Actor()
    agent = Agent(phone=None, screen=Screen(), actor=actor, planner=None, trace=Trace(None))
    assert asyncio.run(agent._next_actions())[0]["action"] == "wait"
    assert actor.calls == [0, 0]  # the second ask kept the same temperature: it was not a new attempt
