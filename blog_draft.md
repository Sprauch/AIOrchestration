# What running an AI team actually looks like

I've spent years building products and running engineering teams. The thing I keep coming back to is the gap between how teams are supposed to work and how they actually work. The handoffs that lose context. The PMs who optimize for ticket volume instead of user value. The dashboards that show activity but not progress. That gap is where most of the real product work happens.

I learn by doing. One of my goals this quarter was to build a real understanding of agentic AI, not just read about it. Last quarter I spent rebuilding my personal website using RAG. This quarter I wanted to go deeper: understand what happens when AI isn't a tool you use but a team you run.

But I also had a second goal. As a product person, I wanted to form my own opinion on what AI actually means for the role, the craft, and the team. Everyone talks about AI PMs. Nobody agrees on what that means, and most of the conversation is theoretical. I wanted something concrete. So I decided to simulate being an AI-first product founder: build something product-shaped that simulates what the world looks like when you have an AI team instead of a human one, run it for real, and see what the experience teaches you.

So I built a system that coordinates five AI agents: a Product Manager, a Product Designer, a Tech Lead, two Developers, and a Code Reviewer. Each one has a defined role, clear boundaries, and a structured way to pass work to the next. I pointed it at a real codebase and ran it for a week.

By the end I had one fully autonomous pull request, 360 tests, a live dashboard, and a set of lessons I wasn't expecting. Not about AI. About how I lead.

Can AI agents coordinate? Can they specialize and hand off work without losing context? Can they produce something that survives code review? And what does the human actually need to do to make any of this work?

I could have read about this. Instead I built it, turned it on, and paid attention. That's how I've always learned fastest. Not from the outside looking in, but from inside the thing, watching what breaks.

The team works like this:

- The PM analyzes the codebase and proposes improvements, framed around user need
- The Product Designer reviews user-facing proposals for experience quality, clarity, and trust
- The Tech Lead reviews proposals (with design feedback attached) for technical feasibility, shapes the implementation, and assigns tasks
- The Developers implement approved specs on isolated branches
- The Reviewer checks the code and approves or sends it back

The PM routes work based on what kind of proposal it is. User-facing work (product, UX, trust, onboarding, workflow) goes through the Designer first, who adds experience feedback before passing to the Tech Lead. Technical proposals skip straight to the Tech Lead. The pipeline adapts its depth based on what's flowing through it.

They talk through a message pipeline. Each handoff is a typed contract, not a conversation. There's a safety layer that blocks dangerous actions and escalates risky ones for human approval. There's a dashboard so I can watch everything in real time.

One thing that struck me during the week was how much the experiment echoed ideas from *Team Topologies*. Even with AI agents, clear boundaries mattered, cognitive load still mattered, and every extra handoff introduced friction. The supporting layer around the team (orchestration, safety, monitoring, recovery) ended up being just as important as the execution roles themselves. My setup was more pipeline-shaped than ownership-shaped, which made coordination costs especially visible. A useful reminder that adding roles doesn't automatically create clarity. Sometimes it just creates more handoffs.

I'm everything. The person who designed the pipeline. The product lead who defines what the PM should care about. The operator watching the dashboard. The one debugging race conditions at 3pm. The only human on a team of six.

---

## The surprise: it felt exactly like managing a real team

I expected it to feel like configuring software. It didn't. Within hours, dynamics I've seen on every team I've led started showing up.

One role was generating more work than the next could absorb. Handoffs were the source of failure, not individual ability. Unclear expectations caused quality drift. The system could look impressively busy without producing anything valuable.

That last one got me. I've sat in standups where everyone sounds productive and the sprint board is full of movement, but the product isn't getting better. This was that, except with AI. Lots of proposals. Lots of reviews. Lots of branches. Very little of it aimed at the right problem.

Left alone, the AI PM generated twelve proposals in its first run. Well-articulated, technically reasonable, covering everything from tests to performance to security. The Tech Lead approved two. Eighty-three percent rejection rate. The PM wasn't stupid. It was unfocused. Doing what an unsupervised PM does: generating work that sounds good but isn't grounded in what actually matters to the user.

I've managed human PMs with this exact problem. Smart, productive, drifting. I've also been that PM. The fix is never "work harder." It's always "get clearer on what matters."

So I did what I'd do with any team. I wrote a one-page product brief. Three to five priorities. Explicit out-of-scope items. The effect was immediate. Proposals aligned. Approval rate went up. Work started flowing toward real value.

What surprised me was how directly the playbook transferred. The AI PM responded to product leadership the same way a human PM does: give it clear priorities and the output gets dramatically better. The skill wasn't AI-specific. It was the same instinct I've been developing my whole career, applied in a new context.

Early on, the agents talked to each other in natural language. Paragraphs of reasoning passed from one role to the next. Each stage interpreted the previous one and generated more text. By the time the Developer got a spec, it had been through multiple layers of translation. The telephone game that happens in every organization, except here nobody could ask a clarifying question.

I replaced the prose with minimal typed payloads. Title, priority, acceptance criteria. Decision, blocking issues, specific file and line comments. Less expressive, way more reliable. I've been advocating for tighter handoff contracts on human teams for years. AI made the case more clearly than any retrospective ever has. When you remove the human ability to compensate for noise, you see exactly how much waste that noise creates.

One proposal went through three review cycles. The Developer kept submitting code, the Reviewer kept catching the same bug: a subtle issue with library-specific exception types. After three rounds, the system blocked the thread. I've seen this exact loop on human teams. The difference is the system detected it in hours instead of sprints.

Adding the Product Designer was a later evolution. The original four-agent system treated every proposal the same way: straight to technical review. But user-facing work needs a different lens. The Designer as a craft review stage before the Tech Lead meant proposals about experience, trust, and usability got evaluated on those terms before anyone thought about implementation. The Tech Lead received proposals with design context already attached, which made technical decisions better informed. Same reason real teams have designers review specs before engineering starts.

At some point I realized what I was actually doing. Not just running an AI team. Running a controlled experiment on management itself, stripped of all the human compensation that normally hides the dynamics. Human teams are forgiving enough to cover for fuzzy process. AI teams aren't. The PM drifts without priorities. Handoffs lose context without structure. Activity isn't progress without measurement. Every principle I'd learned from managing real teams showed up, except now I could see it clearly because nothing was being smoothed over by social norms. The AI team made my own leadership instincts visible in a way human teams never had.

I built this system to understand what running an AI-first team requires. Turns out the answer had less to do with technology than I expected.

---

## The twist: it felt nothing like managing a real team

The biggest difference: AI teams turn ambiguity into system behavior, not friction. On human teams, ambiguity creates questions and Slack threads. On AI teams, ambiguity creates confidently wrong output. The agents don't ask for clarification. They interpret and continue.

When the workflow state was unclear, the agents didn't work around it. They duplicated work. They looped. They stalled. They filled the pipeline with sophisticated-looking noise. If thread IDs were shared across multiple proposals, the pipeline didn't flag the confusion. It collapsed everything into one untrackable blob.

No informal context. No accumulated team culture. No "everyone knows the Tech Lead cares about error handling." If it's not in the system prompt, it doesn't exist. I found myself writing increasingly specific role definitions, not because the agents were stupid, but because there was no other way to transfer institutional knowledge. That's what led me to define explicit craft for each role: the PM evaluates user need, the Designer evaluates experience quality, the Tech Lead evaluates technical shape. Each one has a written standard for what "good" looks like. On a human team, half of that lives in people's heads.

On a human team, judgment is distributed. The PM has opinions. The tech lead pushes back. The developer raises concerns. Here, I was the only one who could tell whether the work mattered. When the PM drifted toward internal cleanup instead of user-facing improvements, nobody noticed but me. When a thread was stuck on a bug the Developer couldn't fix, no agent flagged it. They just kept trying.

You're the only person in the room with judgment, and you're also the one who built the room. That combination is heavier than it sounds.

It wore me out in ways I didn't anticipate.

I abandoned a stuck thread instead of resetting it for a fourth attempt. Most people learn the "when to stop" skill by wasting weeks on something before admitting it won't work. Here I could see the pattern after three cycles. The system gave me the data to make the call fast. On a human team, the same thing plays out over two sprints before someone escalates.

Other days I paused the PM because the pipeline was full. Other times I flushed stale data because restarting clean was better than debugging noise. The system automates execution. It does not automate judgment about when execution isn't working.

---

## What it actually costs

Something most AI writing glosses over: this costs real money, and the waste is visible in a way it never is with human teams.

Every proposal costs tokens. Every review costs tokens. Every rejected implementation costs tokens. When the PM generated twelve proposals and ten got rejected, those ten still showed up on the bill. When the Developer failed the same code review three times, each attempt cost money, and the third produced the same wrong answer as the first.

On human teams, waste hides in salaries and opportunity cost. Abstract. On AI teams, waste shows up as a line item. Every rejected proposal is an invoice. Every review loop is compound interest on a bad implementation.

Uncomfortable but useful. It changed how I thought about flow. Don't start work the pipeline can't finish. Stop generating new proposals when existing ones haven't been reviewed. The gate has to come before the spend, not after.

The agents created 24 branches during the week. Four were useful. 83% waste rate. Most people assume AI means efficiency. The reality was closer to high output, low yield. The system generates before it evaluates. Everything enters the pipeline. The pipeline does the filtering.

| | |
|---|---|
| Proposals generated | 12 |
| Approved by Tech Lead | 2 (17%) |
| Fully autonomous completions | 1 |
| Review loops correctly stopped | 1 (after 3 cycles) |
| Branches created | 24 |
| Branches useful | 4 |
| Dashboard redesigns | 4 |
| Waste rate | ~83% |

One pull request made it all the way through. The system found the opportunity, checked feasibility, wrote the code, reviewed it, and shipped it. No human wrote a line. The human just defined what mattered.

---

## The role that doesn't have a name yet

I came out of this week frustrated that there's no good name for what I was doing.

"AI PM" is too narrow. I wasn't just defining requirements. I was building the infrastructure, designing the architecture, operating the system, debugging production issues, and making product judgment calls. All at once. All day.

"AI engineer" is too narrow in the other direction. The hardest problems weren't technical. They were about what the PM should optimize for, why the pipeline needed a Designer before the Tech Lead, when the system was doing useful work versus generating noise, and whether the dashboard was building trust or just showing data.

The closest word I have is founder. Not because this is a startup. Because the role requires the same combination: you build the thing, you define the vision, you operate it, you make judgment calls with incomplete information, and you're the only one who can tell if it's working.

During the week I wore four hats, and none of them came off.

I pair-programmed the orchestrator, the dashboard, the safety layer, the test suite. 360 tests, four dashboard redesigns, an auditor I built because I got tired of manually checking pipeline health. When I realized the team needed a Designer role, I wrote the prompt, built the agent, wired it into the pipeline, and updated the routing logic. That's not product management. That's building.

I designed the backpressure model, the three-layer WIP gates, the thread guard, the message contracts between agents. I designed the pipeline topology: PM to Designer to Tech Lead to Developer to Reviewer, with smart routing that skips the Designer for purely technical work. Those decisions about how work flows were mine.

I ran the system live. Diagnosed stalls from Redis state. Paused the PM when the pipeline was full. Abandoned threads that hit capability boundaries. Flushed stale data when restarting was cleaner than debugging.

I defined what each role should care about. Wrote the PM's product brief. Decided the Designer should evaluate for experience quality and trust, not decoration. Decided what the dashboard should show and what to hide. Shaped how proposal quality gets evaluated.

What prepared me for this wasn't any single skill. Years of product work taught me how to define priorities and evaluate whether work is aimed at the right problem. Engineering taught me how to build systems that are observable and debuggable. Running teams taught me when to step in and when to let things play out. This experiment pulled on all of it simultaneously. I don't think you can do it well without the full stack.

That's the bet I'm making on myself: that the most valuable people in AI-first work will be the ones who can operate across all of these dimensions. Not specialists in one. People who can build the system, define the product, operate it live, and make the judgment calls that AI can't.

---

## If you're thinking about trying this

Build the window before the engine. I wish I'd started with the dashboard on day one. You can't lead what you can't see.

Write a product brief before you touch any configuration. One page. Three priorities. What's out of scope. That single page shaped the entire week more than any technical decision I made.

Structure beats intelligence. Smart agents with bad handoffs produce waste. Average agents with clear contracts produce value. Design the handoffs first.

Don't let the system start work it can't finish. I burned real money learning this.

Add craft roles, not just execution roles. The Designer changed the quality of what reached the Tech Lead. On a real team you wouldn't skip design review for user-facing work. Same principle here.

Plan for filtering, not just production. I had an 83% waste rate on branches. That's fine if you plan for it.

Build for human handoff at the edges. The system should find capability boundaries and stop, not loop hoping the fourth attempt works.

You will be the only one who notices when the direction is wrong. Get comfortable with that.

---

## What I'm taking forward

This experiment forced me to articulate every assumption I have about how teams work. What I expect from each role. How handoffs should function. What quality looks like. When to escalate. What matters most. That articulation is now something I'm bringing back to every team I work with. The AI experiment made me a better leader of human teams, because it made my assumptions visible.

Running an AI team didn't make me feel less needed. It made me feel needed in every direction at once. Builder, system designer, operator, product lead. All of them running in parallel, because the system needs all of them and there's only one human in the room.

Some people will call that "AI PM." I don't think that's it. It's closer to founder, just with a different kind of team. You build the system, you define the standards, you operate it, you notice when things drift.

I'm still figuring out if this intensity is sustainable week over week. But the mix of product instinct, technical ability, and operational judgment that this demanded is exactly where I want to be. Not because AI is trendy. Because I came out of this week sharper at every part of my job, and that doesn't happen often.

The learning compounds. Every frustration became a feature. Every failure became a design principle. Even when the system was broken, I was getting better at understanding how teams work, how products earn trust, and where my own instincts are strongest.

I don't think AI replaces the human in the loop. I think it raises the bar for what the human needs to bring. Not just product thinking. Not just engineering. Both, plus the judgment to know which one matters right now. This week gave me a pretty concrete sense of what that looks like in practice.

---

*I'm a product leader and builder exploring what AI-first teams actually require. I built [Agent Orchestrator](https://github.com/ali-myftiu/agent-orchestrator) in a week: architecture, code, dashboard, safety layer, 360 tests, with AI as my pair programmer. Five AI agents (PM, Product Designer, Tech Lead, Developer, Reviewer) working as a structured engineering team on real codebases. Open source if you want to try it. I'm always up for talking to people thinking about the intersection of AI, product, and team design: [LinkedIn](https://linkedin.com/in/alimyftiu).*
