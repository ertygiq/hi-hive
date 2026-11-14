# Task conversations

Hive runs tasks through agent harnesses and keeps their conversations available for steering, review, and follow-up.

## Language

**Task conversation**:
The initial prompt, agent answers, steering messages, self-reviews, and follow-ups belonging to one task.
_Avoid_: Chat history when referring only to follow-ups.

**Initial prompt**:
The user's original instruction for the task.

**Steering message**:
An additional user instruction sent while the agent is working.
An answer can address both the initial prompt and steering messages.

**Follow-up**:
A user instruction sent after the preceding work has settled.

**Self-review**:
An instruction for the agent to check its work, followed by any answers produced during that review.

**Delivery**:
What Hive knows about whether the harness received a submitted message.
Acceptance does not mean the agent has answered it.

**Settlement**:
The point at which the submitted work has finished without further automatic continuation.
Settlement does not imply a separate answer for every input.

**Runner diagnostics**:
Technical records about harness processes and message delivery, separate from conversation content.
