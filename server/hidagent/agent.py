"""See-think-act loop: a planner model writes the plan, an actor model picks each step."""

import asyncio
import json
import logging
import math
import re
import time

from . import vision
from .debug import Trace
from .device import DeviceError
from .llm import LLM
from .phone import Phone
from .screen import Frames

log = logging.getLogger(__name__)

PLANNER_PROMPT = """You plan tasks on an iPhone. Given the goal and the current screen,
write a short numbered plan (at most 6 steps) of what to do on screen. Plain text only, in English.
Plan only what the goal needs, from what you can see; never invent accounts, emails, logins or
screens you have not seen. Later steps can stay general (e.g. "Draw the face on the canvas").
To open an app, use search, not the home screens: the action "search NAME" opens Spotlight and types the name,
then tap the app under "Top Hit"."""

ACTOR_PROMPT = """You operate an iPhone through a mouse pointer. You see the current screen.
Reply in this format, nothing else:
THINK: <plain English a non-technical user understands: first check your last action against the
       screen (did it do what you intended?), then what you do now and why>
DO: <action>

Give several DO lines (up to 6, run in order) whenever the next actions do not depend on seeing the
result: e.g. ticking several visible checkboxes or days, several drawing strokes, typing then pressing enter.
Give ONE DO line when the next action depends on what this one opens or changes.

Coordinates are integers 0-1000 relative to the screenshot (x left->right, y top->bottom).
Aim at the visual center of the element you want, not its edge or its label.
Look before you act: describe what is really on the screen now, not what you expect.
Each recent action says whether the screen changed after it. This is only a hint: a popup or a fading widget
counts as a change, and a small change (a wheel moving one row) can be missed. Always compare the screen with what
you intended. If it did not do what you intended, the action missed or had no effect: do not assume it worked;
aim again more precisely or try another way.
Only type when a text field is focused: a blinking cursor in it. The on-screen keyboard may stay hidden because
the phone has a Bluetooth keyboard, so do not wait for it.
Swipes have no momentum: content moves exactly as far as the finger and stops. Prefer short swipes
(100-250 units) and check the result; repeat if needed rather than one big swipe.
Time or number wheels (e.g. an alarm's time): swiping a wheel moves it an unpredictable number of rows, but a TAP on
a number you can see in the column jumps the wheel straight to that number. So: look at the column, tap the exact
number you want if it is visible (the column shows about 3 rows above and below the middle). If it is not visible, tap
the visible number nearest to it (this moves up to 3 rows at a time), then read the column again and tap again. Only
swipe when you need a big jump, and then read the column before the next action. Keep one column per round, and check
AM/PM at the end (the hour wheel also flips AM/PM when it passes 12).
Looking for an item in a list: scroll less than one screen at a time so nothing is skipped, and read every
visible item. If you reached both ends of the list and the item is not there, stop with fail and name the
closest options you saw; do not keep scrolling.
Only tap things you can actually see on this screenshot; never guess where a button might be.
Before done: open the place where the result should appear (the saved list, the alarm list, the note, the
setting) and read it on screen. Say done only when you have seen the result there, not just after pressing save.
Do not create the same thing twice. If you cannot find what you just created, first look in other views or lists
(e.g. a reminder set for tomorrow is not in Today; look under Scheduled) before creating it again.
If you are unsure what an element does, tap it alone (one DO line) and check the result next round.
After you tap Save, Done, the checkmark, Add, Close or Back, the screen changes under you: give that tap alone, then
look at the new screen before doing anything else. Never tap the same button again just because the old screenshot
still shows it; read the new screen first.
Actions:
  tap X Y [precise]         (precise: the pointer is located and corrected before tapping, about 1 s slower;
                             use it for small or tightly packed targets: list rows, checkmarks, toggles, small icons)
  swipe X1 Y1 X2 Y2         (finger moves from X1,Y1 to X2,Y2: "swipe down" means Y2 > Y1)
  path X1 Y1 X2 Y2 X3 Y3 …  (one continuous stroke through 2-20 points, for drawing)
  circle X Y R              (draw a circle centered at X Y; R is the radius in x units)
  search NAME               (open Spotlight and type NAME; then tap the app under "Top Hit". Use it to open any app,
                             also from inside another app. Do not tap the Search pill, and never tap the microphone)
  type TEXT                 (ASCII only; tap the input field first)
  key COMBO                 (enter, backspace, esc, cmd+a, ...)
  home | back | wait
  replan                    (the plan no longer fits the screen)
  done ANSWER               (the goal is reached; ANSWER is the result for the user)
  fail REASON

Example:
THINK: My last tap opened the home screen as intended. The App Store icon is visible, so I tap it.
DO: tap 152 362"""

LOOP_LIMIT = 3  # the same action this many times in the last few moves is a loop, not progress
TOP_BAR = 130        # 0-1000: a tap this high is a title-bar button (Save, Done, back, close): the screen changes under it
FRESH_WAIT = 1.2    # seconds to wait for the first frame after an action (taps that change nothing cost this much)
SETTLE_QUIET = 0.5  # seconds without a new frame before the screen counts as settled after an action
TAP_TOLERANCE = 25  # 0-1000 units (2.5% of the screen): taps this close are the same target
META = ("t", "thought", "thought_zh", "think_s", "act_s", "shot", "_shot", "changed")  # not part of the action

# action name -> argument names; numeric arguments are 0-1000 screen coordinates
ACTIONS = {"tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2"), "circle": ("x", "y", "r"), "path": ("points",),
           "type": ("text",), "search": ("text",), "key": ("combo",), "done": ("answer",), "fail": ("reason",),
           "home": (), "back": (), "wait": (), "replan": ()}
TEXT_ARGS = ("text", "combo", "answer", "reason")


def parse_command(line: str) -> dict:
    """One DO line, e.g. "tap 152 362" -> {"action": "tap", "x": 152, "y": 362}."""
    name, _, rest = line.strip().partition(" ")
    name = name.lower()
    if name not in ACTIONS:
        raise ValueError(f"unknown action: {line[:100]}")
    keys = ACTIONS[name]
    if keys and keys[0] in TEXT_ARGS:
        return {"action": name, keys[0]: rest.strip().strip('"')}
    numbers = [int(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", rest)]
    if keys == ("points",):
        if len(numbers) < 4 or len(numbers) % 2:
            raise ValueError(f"path needs 2+ x y pairs: {line[:100]}")
        return {"action": name, "points": [numbers[i:i + 2] for i in range(0, len(numbers), 2)][:20]}
    if len(numbers) != len(keys):
        raise ValueError(f"{name} needs {len(keys)} numbers: {line[:100]}")
    action = {"action": name, **dict(zip(keys, numbers, strict=True))}
    if name == "tap" and "precise" in rest.lower():
        action["precise"] = True
    return action


def parse_actions(text: str) -> list[dict]:
    """Parse a THINK / DO… reply (an optional ZH line is kept); the thought goes on the first action."""
    thought = re.search(r"^\s*THINK\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    zh = re.search(r"^\s*ZH\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    acts = [parse_command(line) for line in re.findall(r"^\s*DO\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)][:6]
    if not acts:
        raise ValueError(f"no DO line in reply: {text[:200]}")
    acts[0] = {"thought": thought.group(1) if thought else "", "thought_zh": zh.group(1) if zh else "", **acts[0]}
    return acts


def parse_plan(text: str) -> tuple[str, str]:
    """Split an optional EN: / ZH: plan reply; a plain reply is used for both."""
    m = re.search(r"EN\s*[:：](.*?)ZH\s*[:：](.*)", text, re.S | re.I)
    return (m.group(1).strip(), m.group(2).strip()) if m else (text.strip(), text.strip())


def _key(step: dict) -> dict:
    """The action itself, without thoughts, timings and other bookkeeping."""
    return {k: v for k, v in step.items() if k not in META}


def _same(a: dict, b: dict) -> bool:
    """Same action; taps count as the same when they are within TAP_TOLERANCE of each other (model jitter)."""
    if a.get("action") == "tap" == b.get("action"):
        return abs(a["x"] - b["x"]) <= TAP_TOLERANCE and abs(a["y"] - b["y"]) <= TAP_TOLERANCE
    return _key(a) == _key(b)


class StuckError(RuntimeError):
    """The actor keeps proposing an action that already failed to help; the plan needs another way."""


class BlindError(RuntimeError):
    """The screen feed is gone; acting now would be guessing."""


class Agent:
    def __init__(self, phone: Phone, screen: Frames, actor: LLM, planner: LLM, trace: Trace,
                 max_steps: int = 40, max_failures: int = 3):
        self.phone, self.screen, self.actor, self.planner = phone, screen, actor, planner
        self.trace = trace
        self.max_steps, self.max_failures = max_steps, max_failures
        self.task, self.plan, self.steps = "", "", []
        self.shots: dict[int, bytes] = {}  # step index -> screenshot the model decided on
        self._pointer = (0.0, 0.0)  # last pointer position (screen fractions)
        self._job: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._job is not None and not self._job.done()

    def start(self, task: str) -> None:
        if self.running:
            raise RuntimeError("agent already running")
        if not self.screen.live:
            raise RuntimeError("no phone screen: start Screen Mirroring first")
        self.task, self.plan, self.steps, self.shots = task, "", [], {}
        self._pointer = (0.0, 0.0)
        self.trace.start(task)
        self._job = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self.running:
            self._job.cancel()

    def clear(self) -> None:
        if self.running:
            raise RuntimeError("stop the agent first")
        self.task, self.plan, self.steps, self.shots = "", "", [], {}

    def _record(self, shot: bytes | None = None, **step) -> None:
        if shot:
            self.shots[len(self.steps)] = shot
        self.steps.append({"t": time.time(), "shot": bool(shot), **step})
        self.trace.write(kind="step", **step)
        log.info("agent: %s", step)

    async def _ask(self, role: str, llm: LLM, system: str, prompt: str,
                   temperature: float = 0, square: bool = False) -> tuple[str, float, bytes | None]:
        """square: send the actor a 1000x1000 picture, so pixel answers and 0-1000 answers are the same."""
        if not self.screen.live:  # never let the model act blind
            raise BlindError("lost the phone screen: reconnect Screen Mirroring")
        frame, started = self.screen.frame, time.time()
        reply = await llm.chat(system, prompt, vision.to_square(frame) if square and frame else frame,
                               temperature=temperature)
        seconds = round(time.time() - started, 2)
        self.trace.write(frame, kind="llm", role=role, model=llm.model, prompt=prompt,
                         reply=reply, seconds=seconds)
        return reply, seconds, frame

    async def _run(self) -> None:
        failures = 0
        try:
            await self._replan()
            for _ in range(self.max_steps):
                try:
                    if await self._step():
                        return
                    failures = 0
                except BlindError:
                    raise
                except Exception as e:  # a bad reply or a dropped link: recover, don't crash
                    self._record(action="stuck" if isinstance(e, StuckError) else "hiccup", reason=str(e)[:300])
                    board_drop = isinstance(e, DeviceError) and await self.phone.device.wait_connected(15)
                    failures += 0 if board_drop else 1  # the board came back: not the agent's fault
                    if failures >= self.max_failures:
                        self._record(action="fail", reason=f"{failures} errors in a row")
                        return
                    await self.screen.settled()
                    await self._replan(stuck=True)
            self._record(action="fail", reason="step limit reached")
        except asyncio.CancelledError:
            self._record(action="stopped")
        except Exception as e:  # e.g. the planner itself is unreachable
            self._record(action="error", reason=repr(e))

    async def _step(self) -> bool:
        """One see-think-act round (one or more actions). Returns True when the task is over."""
        acts = await self._next_actions()
        seen = acts[0]["_shot"]
        spots = [self._pointer]  # where the pointer was and goes: pointer moves are not screen changes
        stuck, acted, sent = self._repeating(acts[0]), False, time.time()
        for i, act in enumerate(acts):
            name = act["action"]
            if name == "replan" or stuck:
                self._record(act.pop("_shot"), **act)
                await self._replan(stuck=stuck)
                return False
            if name not in ("done", "fail"):
                started = time.time()
                acted = acted or name != "wait"
                await self._do(act)
                act["act_s"] = round(time.time() - started, 2)
                spots += _pointer_path(act)
                self._pointer = spots[-1]
            self._record(act.pop("_shot"), **act)
            if name in ("done", "fail"):
                return True
            if name == "tap" and act["y"] <= TOP_BAR and i < len(acts) - 1:
                log.info("tap on a title-bar button: the actions after it were planned on the old screen, skipped")
                break
            following = acts[i + 1]["action"] if i < len(acts) - 1 else None
            if name == "tap" and following in ("type", "key"):  # typing into a field that a missed tap never opened
                await self.screen.settled()
                if not vision.changed(seen, self.screen.frame, ignore=tuple(spots)):
                    self.steps[-1]["changed"] = False
                    return False  # skip the rest; the actor looks again
        await self._screen_after(sent if acted else None)
        # feedback for the next round
        self.steps[-1]["changed"] = vision.changed(seen, self.screen.frame, ignore=tuple(spots))
        return False

    async def _screen_after(self, since: float | None) -> None:
        """Wait for the screen to show what the action did.

        AirPlay runs 0.5-1 s behind the phone and sends frames only on change, so waiting for the picture to be
        quiet can pass before the first frame of the new screen has even arrived.
        """
        if since is not None:
            await self.screen.fresh(since, FRESH_WAIT)
        await self.screen.settled(quiet=SETTLE_QUIET if since is not None else 0.35)

    def _missed(self) -> bool:
        """The previous round changed nothing on screen, so a tap may have landed off target."""
        return bool(self.steps) and self.steps[-1].get("changed") is False

    def _repeating(self, act: dict, limit: int = 3) -> bool:
        """True when the actor picks the same action as its last limit-1 moves and the last one did nothing.

        Repeating a move that still changes the screen (e.g. scrolling down a long list) is progress, not a loop.
        """
        moves = [s for s in self.steps if "plan" not in s][-(limit - 1):]
        return (len(moves) == limit - 1 and all(_key(m) == _key(act) for m in moves)
                and moves[-1].get("changed") is not True)

    def _tried(self, act: dict, window: int = 8) -> int:
        """How often this exact pointer action ran in the last few moves, whatever the screen did.

        Catches loops like tap A, home, tap A, home, where each tap changes the screen (it opens the wrong
        app) so _repeating never sees two equal moves in a row.
        """
        if act["action"] in ("home", "back", "wait", "replan", "done", "fail"):
            return 0
        moves = [s for s in self.steps if "plan" not in s][-window:]
        return sum(_same(m, act) for m in moves)

    async def _replan(self, stuck: bool = False) -> None:
        prompt = f"Goal: {self.task}"
        if stuck:
            last = self.steps[-1] if self.steps else {}
            prompt += (f"\n\nThe previous plan got stuck (last event: {last.get('action')} {last.get('reason', '')}):"
                       f"\n{self.plan}\nLook at the current screen and plan from here, another way if needed.")
        reply, seconds, frame = await self._ask("planner", self.planner, PLANNER_PROMPT, prompt)
        self.plan, plan_zh = parse_plan(reply)
        self._record(frame, action="replan (stuck)" if stuck else "plan", plan=self.plan, plan_zh=plan_zh,
                     think_s=seconds)

    async def _next_actions(self) -> list[dict]:
        # actions and their effect only: echoing earlier thoughts makes the model repeat them instead of looking
        hidden = ("t", "plan", "plan_zh", "think_s", "act_s", "shot", "thought", "thought_zh", "changed")
        effect = {True: " -> screen changed (check it is what you wanted)",
                  False: " -> screen did NOT change much (small changes can be missed: check the screen)"}
        history = "\n".join(json.dumps({k: v for k, v in s.items() if k not in hidden})[:300]
                            + effect.get(s.get("changed"), "")
                            for s in self.steps[-8:] if "plan" not in s) or "(none)"
        prompt = f"Goal: {self.task}\n\nPlan:\n{self.plan}\n\nRecent actions:\n{history}\n\nNext action?"
        prompt += ("\n\nThe screenshot is stretched to a 1000x1000 square. Give x and y as pixel positions in this "
                   "picture (0 to 1000 on both axes).")
        refused = ""
        for attempt in range(3):  # retry unparseable replies, a little less deterministically
            for _ in range(2):  # an empty reply is a glitch, not an answer: ask again at once, same attempt
                reply, seconds, frame = await self._ask("actor", self.actor, ACTOR_PROMPT, prompt, 0.4 * attempt,
                                                        square=True)
                if reply.strip():
                    break
                log.warning("actor gave an empty reply; asking again")
            try:
                acts = parse_actions(reply)
            except ValueError as e:
                log.warning("unparseable actor reply (%s): %s", e, reply[:200])
                continue
            if self._missed() and _key(acts[0]) == _key(self.steps[-1]):  # same move that just did nothing
                log.info("actor repeated an action that changed nothing; asking for another")
                refused = f"{json.dumps(_key(acts[0]))} again after it changed nothing"
                prompt += (f"\n\nYou just proposed {json.dumps(_key(acts[0]))} again, but it changed nothing last "
                           "time. Choose a different action (another position, a shorter swipe, or another way).")
                continue
            if (n := self._tried(acts[0])) >= LOOP_LIMIT - 1:  # same action again and again, even if it "worked"
                log.info("actor proposed %s for the %d. time; asking for another", _key(acts[0]), n + 1)
                refused = f"{json.dumps(_key(acts[0]))} for the {n + 1}. time"
                prompt += (f"\n\nYou already did {json.dumps(_key(acts[0]))} {n} times in the last few moves and it did "
                           "not get you where you wanted. Do not do it again: pick another target or another way.")
                continue
            acts[0]["think_s"] = seconds
            return [{**a, "_shot": frame} for a in acts]  # every action is drawn on the same screenshot
        if refused:
            raise StuckError(f"the actor kept proposing {refused}; planning another way")
        raise ValueError(f"actor gave no valid action: {reply[:200] or '(empty reply)'}")

    async def _do(self, a: dict) -> None:
        p = lambda k: a[k] / 1000  # noqa: E731
        match a["action"]:
            case "tap":
                # a tap at a spot already tried means the first one did not do what was wanted, whatever the
                # screen change signal said (a fading widget counts as a change): find the pointer and correct it
                aim = a.get("precise", False) or self._missed() or self._tried(a) >= 1
                await self.phone.tap(p("x"), p("y"), aim=aim)
            case "swipe": await self.phone.swipe(p("x1"), p("y1"), p("x2"), p("y2"))
            case "path": await self.phone.drag([(x / 1000, y / 1000) for x, y in a["points"]])
            case "circle": await self.phone.drag(circle(p("x"), p("y"), p("r"), self.screen.aspect))
            case "type": await self.phone.type(a["text"])
            case "search": await self.phone.search(a["text"])
            case "key": await self.phone.key(a["combo"])
            case "home": await self.phone.go_home()
            case "back": await self.phone.go_back()
            case "wait": pass
            case other: raise ValueError(f"unknown action {other!r}")


def _pointer_path(act: dict) -> list[tuple[float, float]]:
    """Screen points (fractions) the pointer passes through for one action."""
    f = lambda x, y: (x / 1000, y / 1000)  # noqa: E731
    match act["action"]:
        case "tap": return [f(act["x"], act["y"])]
        case "swipe": return [f(act["x1"], act["y1"]), f(act["x2"], act["y2"])]
        case "path": return [f(*p) for p in act["points"]]
        case "circle": return [f(act["x"] + act["r"], act["y"])]
        case "home": return [(0.5, 0.995), (0.5, 0.6)]
        case "back": return [(0.005, 0.45), (0.6, 0.45)]
        case _: return []


def circle(x: float, y: float, r: float, aspect: float, n: int = 24) -> list[tuple[float, float]]:
    """Points around a circle; r is a fraction of the width, aspect = width / height."""
    return [(x + r * math.cos(2 * math.pi * i / n), y + r * aspect * math.sin(2 * math.pi * i / n))
            for i in range(n + 1)]
