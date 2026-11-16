# Wakeup assistant: scope

**Status: Not built.** This is a scope document only. No wakeup code, route, table or
schedule exists. Owner request: "homeassistant runs my home, but we can put a pin in
this, just scope out the idea of waking me up if I am not clearly active by seeing
activity on my phone or physical movement from the phone."

## Goal

If the owner is not clearly awake and active by a chosen time, wake them: play music on
the Echos and flash the lights, escalating until there is evidence of activity.

## Trigger

- A wake-by time per day (default none; the feature does nothing until one is set).
- At that time, evaluate "clearly active". If yes, do nothing. If no, start the ladder.
- A snooze/"I'm up" action (web, iPhone) ends the ladder for the day.

## Signals ("clearly active")

| Signal | Source | Notes |
|---|---|---|
| Steps or walking since the wake window opened | Apple Health samples (FEAT-002, `health_samples`, kinds steps / walking) | Bridge syncs in batches, so data can be minutes old. |
| Phone unlocked / app foreground | New iPhone app event (not built) | Needs an iOS signal that does not exist today. |
| Messages sent from the owner's phone | Messages relay | Outgoing only; the relay sees what the Mac sees. Never read content, only "an outgoing message happened". |
| Sleep state | Apple Health sleep records | An unfinished sleep night is a hint, not proof of being asleep. |
| Home presence/motion | Home Assistant sensors | Optional extra evidence. |

A rule needs at least two independent signals before it decides "not active"; one stale
source must not wake someone who is up.

## Actions (Home Assistant)

Home Assistant runs the home. NerveCenter would call HA's REST API with a long-lived
token held in a Secret (never logged): `media_player.play_media` on the Echos (volume
ramp), `light.turn_on` / flash effect on named lights, optionally a notification to the
phone. The ladder: gentle (soft music, dim lights), medium (louder, lights flash),
final (phone notification and a push). Each step has a delay and a hard stop.

## False-wake risks

- Stale health data (bridge offline) looks like "no activity". Mitigation: if the last
  sync is older than a threshold, send a phone notification only, no Echo/lights.
- Owner awake but away from the phone (phone on the charger). Mitigation: home motion
  sensor signal, snooze, and a long first grace period.
- Travel and time zones: use the phone's current zone, not the server's.
- Illness, a late night, or sleeping in on purpose. Mitigation below.
- Waking other people in the house. Mitigation: only named rooms/lights/Echos.
- A bug loops the ladder. Mitigation: a hard cap per day and a kill switch.

## Quiet hours, vacation and overrides

- Per-weekday wake-by times, a "do not wake before" floor, and a one-tap "off today".
- Vacation / away mode (date range) turns the feature off entirely.
- Re-arm only after a manual enable following any kill switch use.

## Privacy

- Uses only counts and timestamps from health data and "an event happened" flags; no
  message, mail or health content is read for this feature.
- Evaluation runs inside NerveCenter; nothing is sent to a cloud model.
- The HA token and any phone push credentials live in Secrets; audit log records the
  ladder step and outcome, not signal values.

## Open questions

1. Which Echos and lights, and the HA entity ids?
2. Which wake-by times, per weekday?
3. Is an iPhone "phone in use" signal wanted (new app work), or are health and HA enough?
4. Is the final step a phone notification, a call, or only the house?
5. Where does the HA token come from (existing HA instance URL and a long-lived token)?
6. Should mental-health or sleep trends adjust the schedule? (Not by default.)
