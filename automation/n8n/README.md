# n8n flow: weekly content autopilot

`socialpilot-content-autopilot.json` is an n8n workflow that drives SocialPilot through its API:

1. **Run now** (manual) or **Every Monday 09:00** (schedule) starts the run.
2. **Settings** holds the SocialPilot URL, the login and the week's topics (one per line).
3. **Sign in to SocialPilot** logs in and keeps the session cookie.
4. **Split topics** turns the topic list into one item per topic, alternating LinkedIn and Instagram.
5. **Generate content** creates post variations for each topic (demo mode: no AI provider is called).
6. **Save as draft** and **Submit for approval** put every post into the business's approval queue.
7. **Run summary** reports what was created and what happens next.

Nothing is published by the flow: an approver still reviews each draft in SocialPilot, then schedules it.

## Use it
1. In n8n: **Workflows → Import from file** and pick the JSON file.
2. Open **Settings** and set `baseUrl`, `email` and `password` for a SocialPilot account (replace `CHANGE_ME`).
3. Click **Execute workflow**. To run it weekly, activate the workflow.

For production use, move the password into an n8n credential instead of the Settings node.
