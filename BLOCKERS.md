# Blockers

Things only Easton can do. Each says what is needed, why, and what the system does meanwhile.

| # | Needed | Why | Meanwhile |
|---|---|---|---|
| B1 | **Log the Pi back into Tailscale:** `sudo tailscale up` and open the login URL. Then put `tailscale ip -4` into `.env` as `CF_BIND_HOST`, set `CF_PUBLIC_BASE_URL=http://raspberrypi:8420` (or your MagicDNS name), and run `systemctl --user restart clipforge-web`. | `tailscale status` on 2026-10-26 said "Logged out". Your phone and laptop reach the UI only through Tailscale. | The UI binds to `127.0.0.1:8420`. It's reachable from the Pi itself, or via `ssh -L 8420:127.0.0.1:8420`. |
| B2 | **Anthropic API key** from console.anthropic.com (prepaid credits; $5 lasts many months on Haiku). Put it in `.env` as `CF_ANTHROPIC_API_KEY`. | Clip selection. See D9 for why not the Pro CLI. | `select` jobs are deferred and retried every 6 h. To use your Pro plan anyway, set `CF_SELECT_PROVIDER=claude_cli` (read D9 first). |
| B3 | **Groq API key** from console.groq.com (free tier, no card). Put it in `.env` as `CF_GROQ_API_KEY` on the Pi and on the laptop. | Transcription with word timestamps. | `transcribe` jobs are deferred and retried every 6 h. |
| B4 | **Discord webhook:** in your server, Channel → Edit → Integrations → Webhooks → New → Copy URL. Put it in `.env` as `CF_DISCORD_WEBHOOK_URL`. | "N clips ready" pings. | Notifications go to the log only (`journalctl --user -u clipforge-worker-render`). |
| B5 | **Laptop setup:** copy the repo to the laptop (git clone over Tailscale SSH, or a zip), run `scripts\laptop\setup.ps1`, then paste `CF_WORKER_TOKEN` from the Pi's `.env` and the Groq key. Needs B1. | Optional worker with large storage. | The Pi does everything alone, with a 5 GB raw-media cap. |
| B6 | **Claim social handles in the apps:** TikTok + YouTube first. Top picks are in research/account-names.md; TikTok and X availability couldn't be verified from the web. | Accounts need a 3-day warm-up before day-1 posting. | Nothing; this is the critical path to first income. |
| B7 | **Pick 1–2 real Clipping.net campaigns** and enter them on the Campaigns page with their real rate, minimum views, cap, required tags and footage links. | Clipping.net blocked our research bot, so rates in research are second-hand. | The pipeline is tested with fixture data only. |
| B8 | **One real reboot test:** run `sudo reboot`, then check `systemctl --user status 'clipforge-*'`. | Definition of done: "services survive a Pi reboot". I didn't reboot your Pi myself. | The units are enabled under lingering, so they should start at boot. |
