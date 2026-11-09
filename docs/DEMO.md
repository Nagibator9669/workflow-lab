# Showing the work

Use the running application and the README screenshots when a client asks for an example. Keep the claim specific:

> I built Workflow Lab to demonstrate three parts of a reliable automation: reviewing a request before it becomes a task, validating data before an import, and preventing duplicate work when webhooks retry. It runs locally with FastAPI and SQLite. I can adapt the relevant part after reviewing your actual requirements.

## Demonstrate behavior, not just appearance

- Create one task and move it to In progress. Refresh to show persistence.
- Preview the incomplete request. Point out that missing details remain blank.
- Run the bundled CSV. Open the downloaded result and show that the bad rows are absent and the formula is text.
- Send one webhook twice. Show that both receipts reference the same task.
- Send an invalid signature. Show that the task count does not increase.

If asked about AI: the current intake parser is rule-based. An LLM integration is possible future client work, not a feature to claim in this demo.

Do not describe the sample organizations as clients, claim this is production-ready, or promise a result before inspecting a client's code and scope.
