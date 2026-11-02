# Blockers

Things only Easton can do. Each says what is needed, why, and what the system does meanwhile.

| # | Needed | Why | Meanwhile |
|---|---|---|---|
| B1 | ✅ Done 2026-10-26. Tailscale is up; the UI is bound to `100.x.y.z:8420`. | | |
| B2 | ✅ Done 2026-10-26. Anthropic key set and verified. | | |
| B3 | ✅ Done 2026-10-26. Groq key set and verified. | | |
| B4 | ✅ Done 2026-10-26. Discord webhook set and verified. | | |
| B5 | **Laptop setup:** copy the repo to the laptop (git clone over Tailscale SSH, or a zip), run `scripts\laptop\setup.ps1`, then paste `CF_WORKER_TOKEN` from the Pi's `.env` and the Groq key. Needs B1. | Optional worker with large storage. | The Pi does everything alone, with a 5 GB raw-media cap. |
| B6 | ✅ Done. @ggcutroom on TikTok and YouTube. Still do the 3-day warm-up (launch playbook). | | |
| B7 | **Partly done.** Campaigns were evaluated in research/campaigns-2026-10-26.md. Still needed: copy the text of the Clipping.net-hosted requirement docs (GFuel gfuel-2, Brandon Joe Williams) and the Man On episode-1 doc (its link didn't open from the screenshot), and download the Man On logo PNGs from the brief's Drive folder. | Clipping.net docs are behind a bot checkpoint we don't bypass. | Man On episode-4 is ready to enter. |
| B8 | **One real reboot test:** run `sudo reboot`, then check `systemctl --user status 'clipforge-*'`. | Definition of done: "services survive a Pi reboot". I didn't reboot your Pi myself. | The units are enabled under lingering, so they should start at boot. |
| B9 | **YouTube engaged views by API:** a Google Cloud OAuth client (YouTube Analytics API, read-only scope) for the channel. | The Analytics API needs the channel owner's sign-in; engaged views aren't public. | Enter engaged views by hand next to views on YouTube posts (Posted tab). |
| B10 | **Which view count Content Rewards pays on, per platform:** ask CR support or read it off a campaign page, then set "YouTube pays on" per bounty. | Their terms say views are verified through platform APIs but don't name the metric. | `pays_on` stays "not stated"; estimates use public views. |

