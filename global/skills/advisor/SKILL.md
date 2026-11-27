---
name: advisor
description: Get a second opinion on a plan, a design or a stuck problem. Use when the built-in /advisor command is not available.
---

# Advisor

Hand the question to the `kerbtap:advisor` agent and return its answer.

1. Take the question from the user's words after the command. If there are none, use the problem the session is working on now.
2. Write the question and the session's current task in a short brief. Name the files that the question concerns.
3. Start the `kerbtap:advisor` agent with that brief.
4. Return its answer to the user without change. Do not add your own opinion to it.
