# I ran an AI team for a week. It changed how I think about management.

I'm a product leader. Most of my job is figuring out how teams build things, where they get stuck, and why smart people still ship the wrong thing.

A few weeks ago I ran an experiment. I wanted to know what happens if you replace a human engineering team with AI agents and try to manage them the way you'd manage real people.

I built a system that coordinates four AI agents: a Product Manager, an Architect, two Developers, and a Code Reviewer. Each one has a defined role, clear boundaries, and a structured way to pass work to the next. I pointed it at a real codebase and ran it for a week.

By the end I had one fully autonomous pull request, 305 tests, a live dashboard, and a bunch of lessons I wasn't expecting. Not about AI. About leadership.

---

## Why I did this

Everyone talks about AI replacing jobs. Fewer people talk about what it feels like to actually work alongside AI day after day. The demos look great. The blog posts are optimistic. I wanted the ground truth.

Not "can AI write code?" That's answered. But can AI agents coordinate? Can they specialize and hand off work without losing context? Can they produce something that survives review? And what does the human actually need to do to make any of this work?

I learn by doing. The fastest way to understand a new dynamic is to put yourself inside it. So I built the thing, turned it on, and paid attention.

---

## The setup

Four agents, each with a job:
- The PM analyzes the codebase and proposes improvements
- The Architect reviews proposals for feasibility and writes specs
- The Developers implement approved specs on isolated branches
- The Reviewer checks the code and approves or sends it back

They talk through a message pipeline. Each handoff is structured, not a conversation but a typed contract. There's a safety layer that blocks dangerous actions and escalates risky ones for human approval. There's a dashboard so I can watch everything in real time.

I'm the operator. The only human. The one who defines what matters, watches the work, and steps in when something breaks.

Here's what I learned.

---

## This felt like managing a real team

I expected it to feel like configuring software. It didn't. Within hours, dynamics I've seen on every team I've led started showing up.

One role was generating more work than the next could absorb. Handoffs were the source of failure, not individual ability. Unclear expectations caused quality drift. The system could look impressively busy without producing anything valuable.

That last one got me. I've sat in standups where everyone sounds productive and the sprint board is full of movement, but the product isn't getting better. This was that, except with AI. Lots of proposals. Lots of reviews. Lots of branches. Very little of it aimed at the right problem.

The surprise wasn't that AI can do work. It's that AI work has the same organizational failure modes as human work.

---

## The PM sets the ceiling

This shaped the rest of my week.

Left alone, the AI PM generated twelve proposals in its first run. Well-articulated, technically reasonable, covering everything from tests to performance to security. The Architect approved two. Eighty-three percent rejection rate.

The PM wasn't stupid. It was unfocused. Doing what an unsupervised PM does: generating work that sounds good but isn't grounded in what actually matters to the user.

I've managed human PMs with this exact problem. Smart, productive, drifting. The fix is never "work harder." It's always "get clearer on what matters."

So I wrote a one-page product brief. Three to five priorities, explicit out-of-scope items. The kind of thing you'd put in front of a new PM on their first day. The effect was immediate. Proposals aligned. Approval rate went up. Work started flowing toward real value.

Later I went further and wrote down what "good PM" means in this system: start from user need, not code neatness. Evaluate the product, not just the codebase. Notice confusion and friction and trust gaps. Describe expected outcomes, not implementation scope.

This didn't feel like prompt engineering. It felt like coaching a new hire. And that's when something clicked: the skills that matter in an AI world aren't technical. They're leadership skills.

---

## Handoffs are where work dies

The biggest source of failure wasn't any single agent. It was the space between them.

Early on, the agents talked to each other in natural language. Paragraphs of reasoning passed from one role to the next. Each stage interpreted the previous one and generated more text. By the time the Developer got a spec, it had been through two layers of translation.

You know this. It's the telephone game that happens in every organization. Product writes a brief. Design interprets it. Engineering interprets the interpretation. What ships doesn't match what anyone intended.

On human teams, people compensate by asking questions, reading body language, catching things in hallway conversations. AI agents can't do any of that. They take what they get and execute. If the handoff is muddled, the output is confidently wrong.

I replaced the prose with minimal typed payloads. Title, priority, acceptance criteria. Decision, blocking issues, specific file and line comments. Like replacing an hour-long meeting with a well-designed ticket template. Less expressive, way more reliable.

The meta-lesson: good process design isn't about control. It's about removing the places where meaning gets lost. That's true for human teams too. We just tolerate more noise because humans can compensate. AI makes you see how much waste that compensation hides.

---

## Activity is not progress

This kept coming back all week.

The system could be extremely busy. Agents active, messages flowing, branches being created, reviews coming back. And I'd look at the dashboard and feel nothing. No confidence that anything meaningful was happening.

Twelve proposals in flight and I couldn't tell which ones mattered. Threads "in progress" but I didn't know if that meant "almost done" or "stuck on the same bug for the third time." The dashboard showed state but not trajectory.

The gap between activity and trust was the most important product insight of the week.

I redesigned the dashboard four times. Each time was a response to the same question: what do I actually need to know? The answer was never "more data." Always "better framing."

The final version organized around four questions: What is stuck? What needs my decision? What failed? Is the system making real progress? Everything else got demoted or removed. The dashboard stopped being an operator console and became a leadership tool.

I keep thinking about this in terms of the products I build for human teams. How much of what we show people is activity, and how much is progress? How many dashboards create the illusion of visibility without actually building trust?

---

## The economics of waste

Here's something most AI writing glosses over: this costs real money, and the waste is visible in a way it never is with human teams.

Every proposal costs tokens. Every review costs tokens. Every rejected implementation costs tokens. When the PM generated twelve proposals and ten got rejected, those ten still showed up on the bill. When the Developer failed the same code review three times, each attempt cost money, and the third produced the same wrong answer as the first.

On human teams, waste is hidden in salaries and opportunity cost. It's abstract. On AI teams, waste shows up as a line item. Every rejected proposal is an invoice. Every review loop is compound interest on a bad implementation.

Uncomfortable but useful. It changed how I thought about flow. Don't start work the pipeline can't finish. Stop generating new proposals when existing ones haven't been reviewed. The gate has to come before the spend, not after.

Same principles as lean manufacturing and WIP limits. But with AI, the feedback is immediate and financial.

The agents created 24 branches during the week. Four were useful. That's an 83% waste rate. Most people assume AI means efficiency. The reality was closer to high output, low yield. The system generates before it evaluates. Everything enters the pipeline. The pipeline has to do the filtering.

The question I keep coming back to isn't "can AI do the work?" It's whether you can design a system where the work that gets done is the work that matters. That's not a technology problem.

---

## You are the only one with judgment

This was the loneliest part.

On a human team, judgment is distributed. The PM has opinions. The architect pushes back. The developer raises concerns. When something drifts in the wrong direction, multiple people notice and react from different angles.

On an AI team, I was the only one who could tell whether the work mattered.

When the PM drifted toward internal cleanup instead of user-facing improvements, nobody noticed but me. When a thread was stuck on a bug the Developer couldn't fix, no agent flagged it. They just kept trying. When the dashboard surfaced the wrong information, nobody was confused except me.

The agents don't get frustrated. They don't wonder if the project is heading the right way. They don't have that nagging feeling that something is off. All of that sits with the single human.

I found myself wanting a second perspective. Not a second model. The system already had internal critique mechanisms. A second judgment. Someone to say "are we sure this is the right priority?" or "I think we're solving the wrong problem."

That's the real job of a leader when the team is AI: being the judgment the system can't generate for itself. Not task assignment. Not status tracking. Judgment about what matters and whether the work is going somewhere valuable.

It wore me out in ways I didn't anticipate.

---

## Knowing when to step in

The hardest skill I developed was knowing when to intervene and when to let things run.

One thread was stuck. The Developer kept submitting code, the Reviewer kept catching the same bug: a subtle issue with library-specific exception types that the Developer just couldn't see. After three rounds, the system blocked the thread automatically.

I could have reset it. Let the Developer try a fourth time. But the Reviewer was right every round, and the Developer had hit a wall. More retries would burn more money on the same wrong answer.

I abandoned the thread instead. That felt like a management decision, not a system failure. Some work items are correctly identified, correctly scoped, and beyond what your team can execute right now. Recognizing that and stopping is the same call a manager makes when they reassign a task to someone more senior.

Other days I paused the PM because the pipeline was full. Other times I flushed stale data because restarting clean was better than debugging noise. Each call required understanding not just the system state but the semantic state. Not "is this thread stuck?" but "is it stuck for a reason more effort won't fix?"

The system automates execution. It doesn't automate judgment about when execution isn't working. That stays with the human. And getting good at that judgment, fast, with incomplete information, is the core skill of running an AI team.

---

## When the team analyzed itself

Halfway through, I pointed the system at its own codebase. Self-improvement seemed like the natural next step.

Hardest thing I attempted. Not technically. Structurally.

The PM proposed changes to files the safety layer was designed to protect. The proposals silently vanished. No error, no gate, just silence. The PM framed everything wrong. Its default lens is "user pain points" but the product it was analyzing was developer infrastructure. It proposed user-facing features for a system that doesn't have users in the traditional sense.

Deeper problem: a system that edits the code defining its own behavior has failure modes you don't see anywhere else. Its mistakes distort the environment it uses to evaluate itself.

The fix wasn't technical. It was framing. I created an explicit self-analysis mode with different expectations, different priorities, and tighter human oversight. The system needed to know it was looking at itself, and I needed to see every decision it made about itself.

The product lesson: context changes everything. The same team, same capabilities, needs different framing depending on what it's working on. Good leadership means noticing when the context has shifted and adjusting expectations accordingly.

---

## Build the window before the machine

The most valuable thing I built all week wasn't the agent pipeline. It was the dashboard.

Four redesigns in five days.

The first was an engineer's console. Raw data, message dumps. I built it because that's what I know how to build. Useless for actual decisions.

The second looked professional. Dark theme, live feeds, the Datadog aesthetic. Looked great. Still showing the wrong stuff. I had a beautiful dashboard that told me what was happening but not what I should do about it.

Third time I asked a better question: what does the operator need to decide? That reframe finally got me somewhere. I tore out most of what I'd built and organized around four things: what's stuck, what needs me, what failed, is the system actually making progress.

The fourth redesign came from one specific frustration. I kept switching between views trying to follow a single proposal through the pipeline. So I added a way to click a thread and see its full journey, from the PM's idea through review, implementation, and back. That one feature taught me more about the system than all the aggregate metrics combined.

There's a product principle here that goes well beyond AI: observability is not about showing data. It's about building trust. If the people responsible for a system can't understand what it's doing and why, it doesn't earn trust no matter how well it performs.

I also noticed that the features I most needed came from my own frustration. I kept manually checking pipeline health, so I automated the check. I kept asking "what needs my attention?" so I built an attention surface. I kept pausing the PM by hand, so I built a pause button.

The best features were the ones I built to stop doing the same thing twice. That's a product instinct, and this experiment sharpened it.

---

## I used AI to build the AI team

The meta-layer I can't skip: I built this whole system in collaboration with AI. The same technology powering the agents was my pair-programming partner for the orchestrator, the dashboard, the safety layer, everything.

That loop had consequences. My AI collaborator would sometimes break things I'd just fixed. I'd give vague direction and get technically correct output that missed the point. The most productive moments were when I gave structured, specific feedback. Not open-ended instructions.

Same lesson the agents taught me. Managing an AI collaborator and managing an AI team take the same skills. Clear expectations. Structured feedback. Verification.

The people who thrive in an AI world won't necessarily be the most technical. They'll be the ones who can define what good looks like, say it precisely, and check whether they got it.

Those are product skills. Leadership skills. Coaching skills.

---

## What this means for how we'll work

I started this as an experiment. I came out with a sharper view of where things are headed.

The work of management is going to change. Less task assignment, less status tracking. AI handles that. More standard-setting, more judgment calls. Those stay with humans.

Product thinking becomes more important, not less. When you can generate unlimited work, the bottleneck moves from execution to direction. "What should we build?" matters more than "can we build it?"

The skills that matter shift. Defining what good looks like, for each role, for each handoff. Knowing when to intervene and when to back off. Designing for coordination, not just execution. Building trust through visibility. Calibrating what AI can reliably do versus where humans close the gap.

The role of a founder or product leader starts to look more like coaching. You're not managing anyone's time. You're defining standards, strengthening boundaries, creating feedback loops. You're deciding what matters and encoding that decision in a way the system can act on.

---

## The biggest takeaway

An AI team doesn't remove the need for leadership. It makes leadership more explicit.

You still have to define what good looks like for each role. How work should flow. What's automatic versus manual. What changes are actually valuable. How the system knows whether it's improving the product or just producing output.

On a human team, a lot of this is implicit. Carried in people's heads. Communicated in hallway conversations. Enforced by social norms. On an AI team, you have to write all of it down. Every assumption, every standard, every expectation.

It's like running a team with perfect amnesia. Every day is day one. Every message is the entire relationship. No trust accumulates. No shared understanding builds up over time.

That sounds exhausting, and it is. But it's also clarifying.

Because a lot of what makes teams work is stuff we rarely articulate. What we expect from each role. How handoffs should work. What quality looks like. When to escalate. What matters most.

This experiment forced me to articulate all of it. Honestly, my human teams would benefit from the same exercise.

The most personal takeaway: running an AI team didn't make me feel less needed as a product leader. It made me feel needed differently. Less task management. More standard-setting. Less "what should we build next?" More "what does good look like, and how will we know?"

I'm still figuring out if that's a smaller job or a harder one. Right now it feels like both.

---

## The numbers

| | |
|---|---|
| Proposals generated | 12 |
| Approved by Architect | 2 (17%) |
| Fully autonomous completions | 1 |
| Review loops correctly stopped | 1 (after 3 cycles) |
| Branches created | 24 |
| Branches that were useful | 4 |
| Dashboard redesigns | 4 |
| Waste rate | ~83% |

One pull request made it all the way through. The system found the opportunity, checked feasibility, wrote the code, reviewed it, and shipped it. No human wrote a line. The human just defined what mattered.

Low yield? Sure. But I didn't do this for the yield. I did it to understand something. And I do now, a lot better than before.

---

## If you're thinking about trying this

Build the window before the engine. I wish I'd started with the dashboard on day one instead of day two. You can't lead what you can't see.

Write a product brief before you touch any configuration. One page. Three priorities. What's out of scope. That single page shaped the entire week more than any technical decision I made.

Structure beats intelligence. I had this proven to me repeatedly. Smart agents with bad handoffs produce waste. Average agents with clear contracts produce value. Design the handoffs first.

Don't let the system start work it can't finish. I burned real money learning this. The gate has to come before the spend.

Plan for filtering, not just production. AI generates before it evaluates. I had an 83% waste rate on branches. That's fine if you plan for it. It's expensive if you don't.

Build for human handoff at the edges. There will be things the AI can't do. The system should find that boundary and stop, not loop forever hoping the fourth attempt works.

You will be the only one who notices when the direction is wrong. That's the real job. It's lonely and it's tiring and it matters more than anything else.

One last thing: the learning compounds. Every frustration I had became a feature. Every failure became a design principle. Even when the system was broken, I was getting better at understanding how teams work. That part was worth the whole experiment.

I don't think AI replaces the human in the loop. I think it changes what the human needs to be good at. This week gave me a pretty concrete sense of what that looks like.

---

*I built [Agent Orchestrator](https://github.com/ali-myftiu/agent-orchestrator) to learn by doing. Four AI agents working as a structured team on real codebases. The system is open source if you want to try it yourself.*
