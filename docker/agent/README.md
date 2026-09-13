Generic agent image containing Codex, Claude, Pi and Harbor Terminus 2.

Build an AgentFlow wheel from the desired checkout and put it under `wheels/` in
a temporary build context alongside this Dockerfile. Record the wheel SHA-256
and final image identity. The recipe contains no task tools, endpoints or grader.
The calling application supplies its Docker isolation policy and mounted inputs.
